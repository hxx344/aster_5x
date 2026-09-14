"""Public-book hints for waking a cycle; never authorize an order."""
from fractions import Fraction
import math
import time

from .cycle import CYCLE_DEPTH_MAX_AGE, _depth_sweeps, _spread, _state
from .models import TradingError, dec, decimal_value, positive, wire


def cycle_signal_quote(account, progress, book, depth, rule, *, now):
    """Reject impossible public conditions before scheduling private reads.

    Opening checks only the smallest feasible public quantity. The account
    worker still calculates its actual quantity and repeats every risk check.
    Closing checks the complete tracked quantity with the frozen cycle config.
    """
    started = time.monotonic()
    if not account.get("enabled") or not account.get("cycle", {}).get("enabled") \
            or account.get("migration", {}).get("enabled"):
        return None
    config, phase, tracked = _state(account, progress)
    if not config["enabled"] or rule.symbol != config["symbol"] or rule.margin_asset != "USD1":
        return None
    progress = progress or {}
    if phase == "waiting_open":
        retry_at = progress.get("retry_at") or 0
        if type(retry_at) not in (int, float) or not math.isfinite(retry_at) or retry_at > now:
            return None
    else:
        opened_at = progress.get("opened_at")
        if type(opened_at) not in (int, float) or not math.isfinite(opened_at) \
                or not 0 < opened_at <= now or now < opened_at + config["hold_seconds"]:
            return None
    book.require_fresh(now)
    bids, asks = _depth_sweeps(depth, now)
    reference = Fraction(dec(config["spread_notional"]))
    limit = Fraction(dec(config["spread_limit_bp"]))
    if min(bids.notional, asks.notional) < reference:
        return None
    spread = _spread(reference / asks.quantity_for(reference), reference / bids.quantity_for(reference))
    if spread > limit:
        return None
    if phase == "waiting_open":
        step = Fraction(positive(rule.step))
        quantity = max(step, Fraction(positive(rule.min_qty)), Fraction(positive(rule.min_notional, True)) / Fraction(positive(book.mark)))
        quantity = -(-quantity // step) * step
        minimum = Fraction(dec(config["min_notional"]))
        maximum = Fraction(dec(config["max_notional"]))
        steps = int(min(bids.quantity, asks.quantity, Fraction(positive(rule.max_qty))) // step)
        def amount(qty):
            buy, sell = asks.amount(qty), bids.amount(qty)
            return min(buy, sell) if config["notional_scope"] == "per_side" else buy + sell
        if steps <= 0 or amount(steps * step) < minimum:
            return None
        if minimum:
            low, high = 1, steps
            while low < high:
                middle = (low + high) // 2
                if amount(middle * step) >= minimum:
                    high = middle
                else:
                    low = middle + 1
            quantity = max(quantity, low * step)
        if quantity > steps * step:
            return None
        buy, sell = asks.amount(quantity), bids.amount(quantity)
        upper = max(buy, sell) if config["notional_scope"] == "per_side" else buy + sell
        if upper > maximum:
            return None
    else:
        quantity = Fraction(tracked["LONG"])
        if quantity > min(bids.quantity, asks.quantity) or not rule.min_qty <= quantity <= rule.max_qty \
                or quantity % Fraction(rule.step):
            return None
        buy, sell = asks.amount(quantity), bids.amount(quantity)
    if _spread(buy, sell) > limit:
        return None
    depth.require_fresh(now)
    if depth.age(now) + max(0, time.monotonic() - started) > CYCLE_DEPTH_MAX_AGE:
        raise TradingError("循环行情预筛期间深度已过期")
    # The returned quantity is only the hint's public sizing basis; the caller
    # must never substitute it for plan_cycle's fresh account-aware result.
    return {"phase": "open" if phase == "waiting_open" else "close",
            "minimum_quantity": wire(quantity), "reference_spread_bp": wire(decimal_value(spread))}
