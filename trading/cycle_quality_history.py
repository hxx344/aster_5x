"""Bounded, display-only history of original cycle batch requests."""
import copy
import json
import math

LIMIT = 100


def finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def request_time(quality):
    timing = quality.get("timing") or {}
    value = timing.get("request_started_at")
    if timing.get("request_status") in ("returned", "failed") and finite(value) and value <= 253402300799:
        return value
    return None


def response_ms(quality):
    timing = quality.get("timing") or {}
    start, end, value = request_time(quality), timing.get("response_received_at"), timing.get("request_to_response_ms")
    if timing.get("request_status") == "returned" and start is not None and finite(end) \
            and start <= end <= 253402300799 and finite(value):
        return value
    return None


def initialize(db):
    existed = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cycle_quality_history'").fetchone()
    db.execute("""CREATE TABLE IF NOT EXISTS cycle_quality_history (
        intent_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, symbol TEXT NOT NULL,
        phase TEXT NOT NULL, requested_at REAL NOT NULL, response_ms REAL, data TEXT NOT NULL)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_cycle_quality_window ON cycle_quality_history
        (account_id,symbol,phase,requested_at DESC,intent_id DESC)""")
    if existed:
        return
    # One upgrade pass; ordinary dashboard reads never scan durable intents.
    for row in db.execute("SELECT id,account_id,data FROM intents"):
        try:
            intent = json.loads(row["data"])
            quality = intent.get("execution_quality")
            if intent.get("kind") != "cycle" or not isinstance(quality, dict) or quality.get("version") != 1 \
                    or intent.get("id") != row["id"] or intent.get("account_id") != row["account_id"] \
                    or quality.get("intent_id") != row["id"] \
                    or any(quality.get(key) != intent.get(key) for key in ("symbol", "phase", "quantity", "created_at")):
                continue
            record(db, row["account_id"], quality)
        except (TypeError, ValueError, AttributeError, KeyError, OverflowError):
            # Historical telemetry is optional, never fabricate missing clocks.
            continue


def merge(db, saved, incoming):
    """Freeze request observations; refresh fills from the persisted receipts."""
    from .cycle_quality import actual
    row = db.execute("SELECT data FROM cycle_quality_history WHERE intent_id=?", (saved["id"],)).fetchone()
    prior = json.loads(row["data"]) if row else saved.get("execution_quality")
    source = prior if isinstance(prior, dict) and request_time(prior) is not None else incoming
    quality = copy.deepcopy(source)
    quality["actual"] = actual(saved)
    updates = [value for value in (quality.get("updated_at"), incoming.get("updated_at")) if finite(value)]
    quality["updated_at"] = max(updates) if updates else None
    return quality


def record(db, account_id, quality):
    started = request_time(quality)
    if started is None or quality.get("version") != 1 or quality.get("phase") not in ("open", "close") \
            or not isinstance(quality.get("symbol"), str) or not quality["symbol"] \
            or not isinstance(quality.get("intent_id"), str) or not quality["intent_id"]:
        return
    serialized = json.dumps(quality, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    scope = (account_id, quality["symbol"], quality["phase"])
    db.execute("""INSERT INTO cycle_quality_history VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(intent_id) DO UPDATE SET data=excluded.data,response_ms=excluded.response_ms""",
        (quality["intent_id"], *scope, started, response_ms(quality), serialized))
    # Delayed updates keep their original position; an evicted request cannot
    # become new merely because reconciliation refreshed its updated_at.
    db.execute("""DELETE FROM cycle_quality_history WHERE intent_id IN (
        SELECT intent_id FROM cycle_quality_history WHERE account_id=? AND symbol=? AND phase=?
        ORDER BY requested_at DESC,intent_id DESC LIMIT -1 OFFSET ?)""", (*scope, LIMIT))


def summary(db, account_id):
    groups = []
    for row in db.execute("""SELECT symbol,phase,COUNT(*) AS count,COUNT(response_ms) AS comparable_count
        FROM cycle_quality_history WHERE account_id=? GROUP BY symbol,phase ORDER BY symbol,phase DESC""", (account_id,)):
        group = dict(row)
        for name, direction in (("best", "ASC"), ("worst", "DESC")):
            record_row = db.execute(f"""SELECT data FROM cycle_quality_history
                WHERE account_id=? AND symbol=? AND phase=? AND response_ms IS NOT NULL
                ORDER BY response_ms {direction},requested_at DESC,intent_id DESC LIMIT 1""",
                (account_id, row["symbol"], row["phase"])).fetchone()
            group[name] = json.loads(record_row["data"]) if record_row else None
        groups.append(group)
    return {"limit": LIMIT, "metric": "request_to_response_ms", "groups": groups}
