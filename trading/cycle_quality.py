"""Display-only observations of an original two-order cycle batch.

Wall timestamps are Unix seconds and elapsed times are milliseconds. Prices,
quantities and spreads are decimal strings (36 significant digits for ratios).
Monotonic clock readings and market snapshot objects are never persisted.
"""
from decimal import Context, Decimal, localcontext
from fractions import Fraction
import math
import time

from .cycle import _depth_sweeps
from .exchange import RequestNotSent
from .models import positive, wire


def timestamp(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def number(value):
    value = Fraction(value)
    with localcontext(Context(prec=36)):
        result = format(Decimal(value.numerator) / Decimal(value.denominator), "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def estimate(quantity, depth=None, checked_at=None):
    result = {"status": "unavailable", "sampled_at": None, "checked_at": timestamp(checked_at),
              "quantity": quantity, "buy_vwap": None, "sell_vwap": None, "spread_bp": None}
    if depth is None:
        return result
    try:
        result["sampled_at"] = timestamp(depth.timestamp)
        checked_at = result["checked_at"] if result["checked_at"] is not None else time.time()
        bids, asks = _depth_sweeps(depth, checked_at)
        qty = Fraction(positive(quantity))
        buy, sell = asks.amount(qty) / qty, bids.amount(qty) / qty
        result.update(status="available", checked_at=checked_at, buy_vwap=number(buy),
                      sell_vwap=number(sell), spread_bp=number((buy - sell) * 20000 / (buy + sell)))
    except Exception:
        # The original admission checks decide whether orders may be sent.
        # Failed display observations must not introduce a new trading gate.
        pass
    return result


def new_quality(intent, trigger=None):
    trigger = trigger if isinstance(trigger, dict) else {}
    quantity = wire(positive(intent["quantity"]))
    source = trigger.get("source")
    return {"version": 1, "intent_id": intent.get("id"), "symbol": intent["symbol"], "phase": intent["phase"],
            "quantity": quantity, "created_at": timestamp(intent.get("created_at")), "updated_at": time.time(),
            "trigger": {"source": source if source in ("bbo", "depth", "poll") else "unknown",
                        "received_at": timestamp(trigger.get("received_at"))},
            "trigger_estimate": estimate(quantity, trigger.get("depth"), trigger.get("checked_at")),
            "final_estimate": estimate(quantity),
            "timing": {"request_status": "unknown", "request_started_at": None, "response_received_at": None,
                       "trigger_to_request_ms": None, "final_check_to_request_ms": None,
                       "request_to_response_ms": None},
            "actual": actual(intent)}


def actual(intent):
    result = {"status": "unknown", "confirmed": False, "quantity": None, "buy_vwap": None,
              "sell_vwap": None, "spread_bp": None, "buy_quantity": None, "sell_quantity": None,
              "buy_status": None, "sell_status": None, "repairs_present": bool(intent.get("repairs"))}
    try:
        # Read original orders only. A repair can flatten exposure but cannot
        # turn an incomplete original pair into a fully executed pair.
        from .execution import Executor, TERMINAL
        orders = intent["orders"]
        if len(orders) != 2 or {order["side"] for order in orders} != {"BUY", "SELL"} \
                or {order["positionSide"] for order in orders} != {"LONG", "SHORT"}:
            return result
        qty = Fraction(positive(intent["quantity"]))
        rows = []
        for order in orders:
            side = order["side"].lower()
            if order["symbol"] != intent["symbol"] or Fraction(positive(order["quantity"])) != qty:
                return result
            row = intent.get("receipts", {}).get(order["newClientOrderId"])
            if row is not None:
                Executor.validate_receipt(order, row)
                result[side + "_quantity"] = wire(positive(row["executedQty"], True))
                result[side + "_status"] = row["status"]
            rows.append(row)
        if all(row is not None and row["status"] == "FILLED" for row in rows):
            prices = {order["side"]: Fraction(positive(row["avgPrice"])) for order, row in zip(orders, rows)}
            buy, sell = prices["BUY"], prices["SELL"]
            result.update(status="filled", confirmed=True, quantity=wire(positive(intent["quantity"])),
                          buy_vwap=number(buy), sell_vwap=number(sell),
                          spread_bp=number((buy - sell) * 20000 / (buy + sell)))
        elif any(row is not None and positive(row["executedQty"], True) for row in rows):
            result["status"] = "partial"
        elif all(row is not None and row.get("local_not_sent") for row in rows):
            result["status"] = "not_sent"
        elif all(row is not None and row["status"] in TERMINAL for row in rows):
            result["status"] = "rejected"
        elif any(row is not None and row["status"] not in TERMINAL for row in rows):
            result["status"] = "pending"
    except Exception:
        # Invalid or incomplete receipts provide no evidence of a full pair.
        result.update(status="unknown", confirmed=False, quantity=None, buy_vwap=None,
                      sell_vwap=None, spread_bp=None)
    return result


def elapsed(start, end):
    start, end = timestamp(start), timestamp(end)
    if start is None or end is None or end < start:
        return None
    value = (end - start) * 1000
    return value if math.isfinite(value) else None


class ObservedBroker:
    """Per-call adapter: never replace or modify a shared broker method."""

    def __init__(self, broker, quality, trigger_ticks=None, final_ticks=None):
        self._broker, self._quality = broker, quality
        self._trigger_ticks, self._final_ticks = trigger_ticks, final_ticks

    def __getattr__(self, name):
        return getattr(self._broker, name)

    def submit(self, orders):
        started = None
        try:
            started = time.monotonic()
            timing = self._quality["timing"]
            timing.update(request_started_at=time.time(), request_status="unknown",
                          trigger_to_request_ms=elapsed(self._trigger_ticks, started),
                          final_check_to_request_ms=elapsed(self._final_ticks, started))
        except Exception:
            pass
        try:
            result = self._broker.submit(orders)
        except Exception as exc:
            try:
                self._quality["timing"]["request_status"] = "not_sent" if isinstance(exc, RequestNotSent) else "failed"
            except Exception:
                pass
            raise
        else:
            try:
                finished = time.monotonic()
                self._quality["timing"].update(request_status="returned", response_received_at=time.time(),
                                               request_to_response_ms=elapsed(started, finished))
            except Exception:
                pass
            return result
