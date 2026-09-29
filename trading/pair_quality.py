"""Optional, bounded observations of original paired-cycle submissions.

This module never queries an exchange or changes order/recovery decisions.
Elapsed times use the local monotonic clock; only durations and wall times persist.
"""
from contextlib import contextmanager
from copy import deepcopy
import json
import time

from . import cycle_quality as quality, cycle_quality_history as history
from .exchange import RequestNotSent
from .request_timing import observe_transport


def wall():
    try:
        return quality.timestamp(time.time())
    except Exception:
        return None


def intent(batch):
    legs = batch.get("legs", [])
    return {"id": "pair:" + batch["id"], "kind": "cycle", "symbol": batch["symbol"],
            "phase": batch["phase"], "quantity": batch["quantity"], "created_at": batch["created_at"],
            "orders": [leg["order"] for leg in legs], "repairs": batch.get("repairs", []),
            "receipts": {leg["order"]["newClientOrderId"]: leg["receipt"]
                         for leg in legs if leg.get("receipt") is not None}}


def prepare(batch, pair_id, depth=None, *, final=False):
    try:
        if batch.get("kind") != "cycle":
            return
        row = batch.get("execution_quality")
        if not isinstance(row, dict):
            row = quality.new_quality(intent(batch))
            row.update(scope="pair", pair_id=pair_id)
            batch["execution_quality"] = row
        if depth is not None:
            row["final_estimate" if final else "trigger_estimate"] = quality.estimate(
                batch["quantity"], depth, wall())
    except Exception:
        # An unavailable observation must never prevent a submission/recovery.
        pass


@contextmanager
def observe(leg, clocks, enabled):
    if not enabled:
        yield
        return
    started, start_wall = quality.clock_tick(), wall()
    timing = {"request_status": "unknown", "request_started_at": start_wall,
              "response_received_at": None, "request_to_response_ms": None}
    transport = {}
    try:
        with observe_transport(transport):
            yield
    except Exception as exc:
        timing["request_status"] = "not_sent" if isinstance(exc, RequestNotSent) else "failed"
        raise
    else:
        timing.update(request_status="returned", response_received_at=wall(),
                      request_to_response_ms=quality.elapsed(started, quality.clock_tick()))
    finally:
        try:
            if transport:
                timing["transport"] = transport
            leg["execution_timing"] = timing
            clocks[leg["key"]] = (started, quality.clock_tick())
        except Exception:
            pass


def complete(batch, clocks):
    """Record the two-call envelope only after both original workers finish."""
    try:
        row = batch.get("execution_quality")
        if not isinstance(row, dict):
            return
        legs = batch["legs"]
        timings = [leg.get("execution_timing", {}) for leg in legs]
        sent = [item for item in timings if item.get("request_status") in ("returned", "failed")
                and quality.timestamp(item.get("request_started_at")) is not None]
        timing = row["timing"]
        timing.update(request_status="unknown", request_started_at=None,
                      response_received_at=None, request_to_response_ms=None)
        if not sent:
            if all((leg.get("receipt") or {}).get("local_not_sent") for leg in legs):
                timing["request_status"] = "not_sent"
            return
        timing.update(request_started_at=min(item["request_started_at"] for item in sent),
                      request_status="failed")
        ticks = [clocks.get(leg["key"], (None, None)) for leg in legs]
        if (len(legs) == 2 and all(item.get("request_status") == "returned" for item in timings)
                and all(quality.timestamp(value) is not None for pair in ticks for value in pair)
                and all(quality.timestamp(item.get("response_received_at")) is not None for item in timings)):
            duration = quality.elapsed(min(pair[0] for pair in ticks), max(pair[1] for pair in ticks))
            if duration is not None:
                timing.update(request_status="returned", request_to_response_ms=duration,
                              response_received_at=max(item["response_received_at"] for item in timings))
    except Exception:
        pass


def view(pair_id, batch):
    if not isinstance(batch, dict) or batch.get("kind") != "cycle":
        return None
    normalized = intent(batch)
    row = batch.get("execution_quality")
    if not isinstance(row, dict) or row.get("intent_id") != normalized["id"]:
        row = quality.new_quality(normalized)
        # Old receipts can recover prices, never missing request/quote clocks.
        row["updated_at"] = batch.get("finished_at", batch["created_at"])
    row = deepcopy(row)
    row.update(scope="pair", pair_id=pair_id, actual=quality.actual(normalized))
    row["timing"]["legs"] = {
        leg["key"]: {"account_id": batch.get("identities", {}).get(leg["key"], {}).get("account_id"),
                     **deepcopy(leg.get("execution_timing", {}))}
        for leg in batch["legs"] if leg.get("key") in ("long", "short")}
    return row


def record(store, pair_id, batch):
    """Read committed receipts before writing display metadata; failures are isolated."""
    try:
        if (not isinstance(batch, dict) or batch.get("kind") != "cycle"
                or history.request_time(batch.get("execution_quality") or {}) is None):
            return
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            saved = db.execute("SELECT data FROM kv WHERE key=?", ("pair_batch:" + batch["id"],)).fetchone()
            if saved:
                saved = json.loads(saved[0])
            else:
                runtime = db.execute("SELECT data FROM kv WHERE key=?", ("pair_runtime:" + pair_id,)).fetchone()
                saved = (json.loads(runtime[0]).get("pending") if runtime else None)
            if not saved or saved.get("id") != batch["id"] or saved.get("identities") != batch.get("identities"):
                return
            if (saved.get("execution_quality") or {}).get("pair_id") != pair_id:
                return
            incoming = view(pair_id, saved)
            if incoming is None:
                return
            adapted = {**intent(saved), "execution_quality": incoming}
            merged = history.merge(db, adapted, incoming)
            key = "pair_execution:" + pair_id
            latest_row = db.execute("SELECT data FROM kv WHERE key=?", (key,)).fetchone()
            latest = json.loads(latest_row[0]) if latest_row else None
            if merged == latest:
                return
            merged["updated_at"] = wall()
            history.record(db, "pair:" + pair_id, merged)
            if not latest or latest.get("created_at", 0) <= merged["created_at"]:
                db.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                           (key, json.dumps(merged, ensure_ascii=False, allow_nan=False)))
    except Exception:
        # Display persistence must not roll back an already committed trade.
        pass


def read(reader, pair_id, runtime):
    try:
        latest = reader.get("pair_execution:" + pair_id)
        batch = runtime.get("pending")
        if not isinstance(batch, dict) or batch.get("kind") != "cycle":
            last = runtime.get("last_batch") or {}
            batch = reader.get("pair_batch:" + last["id"]) if last.get("kind") == "cycle" else None
        current = view(pair_id, batch)
        if current and (not latest or current["created_at"] >= latest.get("created_at", 0)):
            if latest and current["intent_id"] == latest.get("intent_id") and history.request_time(latest) is not None:
                latest["actual"] = current["actual"]
            else:
                latest = current
        return {"execution_quality": latest,
                "execution_quality_history": reader.cycle_execution_quality_history("pair:" + pair_id)}
    except Exception:
        return {"execution_quality": None, "execution_quality_history": None}
