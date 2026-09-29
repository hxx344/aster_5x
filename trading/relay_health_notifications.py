"""Durable relay incidents, observed from local WS telemetry only."""
import json
import math
import uuid
from copy import deepcopy

from . import monitoring

KEY = "relay_health_notifications"
CATEGORY = "relay_health"
FAILURE_LIMIT = 3
DISCONNECT_SECONDS = 60
OI_IDLE_SECONDS = 120
RECOVERY_SECONDS = 30


def read(db):
    row = db.execute("SELECT data FROM kv WHERE key=?", (KEY,)).fetchone()
    return json.loads(row[0]) if row else {}


def write(db, state):
    db.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data",
               (KEY, json.dumps(state, separators=(",", ":"))))


def cancel(db, message_id):
    if message_id:
        db.execute("UPDATE outbox SET expires_at=0 WHERE id=? AND delivered_at IS NULL", (message_id,))


def reset(db):
    db.execute("UPDATE outbox SET expires_at=0 WHERE category=? AND delivered_at IS NULL", (CATEGORY,))
    write(db, {"rebaseline": True})


def age(value):
    return float(value) if type(value) in (float, int) and math.isfinite(value) and value >= 0 else None


def queue(db, identity, message, now):
    db.execute("""INSERT INTO outbox(id,message,due_at,category,symbols) VALUES (?,?,?,?,'[]')
        ON CONFLICT(id) DO UPDATE SET message=excluded.message,expires_at=NULL
        WHERE outbox.delivered_at IS NULL""", (identity, message, now, CATEGORY))


def observe(db, status, *, available, revision, now):
    config = monitoring.read(db)
    if revision != config["revision"]:
        return False
    if (not available or not monitoring.allowed(config, CATEGORY, []) or not status
            or not status.get("enabled") or status.get("closed")):
        if read(db) != {"rebaseline": True}:
            reset(db)
        return False
    ws = status["ws"]
    instance = status["instance_id"]
    count = ws["failure_count"]
    if not isinstance(instance, str) or not instance or type(count) is not int or count < 0:
        raise ValueError("Invalid relay health telemetry")
    connected = status.get("connected") is True
    connection_age = age(ws.get("connected_age_seconds")) if connected else None
    disconnected_age = age(ws.get("disconnected_age_seconds")) if not connected else None
    idle = age(ws.get("oi_idle_seconds")) if connected else None
    state = read(db)
    previous = deepcopy(state)
    fresh = state.pop("rebaseline", False)
    changed_instance = state.get("instance_id") != instance
    failure_changed = count != state.get("last_failure_count", count)
    changed_connection = (changed_instance
        or state.get("connected_at") != ws.get("connected_at")
        or (connection_age is not None and state.get("last_connection_age") is not None
            and connection_age < state["last_connection_age"]))
    if changed_instance or fresh:
        state.update(failure_base=count if fresh else 0, healthy_since_age=None,
                     disconnected_base=disconnected_age if fresh else 0, idle_base=idle if fresh else 0)
    elif changed_connection:
        state.update(healthy_since_age=None, idle_base=0)
    if failure_changed:
        state["healthy_since_age"] = None
    if connected:
        state["disconnected_base"] = 0
    if idle is not None and state.get("last_idle") is not None and idle < state["last_idle"]:
        state["idle_base"] = 0
    if disconnected_age is not None and state.get("last_disconnected_age") is not None and disconnected_age < state["last_disconnected_age"]:
        state["disconnected_base"] = 0
    state.update(instance_id=instance, last_failure_count=count, connected_at=ws.get("connected_at"),
                 last_connection_age=connection_age, last_disconnected_age=disconnected_age, last_idle=idle)
    healthy = connected and connection_age is not None and ws.get("has_oi_sample") is True and idle is not None and idle < OI_IDLE_SECONDS
    if not healthy:
        state["healthy_since_age"] = None
    elif state.get("healthy_since_age") is None:
        state["healthy_since_age"] = connection_age
    stable = healthy and connection_age - state["healthy_since_age"] >= RECOVERY_SECONDS
    reasons = []
    if count - state.get("failure_base", 0) >= FAILURE_LIMIT:
        reasons.append("WS 连续失败已达 3 次")
    if disconnected_age is not None and disconnected_age - (state.get("disconnected_base") or 0) >= DISCONNECT_SECONDS:
        reasons.append("WS 持续断连已达 60 秒")
    if idle is not None and idle - (state.get("idle_base") or 0) >= OI_IDLE_SECONDS:
        reasons.append("WS 已连接，但 120 秒没有新的有效公开额度样本")
    incident = state.get("incident")
    if stable:
        state["failure_base"] = count
        if incident:
            cancel(db, incident["fault_id"])
            if incident["fault_sent"]:
                incident["recovering"] = True
                queue(db, incident["recovery_id"], "ASTER 副服务器 WS 已恢复\n连接与有效公开额度样本已持续稳定 30 秒。", now)
            else:
                cancel(db, incident["recovery_id"])
                state["incident"] = None
    else:
        if incident:
            cancel(db, incident["recovery_id"])
            incident["recovering"] = False
        elif reasons:
            identity = uuid.uuid4().hex
            incident = {"fault_id": f"relay-health:{identity}:fault", "recovery_id": f"relay-health:{identity}:recovery",
                        "fault_sent": False, "recovering": False,
                        "reason": "；".join(reasons)}
            state["incident"] = incident
        if incident and not incident["fault_sent"]:
            http = status.get("http") or {}
            success = age(http.get("last_success_at"))
            if http.get("last_error"):
                fallback = "HTTP 补取也有异常。"
            elif success is not None and 0 <= now - success <= 120:
                fallback = "最近 120 秒 HTTP 补取成功；WS 状态仍异常。"
            else:
                fallback = "暂无近期 HTTP 补取成功记录。"
            message = "ASTER 副服务器 WS 异常\n" + incident["reason"] + "。\n" + fallback
            queue(db, incident["fault_id"], message, now)
    if state != previous:
        write(db, state)
    return True


def deliverable(db, item):
    incident = read(db).get("incident")
    if not incident:
        return False
    if item["id"] == incident["fault_id"]:
        return not incident["fault_sent"] and not incident["recovering"]
    return item["id"] == incident["recovery_id"] and incident["fault_sent"] and incident["recovering"]


def delivered(db, item):
    state = read(db)
    incident = state.get("incident")
    if not incident:
        return
    if item["id"] == incident["fault_id"]:
        incident["fault_sent"] = True
    elif item["id"] == incident["recovery_id"]:
        state["incident"] = None
    else:
        return
    write(db, state)
