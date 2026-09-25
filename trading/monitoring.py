"""Persisted monitoring choices and the shared notification delivery policy."""
import json
import re
import time

from .models import SYMBOLS, TradingError

KEY = "monitoring_settings"
DEFAULTS = {
    "monitoring_enabled": True,
    "discovery_enabled": True,
    "auto_monitor_new": True,
    "feishu_enabled": True,
    "new_listing_alerts": True,
    "strategy_capacity_alerts": True,
    "listing_capacity_alerts": True,
    "trade_summary_alerts": True,
}
CATEGORIES = {"new_listing", "strategy_capacity", "listing_capacity", "trade_summary"}


def read(db):
    row = db.execute("SELECT data FROM kv WHERE key=?", (KEY,)).fetchone()
    return {**DEFAULTS, "symbols": {}, "revision": 0, **(json.loads(row[0]) if row else {})}


def write(db, config):
    db.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data",
               (KEY, json.dumps(config, ensure_ascii=False, separators=(",", ":"))))


def symbol_options(config, symbol):
    return {"monitor": True if symbol in SYMBOLS else config["auto_monitor_new"], "alerts": True,
            **config["symbols"].get(symbol, {})}


def monitored(config, symbol):
    return config["monitoring_enabled"] and symbol_options(config, symbol)["monitor"]


def allowed(config, category, symbols):
    if category not in CATEGORIES or not config["feishu_enabled"] or not config[category + "_alerts"]:
        return False
    if not all(symbol_options(config, symbol)["alerts"] for symbol in symbols):
        return False
    if category != "trade_summary" and not all(monitored(config, symbol) for symbol in symbols):
        return False
    return category != "new_listing" or config["discovery_enabled"]


def metadata(item):
    if item.get("category"):
        return item["category"], json.loads(item["symbols"] or "[]")
    if item.get("capacity_key"):
        return "strategy_capacity", [item["capacity_key"].split(":")[1]]
    parts = item["id"].split(":")
    if parts[0] in ("usd1-listing", "usd1-capacity") and len(parts) >= 2:
        return ("new_listing" if parts[0] == "usd1-listing" else "listing_capacity"), [parts[1]]
    # Legacy trade messages have no trustworthy symbol metadata. Preserve them
    # by default; cancel the whole old aggregate if any constituent is muted.
    return "trade_summary", list(SYMBOLS)


def message_allowed(db, item, config=None):
    category, symbols = metadata(item)
    return allowed(config or read(db), category, symbols)


def validate_edit(changes, fields):
    if not changes or set(changes) - set(fields) or any(type(value) is not bool for value in changes.values()):
        raise TradingError("请提交有效的监控或告警开关")


def validate_symbol(db, symbol):
    state = db.execute("SELECT data FROM kv WHERE key='usd1_listings'").fetchone()
    rows = json.loads(state[0]).get("rows", {}) if state else {}
    if (not isinstance(symbol, str) or not re.fullmatch(r"[\w.-]{1,80}", symbol)
            or symbol not in set(SYMBOLS) | set(rows)):
        raise TradingError("请选择列表中已有的交易对")


def register_symbols(db, symbols):
    config = read(db)
    missing = set(symbols) - config["symbols"].keys()
    if missing:
        for symbol in missing:
            config["symbols"][symbol] = symbol_options(config, symbol)
        write(db, config)
    return config


def cancel_disabled(db, config):
    """Cancellation is durable: later re-enabling cannot revive old retries."""
    for row in db.execute("SELECT * FROM outbox WHERE delivered_at IS NULL AND (expires_at IS NULL OR expires_at>0)").fetchall():
        item = dict(row)
        if not message_allowed(db, item, config):
            db.execute("UPDATE outbox SET expires_at=0 WHERE id=?", (item["id"],))
    for row in db.execute("SELECT key,data FROM kv WHERE key GLOB 'capacity_alert:*' OR key GLOB 'usd1_watch:*'").fetchall():
        parts = row["key"].split(":")
        category = "strategy_capacity" if parts[0] == "capacity_alert" else "listing_capacity"
        if not allowed(config, category, [parts[1]]):
            gate = json.loads(row["data"])
            gate.update(pending_id=None, notified=False, resume_after=time.time())
            db.execute("UPDATE kv SET data=? WHERE key=?", (json.dumps(gate), row["key"]))
    row = db.execute("SELECT data FROM kv WHERE key='usd1_listings'").fetchone()
    if row:
        state = json.loads(row[0])
        if not config["monitoring_enabled"] or not config["discovery_enabled"]:
            # Resuming discovery establishes a baseline for the disabled period.
            state["rebaseline"] = True
        for symbol, listing in state.get("rows", {}).items():
            if not allowed(config, "new_listing", [symbol]) and listing.get("notification_phase") in ("pending", "incomplete"):
                listing["notification_phase"] = "suppressed"
        db.execute("UPDATE kv SET data=? WHERE key='usd1_listings'", (json.dumps(state, ensure_ascii=False),))
