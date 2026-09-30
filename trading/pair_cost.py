"""Read-only paired-cycle cost estimates from durable order receipts.

Fees use the display rate 1/8000. Matched BUY minus SELL consideration already
includes execution slippage; slippage against the final plan is attribution,
not an extra charge. Unknown timing or ownership never becomes a zero cost.
"""
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from fractions import Fraction
import json
import math
import threading
import time

from .cycle_quality import number
from .execution import Executor, TERMINAL
from .models import TradingError, positive


PREFIX = "pair_batch:"
FEE_RATE = Fraction(1, 8000)
AMOUNTS = ("fee", "spread", "slippage", "unmatched")
COUNTS = ("fills", "missing", "unassigned", "slip_known", "slip_missing")


def initialize(db):
    """A rebuildable change index; triggers never interpret receipt JSON."""
    db.execute("CREATE TABLE IF NOT EXISTS pair_cost_revision (id INTEGER PRIMARY KEY, revision INTEGER NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS pair_cost_changes (batch_key TEXT PRIMARY KEY, revision INTEGER NOT NULL)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_pair_cost_changes ON pair_cost_changes(revision,batch_key)")
    for action, references in (("INSERT", ("NEW",)), ("UPDATE", ("OLD", "NEW")), ("DELETE", ("OLD",))):
        condition = " OR ".join(f"substr({ref}.key,1,11)='pair_batch:'" for ref in references)
        if action == "UPDATE":
            condition = f"({condition}) AND (OLD.key!=NEW.key OR OLD.data!=NEW.data)"
        statements = " ".join(
            f"INSERT INTO pair_cost_changes SELECT {ref}.key,(SELECT revision FROM pair_cost_revision WHERE id=1) "
            f"WHERE substr({ref}.key,1,11)='pair_batch:' "
            "ON CONFLICT(batch_key) DO UPDATE SET revision=excluded.revision;" for ref in references)
        db.execute(f"CREATE TRIGGER IF NOT EXISTS pair_cost_{action.lower()} AFTER {action} ON kv WHEN {condition} BEGIN "
                   "INSERT INTO pair_cost_revision VALUES (1,1) ON CONFLICT(id) DO UPDATE SET revision=revision+1; "
                   + statements + " END")


def _stamp(value):
    return value if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 253402300799 else None


def _receipt_stamp(row):
    value = row.get("updateTime", row.get("time"))
    return _stamp(value / 1000) if type(value) in (int, float) and math.isfinite(value) else None


def _windows(now):
    day = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    week = day - timedelta(days=day.weekday())
    return {"daily": (day.timestamp(), (day + timedelta(days=1)).timestamp(), day.strftime("%Y-%m-%d")),
            "weekly": (week.timestamp(), (week + timedelta(days=7)).timestamp(), week.strftime("%Y-%m-%d") + " UTC 周")}


def _empty():
    return {**dict.fromkeys(AMOUNTS, Fraction(0)), **dict.fromkeys(COUNTS, 0)}


def _overlaps(batch, start, end, now):
    created, finished = _stamp(batch.get("created_at")), _stamp(batch.get("finished_at"))
    if created is not None and created >= min(end, now + 0.000001):
        return False
    # A late corrected receipt may be newer than the old local completion
    # clock. Never discard that evidence based only on finished_at.
    stamps = [finished] if finished is not None else []
    for field in ("legs", "repairs"):
        for leg in batch.get(field, []) if isinstance(batch.get(field, []), list) else []:
            if isinstance(leg, dict) and isinstance(leg.get("receipt"), dict):
                stamp = _receipt_stamp(leg["receipt"])
                if stamp is not None:
                    stamps.append(stamp)
    return finished is None or max(stamps, default=now) >= start


def _owner(batch, pair, *, current=False):
    """An account tuple is evidence of a gap, never evidence of group ownership."""
    identities = batch.get("identities")
    bound = isinstance(identities, dict) and all(
        isinstance(identities.get(side), dict)
        and identities[side].get("account_id") == pair[side + "_account_id"]
        and identities[side].get("side") == side.upper() for side in ("long", "short"))
    quality = batch.get("execution_quality")
    marker = batch.get("pair_id")
    quality_marker = quality.get("pair_id") if isinstance(quality, dict) else None
    if marker is not None:
        if marker != pair["id"]:
            return "other"
        if quality_marker is not None and quality_marker != marker:
            return "unassigned"
        return "owned" if bound else "unassigned"
    if quality_marker is not None:
        if quality_marker != pair["id"]:
            return "other"
        valid = (quality.get("scope") == "pair" and quality.get("intent_id") == "pair:" + str(batch.get("id"))
                 and quality.get("symbol") == batch.get("symbol") and quality.get("phase") == batch.get("phase"))
        return "owned" if bound and valid else "unassigned"
    if current and bound:
        return "owned"
    return "unassigned" if bound else "other"


