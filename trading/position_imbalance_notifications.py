"""Durable position incidents derived exclusively from fresh local snapshots."""
import json
import math
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from decimal import localcontext

from . import monitoring
from .models import TradingError, hedge_balanced, positive

KEY = "position_imbalance_notifications"
CATEGORY = "position_imbalance"
FAULT_SECONDS = 60
RECOVERY_SECONDS = 30
SAMPLE_MAX_AGE = 8
SAMPLE_FUTURE_ALLOWANCE = 1


def read(db):
    row = db.execute("SELECT data FROM kv WHERE key=?", (KEY,)).fetchone()
    return json.loads(row[0]) if row else {}


def write(db, state):
    db.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data",
               (KEY, json.dumps(state, ensure_ascii=False, separators=(",", ":"))))


def cancel(db, identity):
    if identity:
        db.execute("UPDATE outbox SET expires_at=0 WHERE id=? AND delivered_at IS NULL", (identity,))


def cancel_target(db, target):
    incident = target.get("incident") or {}
    cancel(db, incident.get("fault_id"))
    cancel(db, incident.get("recovery_id"))
    target["permitted_id"] = None


def reset(db, symbol=None):
    """Forget a disabled scope; enabling it starts a new observation window."""
    state = read(db)
    if symbol is None:
        db.execute("UPDATE outbox SET expires_at=0 WHERE category=? AND delivered_at IS NULL", (CATEGORY,))
        write(db, {})
        return
    targets = state.get("targets", {})
    for key, target in list(targets.items()):
        if target["symbol"] == symbol:
            cancel_target(db, target)
            del targets[key]
    # Also cover an older pending row whose state was already cleared.
    for row in db.execute("SELECT id,symbols FROM outbox WHERE category=? AND delivered_at IS NULL", (CATEGORY,)):
        if symbol in json.loads(row["symbols"] or "[]"):
            cancel(db, row["id"])
    write(db, state)


def finite_time(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def fresh_times(values, now):
    return (isinstance(values, list) and len(values) >= 2
            and all(finite_time(value) and now - SAMPLE_MAX_AGE <= value <= now + SAMPLE_FUTURE_ALLOWANCE
                    for value in values))


def snapshot(observation, now):
    times = observation.get("sample_times")
    if not fresh_times(times, now):
        return None
    try:
        long_qty = positive(observation.get("long_qty"), True)
        short_qty = positive(observation.get("short_qty"), True)
        balanced = hedge_balanced(long_qty, short_qty)
    except TradingError:
        return None
    return long_qty, short_qty, list(times), balanced


def clear_window(target):
    target["candidate"] = None
    target["last_times"] = None
    incident = target.get("incident")
    if incident and not incident["fault_sent"]:
        target["incident"] = None


def queue(db, identity, message, symbol, now):
    # A new observation may refresh the text, but never defeat retry backoff.
    db.execute("""INSERT INTO outbox(id,message,due_at,category,symbols) VALUES (?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET message=excluded.message,expires_at=NULL
        WHERE outbox.delivered_at IS NULL""",
        (identity, message, now, CATEGORY, json.dumps([symbol], separators=(",", ":"))))


def message(target, evidence, candidate, recovery):
    long_qty, short_qty, times, _ = evidence
    with localcontext() as context:
        context.prec = 256
        difference = abs(long_qty - short_qty)
        largest = max(long_qty, short_qty)
        percent = difference * 100 / largest if largest else 0
    stamps = " / ".join(datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="milliseconds")
                        for value in times)
    duration = max(0, candidate["confirmed_at"] - candidate["started_at"])
    title = "仓位已恢复平衡" if recovery else "仓位不平衡"
    text = (f"ASTER {title}\n主体：{target['label']}\n品种：{target['symbol']}\n"
            f"总多仓：{long_qty:f}\n总空仓：{short_qty:f}\n"
            f"差额：{difference:f}；占较大一侧：{percent:.4f}%\n"
            f"样本时间（UTC）：{stamps}\n"
            f"连续观察：{duration:.1f} 秒（{candidate['count']} 个新样本）")
    if target.get("attention"):
        text += "\n自动处理已暂停，请核对实际持仓。"
    return text


