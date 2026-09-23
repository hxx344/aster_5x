"""Selected USD1 maximum-leverage alerts, updated inside Store transactions."""
from datetime import datetime, timezone
import json
import math
import re
import uuid

import monitor
from .listings import STALE_SECONDS, STATE_KEY
from .models import TradingError

WATCH_PREFIX = "usd1_watch:"
MESSAGE_PREFIX = "usd1-capacity:"


def read(db, key, default=None):
    row = db.execute("SELECT data FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def write(db, key, value):
    db.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data",
               (key, json.dumps(value, ensure_ascii=False, separators=(",", ":"))))


def sample(state, row, now):
    """Unknown never means zero, including when only the leverage read succeeded."""
    if not state.get("initialized") or state.get("error") or not row or row.get("error") or row.get("status") != "TRADING":
        return None
    stamps = [state.get("checked_at"), row.get("checked_at"), row.get("brackets_checked_at")]
    if any(type(stamp) not in (int, float) or not math.isfinite(stamp) or not -1 <= now - stamp < STALE_SECONDS for stamp in stamps):
        return None
    leverage = row.get("max_leverage")
    if type(leverage) is not int or not 1 <= leverage <= 10000:
        return None
    try:
        amount, remaining, cap = (monitor.number(row.get(key)) for key in ("capacity", "remaining", "bracket_cap"))
    except monitor.MonitorError:
        return None
    if amount != min(remaining, cap):
        return None
    return leverage, amount, row["checked_at"], min(stamps) + STALE_SECONDS


def cancel(db, gate, *, forget=False):
    if gate.get("pending_id"):
        db.execute("UPDATE outbox SET expires_at=0 WHERE id=? AND delivered_at IS NULL", (gate["pending_id"],))
    if forget:
        gate["pending_id"] = None


def observe(db, symbol, gate, state, now):
    row = state.get("rows", {}).get(symbol)
    current = sample(state, row, now)
    if not current or current[2] < gate.get("checked_at", 0):
        cancel(db, gate)
        return
    leverage, amount, checked_at, expires_at = current
    if gate.get("leverage") != leverage:
        cancel(db, gate, forget=True)
        gate["notified"] = False
    gate.update(leverage=leverage, checked_at=checked_at)
    if amount == 0:
        cancel(db, gate, forget=True)
        gate["notified"] = False
    elif not gate.get("notified"):
        stamp = datetime.fromtimestamp(checked_at, timezone.utc).isoformat()
        message = (f"ASTER USD1 最大杠杆额度提醒\n交易对：{symbol}\n最大杠杆：{leverage}x\n"
                   f"该杠杆公开可用额度：{amount} USD1\n触发条件：> 0 USD1\n"
                   f"公开剩余额度：{row['remaining']} USD1\n该杠杆档位上限：{row['bracket_cap']} USD1\n"
                   f"额度采样时间：{stamp}\n"
                   "公开可用额度取公开剩余额度与档位上限的较小值，非账户可开额度。")
        if gate.get("pending_id"):
            # A recovered sample revives this episode without erasing retry backoff.
            db.execute("UPDATE outbox SET message=?,expires_at=? WHERE id=? AND delivered_at IS NULL",
                       (message, expires_at, gate["pending_id"]))
        else:
            gate["pending_id"] = f"{MESSAGE_PREFIX}{symbol}:{uuid.uuid4().hex}"
            db.execute("INSERT INTO outbox(id,message,due_at,expires_at) VALUES (?,?,?,?)",
                       (gate["pending_id"], message, now, expires_at))
    write(db, WATCH_PREFIX + symbol, gate)


def observe_all(db, state, now):
    for row in db.execute("SELECT key,data FROM kv WHERE key GLOB 'usd1_watch:*'").fetchall():
        gate = json.loads(row["data"])
        if gate.get("enabled"):
            observe(db, row["key"][len(WATCH_PREFIX):], gate, state, now)


def set_watch(db, symbol, enabled, now):
    if not isinstance(symbol, str) or not re.fullmatch(r"[\w.-]{1,80}", symbol) or type(enabled) is not bool:
        raise TradingError("交易对或额度提醒开关无效")
    state = read(db, STATE_KEY, {})
    key = WATCH_PREFIX + symbol
    previous = read(db, key, {})
    if symbol not in state.get("rows", {}) and (enabled or not previous):
        raise TradingError("请先等待该 USD1 交易对出现在上新列表中")
    if previous.get("enabled", False) == enabled:
        return  # Repeated saves are idempotent, including after a lost HTTP response.
    cancel(db, previous, forget=True)
    gate = {"enabled": enabled, "notified": False, "pending_id": None}
    write(db, key, gate)
    if enabled:
        observe(db, symbol, gate, state, now)


def selected_symbols(db):
    return [row["key"][len(WATCH_PREFIX):] for row in db.execute(
        "SELECT key,data FROM kv WHERE key GLOB 'usd1_watch:*' ORDER BY key").fetchall()
        if json.loads(row["data"]).get("enabled")]


def message_symbol(item):
    parts = item["id"].split(":")
    return parts[1] if len(parts) == 3 and parts[0] == "usd1-capacity" else None


def deliverable(db, item, now):
    symbol = message_symbol(item)
    gate = read(db, WATCH_PREFIX + symbol, {}) if symbol else {}
    if not gate.get("enabled") or gate.get("notified") or gate.get("pending_id") != item["id"]:
        return False
    state = read(db, STATE_KEY, {})
    current = sample(state, state.get("rows", {}).get(symbol), now)
    return bool(current and current[1] > 0 and current[0] == gate.get("leverage") and current[2] == gate.get("checked_at"))


def delivered(db, item):
    symbol = message_symbol(item)
    if not symbol:
        return
    key = WATCH_PREFIX + symbol
    gate = read(db, key, {})
    # Every zero, leverage change, or re-selection uses a new message id. A late
    # success from an old in-flight request cannot consume a new episode.
    if gate.get("enabled") and gate.get("pending_id") == item["id"]:
        gate.update(notified=True, pending_id=None)
        write(db, key, gate)