def _receipt(leg, batch, seen):
    if not isinstance(leg, dict) or leg.get("key") not in ("long", "short"):
        raise TradingError("无效的循环成交方向")
    order, row = leg.get("order"), leg.get("receipt")
    if not isinstance(order, dict) or order.get("symbol") != batch["symbol"] \
            or order.get("positionSide") != leg["key"].upper() or order.get("side") not in ("BUY", "SELL"):
        raise TradingError("循环委托与组方向不一致")
    positive(order.get("quantity"))
    cid = order.get("newClientOrderId")
    if not isinstance(cid, str) or not cid or cid in seen:
        raise TradingError("循环委托标识缺失或重复")
    seen.add(cid)
    Executor.validate_receipt(order, row)
    quantity = Fraction(positive(row["executedQty"], True))
    if not quantity:
        return order, row, quantity, Fraction(0)
    quotes = [Fraction(positive(row[field])) for field in ("cumQuote", "cumQuoteQty") if field in row]
    if len(quotes) == 2 and quotes[0] != quotes[1]:
        raise TradingError("成交金额记录不一致")
    amount = quotes[0] if quotes else quantity * Fraction(positive(row["avgPrice"]))
    return order, row, quantity, amount


def _window_cost(batch, start, end, now):
    result = _empty()
    created = _stamp(batch.get("created_at"))
    legs, repairs = batch.get("legs"), batch.get("repairs", [])
    if not isinstance(legs, list) or len(legs) != 2 or not isinstance(repairs, list):
        result["missing"] = 1
        return result
    amounts = {side: [Fraction(0), Fraction(0)] for side in ("BUY", "SELL")}
    originals = deepcopy(amounts)
    seen = set()
    for index, leg in enumerate(legs + repairs):
        try:
            order, row, quantity, amount = _receipt(leg, batch, seen)
        except (TradingError, KeyError, TypeError, ValueError, OverflowError):
            result["missing"] += 1
            continue
        if not quantity:
            if row["status"] not in TERMINAL:
                result["missing"] += 1
            continue
        stamp = _receipt_stamp(row)
        if stamp is not None and not start <= stamp < end:
            # This receipt is outside the window; a batch spanning its start
            # remains uncertain if earlier fills could have occurred inside it.
            if created is None or created < end and stamp >= end:
                result["unassigned"] += 1
            continue
        if (created is None or stamp is None or stamp > now or stamp < created - 1
                or not start <= min(created, stamp) < end):
            result["unassigned"] += 1
            continue
        if row["status"] not in TERMINAL:
            result["missing"] += 1
        result["fills"] += 1
        result["fee"] += amount * FEE_RATE
        for totals in (amounts, originals) if index < len(legs) else (amounts,):
            totals[order["side"]][0] += quantity
            totals[order["side"]][1] += amount
        if index >= len(legs):
            # The original two-order quote is not a repair-order quote.
            result["slip_missing"] += 1
    buy_qty, buy_amount = amounts["BUY"]
    sell_qty, sell_amount = amounts["SELL"]
    matched = min(buy_qty, sell_qty)
    buy_price = buy_amount / buy_qty if buy_qty else Fraction(0)
    sell_price = sell_amount / sell_qty if sell_qty else Fraction(0)
    result["spread"] = matched * (buy_price - sell_price)
    result["unmatched"] = (buy_qty - matched) * buy_price + (sell_qty - matched) * sell_price
    original_qty = min(originals["BUY"][0], originals["SELL"][0])
    if original_qty:
        try:
            quality = batch.get("execution_quality") or {}
            estimate = quality["final_estimate"]
            if (quality.get("scope") != "pair" or quality.get("symbol") != batch["symbol"]
                    or quality.get("phase") != batch.get("phase")
                    or quality.get("intent_id") != "pair:" + batch["id"] or estimate.get("status") != "available"
                    or Fraction(positive(estimate.get("quantity"))) != Fraction(positive(batch.get("quantity")))):
                raise TradingError("缺少本批最终规划价格")
            expected = Fraction(positive(estimate["buy_vwap"])) - Fraction(positive(estimate["sell_vwap"]))
            actual = originals["BUY"][1] / originals["BUY"][0] - originals["SELL"][1] / originals["SELL"][0]
            result["slippage"] = original_qty * (actual - expected)
            result["slip_known"] = 1
        except (TradingError, KeyError, TypeError, ValueError, OverflowError):
            result["slip_missing"] += 1
    if result["unmatched"] or result["missing"] or result["unassigned"]:
        result["slip_missing"] += 1
    return result


def _contribution(raw, key, pair, windows, now, *, current=False):
    batch = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(batch, dict) or batch.get("kind") != "cycle" or batch.get("symbol") != pair["symbol"]:
        return None
    owner = _owner(batch, pair, current=current)
    if owner == "other":
        return None
    result = {}
    for name, (start, end, _) in windows.items():
        if not _overlaps(batch, start, end, now):
            continue
        if owner == "unassigned" or key != PREFIX + str(batch.get("id")):
            result[name] = {**_empty(), "unassigned": 1, "slip_missing": 1}
        else:
            result[name] = _window_cost(batch, start, end, now)
    return result or None