def observe(db, observations, *, available, revision, instance_id, now):
    config = monitoring.read(db)
    if revision != config["revision"]:
        return False
    if not available or not monitoring.allowed(config, CATEGORY, []):
        reset(db)
        return False
    if not finite_time(now) or not isinstance(instance_id, str) or not instance_id:
        raise ValueError("Invalid position observation identity or clock")
    state = read(db)
    previous = deepcopy(state)
    targets = state.setdefault("targets", {})
    changed_instance = state.get("instance_id") != instance_id
    clock_reversed = now < state.get("last_observed_at", now)
    state.update(instance_id=instance_id, last_observed_at=now)
    observations = {item["key"]: item for item in observations}

    for key, target in list(targets.items()):
        # Every pass revokes permission first. Only current valid evidence can
        # restore it, even if the persisted candidate has already matured.
        cancel_target(db, target)
        if key not in observations or not monitoring.allowed(config, CATEGORY, [target["symbol"]]):
            del targets[key]

    for key, observation in observations.items():
        symbol = observation["symbol"]
        if not monitoring.allowed(config, CATEGORY, [symbol]):
            continue
        target = targets.setdefault(key, {"symbol": symbol, "incident": None, "candidate": None})
        activity = observation.get("activity")
        changed_activity = target.get("activity") != activity or target["symbol"] != symbol
        was_suppressed = target.get("suppressed", False)
        suppressed = observation.get("suppressed") is not False or not isinstance(activity, str)
        target.update(symbol=symbol, label=observation.get("label") or key, activity=activity,
                      suppressed=suppressed, attention=bool(observation.get("attention")),
                      permitted_id=None, sample_times=None)
        target["capture"] = ({"key": key, "scope": observation["scope"], "activity": activity,
                              "bindings": observation.get("bindings")} if observation.get("scope") else None)
        if changed_instance or clock_reversed or changed_activity or suppressed or was_suppressed:
            clear_window(target)
        if suppressed:
            continue

        max_gap = observation.get("max_gap")
        max_gap = min(180, max(20, max_gap)) if finite_time(max_gap) else 20
        last_times = target.get("last_times")
        if last_times and now - min(last_times) > max_gap:
            # Silence is not recovery. A sent incident stays open, while its
            # interrupted confirmation window cannot authorize a later retry.
            target["candidate"] = None
            if target.get("incident") and not target["incident"]["fault_sent"]:
                target["incident"] = None
        evidence = snapshot(observation, now)
        if evidence is None:
            continue
        _, _, times, balanced = evidence
        if last_times and len(times) == len(last_times) and any(current < old for current, old in zip(times, last_times)):
            continue
        target["sample_times"] = times
        kind = "recovery" if balanced else "fault"
        candidate = target.get("candidate")
        if candidate and candidate["kind"] != kind:
            candidate = target["candidate"] = None
        incident = target.get("incident")
        if balanced and incident and not incident["fault_sent"]:
            # A single valid balanced read is enough to cancel an unsent fault.
            incident = target["incident"] = None
        newer = (not last_times or (len(times) == len(last_times)
                 and all(current > old for current, old in zip(times, last_times))))
        if newer:
            target["last_times"] = times
            if candidate is None:
                candidate = {"kind": kind, "started_at": max(times), "count": 1}
                target["candidate"] = candidate
            else:
                candidate["count"] += 1
            candidate["confirmed_at"] = min(times)
        elif last_times and len(times) != len(last_times):
            clear_window(target)
            continue
        if (candidate is None or candidate["count"] < 2
                or candidate["confirmed_at"] - candidate["started_at"]
                < (RECOVERY_SECONDS if balanced else FAULT_SECONDS)):
            continue
        # Repeated source timestamps do not grow a window, but a duplicate
        # fresh read can reauthorize an already-confirmed incident before send.
        if balanced:
            if not incident or not incident["fault_sent"]:
                continue
            identity = incident["recovery_id"]
        else:
            if incident is None:
                token = uuid.uuid4().hex
                incident = {"fault_id": f"position-imbalance:{token}:fault",
                            "recovery_id": f"position-imbalance:{token}:recovery", "fault_sent": False}
                target["incident"] = incident
            if incident["fault_sent"]:
                continue
            identity = incident["fault_id"]
        target["permitted_id"] = identity
        target["permitted_at"] = now
        queue(db, identity, message(target, evidence, candidate, balanced), symbol, now)

    if state != previous:
        write(db, state)
    return True


def deliverable(db, item, now=None):
    now = time.time() if now is None else now
    config = monitoring.read(db)
    for target in read(db).get("targets", {}).values():
        incident = target.get("incident")
        if (not incident or target.get("permitted_id") != item["id"] or target.get("suppressed")
                or not finite_time(now) or now < target.get("permitted_at", now)
                or not fresh_times(target.get("sample_times"), now)
                or not monitoring.allowed(config, CATEGORY, [target["symbol"]])):
            continue
        return ((item["id"] == incident["fault_id"] and not incident["fault_sent"])
                or (item["id"] == incident["recovery_id"] and incident["fault_sent"]))
    return False


def delivered(db, item):
    state = read(db)
    for target in state.get("targets", {}).values():
        incident = target.get("incident")
        if not incident:
            continue
        if item["id"] == incident["fault_id"]:
            incident["fault_sent"] = True
        elif item["id"] == incident["recovery_id"]:
            target["incident"] = None
        else:
            continue
        target["permitted_id"] = None
        write(db, state)
        return


def delivery_observation(db, item):
    """Return the capture identity for Store's final transactional SQL check."""
    return next((target.get("capture") for target in read(db).get("targets", {}).values()
                 if target.get("permitted_id") == item["id"]), None)