class HistoryCache:
    """Exact snapshot revisions plus incremental batch deltas, bounded by period."""
    def __init__(self, limit=64):
        self.entries = OrderedDict()
        self.lock = threading.Lock()
        self.limit = limit

    def read(self, db, pair, windows, now):
        row = db.execute("SELECT revision FROM pair_cost_revision WHERE id=1").fetchone()
        revision = row[0] if row else 0
        key = tuple(pair[name] for name in ("id", "long_account_id", "short_account_id", "symbol"))
        period = tuple(windows[name][0] for name in ("daily", "weekly"))
        with self.lock:
            previous = self.entries.get(key)
            reusable = (previous is not None and previous["period"] == period
                        and previous["revision"] <= revision and previous["since"] <= now < previous["until"])
            if reusable and previous["revision"] == revision:
                self.entries.move_to_end(key)
                return deepcopy(previous["totals"])
            if reusable:
                rows = db.execute("SELECT c.batch_key,k.data FROM pair_cost_changes c LEFT JOIN kv k ON k.key=c.batch_key "
                                  "WHERE c.revision>? AND c.revision<=?", (previous["revision"], revision)).fetchall()
            else:
                rows = db.execute("SELECT key,data FROM kv WHERE key>=? AND key<?", (PREFIX, "pair_batch;")).fetchall()
            # Finish all fallible reads/parsing before changing a shared entry.
            changes = []
            for batch_key, raw in rows:
                batch = json.loads(raw) if raw is not None else None
                contribution = _contribution(batch, batch_key, pair, windows, now) if batch is not None else None
                future = []
                if isinstance(batch, dict):
                    future.extend(_stamp(batch.get(field)) for field in ("created_at", "finished_at"))
                    for field in ("legs", "repairs"):
                        for leg in batch.get(field, []) if isinstance(batch.get(field, []), list) else []:
                            if isinstance(leg, dict) and isinstance(leg.get("receipt"), dict):
                                future.append(_receipt_stamp(leg["receipt"]))
                expiry = min((value for value in future if value is not None and value > now), default=math.inf)
                changes.append((batch_key, contribution, expiry))
            entry = previous if reusable else {"period": period, "since": now, "batches": {},
                                               "future": {}, "totals": {name: _empty() for name in windows}}
            for batch_key, contribution, expiry in changes:
                old = entry["batches"].pop(batch_key, None)
                entry["future"].pop(batch_key, None)
                if math.isfinite(expiry):
                    entry["future"][batch_key] = expiry
                for sign, values in ((-1, old), (1, contribution)):
                    for name, record in (values or {}).items():
                        for field in (*AMOUNTS, *COUNTS):
                            entry["totals"][name][field] += sign * record[field]
                if contribution:
                    entry["batches"][batch_key] = contribution
            entry["revision"] = revision
            entry["since"] = now
            entry["until"] = min(windows["daily"][1], min(entry["future"].values(), default=math.inf))
            # A still-open older WAL reader may report its old snapshot, but
            # cannot replace newer shared evidence used by another request.
            if (previous is None or revision > previous["revision"]
                    or revision == previous["revision"] and now >= previous["since"]):
                self.entries[key] = entry
                self.entries.move_to_end(key)
                while len(self.entries) > self.limit:
                    self.entries.popitem(last=False)
            return deepcopy(entry["totals"])


def read(reader, pair, runtime, *, now=None):
    now = time.time() if now is None else now
    if _stamp(now) is None:
        raise TradingError("费用报表时间无效")
    windows = _windows(now)
    with reader.connect() as db:
        totals = reader._pair_cost_cache.read(db, pair, windows, now)
        pending = runtime.get("pending") if isinstance(runtime, dict) else None
        if isinstance(pending, dict) and isinstance(pending.get("id"), str):
            key = PREFIX + pending["id"]
            # Finalized receipts take precedence if both representations exist.
            if db.execute("SELECT 1 FROM kv WHERE key=?", (key,)).fetchone() is None:
                values = _contribution(pending, key, pair, windows, now, current=True)
                for name, record in (values or {}).items():
                    for field in (*AMOUNTS, *COUNTS):
                        totals[name][field] += record[field]
    result = {"as_of": now, "timezone": "UTC", "fee_rate_percent": "0.0125"}
    for name, (start, end, label) in windows.items():
        values = totals[name]
        complete = not (values["missing"] or values["unassigned"] or values["unmatched"])
        slip_complete = complete and not values["slip_missing"]
        result[name] = {"start": start, "end": end, "label": label,
            "estimated_fee": number(values["fee"]), "spread_cost": number(values["spread"]),
            "slippage_cost": number(values["slippage"]) if values["slip_known"] or slip_complete else None,
            "total_cost": number(values["fee"] + values["spread"]), "complete": complete,
            "slippage_complete": slip_complete, "fill_count": values["fills"],
            "unmatched_notional": number(values["unmatched"]), "missing_count": values["missing"],
            "unassigned_count": values["unassigned"]}
    return result
