"""Depth snapshots and fixed-notional display sweeps.

Display and execution may share immutable, validated order-book snapshots.
Execution applies its stricter freshness checks; display aggregates are never
order inputs.
"""
from bisect import bisect_left
from dataclasses import dataclass, field
from functools import cached_property
from fractions import Fraction
import time
from typing import Callable

from .models import TradingError, decimal_value, positive, wire


DEPTH_NOTIONALS = (10000, 50000)
DEPTH_LIMIT = 1000
DEPTH_WEIGHT = 20
DEPTH_POLL_INTERVAL = 1
DEPTH_RESYNC_INTERVAL = 30
DEPTH_MAX_AGE = 15


@dataclass(frozen=True, init=False)
class DepthSweep:
    """Immutable exact quantity/notional prefixes, shared by snapshot readers."""

    levels: tuple
    _quantities: tuple
    _notionals: tuple
    quantity: Fraction
    notional: Fraction

    def __init__(self, levels, *, bids):
        accepted, quantities, notionals = [], [], []
        quantity_sum = notional_sum = Fraction(0)
        previous = None
        if not isinstance(levels, (list, tuple)) or not 0 < len(levels) <= 1000:
            raise TradingError("迁移深度不足或档位无效")
        for row in levels:
            if not isinstance(row, (tuple, list)) or len(row) != 2:
                raise TradingError("迁移深度档位无效")
            price, quantity = row
            price = price if isinstance(price, Fraction) else Fraction(positive(price))
            quantity = quantity if isinstance(quantity, Fraction) else Fraction(positive(quantity, True))
            if price <= 0 or quantity < 0:
                raise TradingError("迁移深度价格或数量无效")
            if previous is not None and (price >= previous if bids else price <= previous):
                raise TradingError("迁移深度档位顺序无效")
            previous = price
            if quantity:
                accepted.append((price, quantity))
                quantity_sum += quantity
                notional_sum += price * quantity
                quantities.append(quantity_sum)
                notionals.append(notional_sum)
        if not accepted:
            raise TradingError("迁移深度不足")
        for name, value in (("levels", tuple(accepted)), ("_quantities", tuple(quantities)),
                            ("_notionals", tuple(notionals)), ("quantity", quantity_sum), ("notional", notional_sum)):
            object.__setattr__(self, name, value)

    def amount(self, quantity):
        if quantity < 0 or quantity > self.quantity:
            raise TradingError("迁移深度不足以成交全部数量")
        index = bisect_left(self._quantities, quantity)
        previous_quantity = self._quantities[index - 1] if index else Fraction(0)
        previous_notional = self._notionals[index - 1] if index else Fraction(0)
        return previous_notional + (quantity - previous_quantity) * self.levels[index][0]

    def quantity_for(self, amount):
        if amount <= 0:
            return Fraction(0)
        if amount >= self.notional:
            return self.quantity
        index = bisect_left(self._notionals, amount)
        previous_quantity = self._quantities[index - 1] if index else Fraction(0)
        previous_notional = self._notionals[index - 1] if index else Fraction(0)
        return previous_quantity + (amount - previous_notional) / self.levels[index][0]

    def last_price(self, quantity):
        if quantity > self.quantity:
            raise TradingError("迁移深度不足以成交全部数量")
        return self.levels[bisect_left(self._quantities, quantity)][0]


def _levels(rows, *, bids):
    if not isinstance(rows, list) or len(rows) > DEPTH_LIMIT:
        raise TradingError("深度档位数据无效")
    result, previous = [], None
    for row in rows:
        if not isinstance(row, list) or len(row) != 2:
            raise TradingError("深度档位格式无效")
        price, quantity = positive(row[0]), positive(row[1], True)
        if previous is not None and (price >= previous if bids else price <= previous):
            raise TradingError("深度档位顺序无效")
        previous = price
        if quantity:
            result.append((Fraction(price), Fraction(quantity)))
    return tuple(result)


def _average(levels, notional):
    remaining, quantity = Fraction(notional), Fraction(0)
    for price, available in levels:
        filled = min(available, remaining / price)
        quantity += filled
        remaining -= filled * price
        if remaining == 0:
            return Fraction(notional) / quantity
    # Never extrapolate missing liquidity or report a partial-fill average.
    return None


@dataclass(frozen=True)
class DepthSnapshot:
    bids: tuple
    asks: tuple
    timestamp: float
    monotonic_timestamp: float | None = None
    validity: Callable[[], bool] | None = field(default=None, compare=False, repr=False)

    def __post_init__(self):
        # Manual/test snapshots may contain lists. Detach those before any
        # reader derives cached prefixes from this immutable version.
        for name in ("bids", "asks"):
            rows = getattr(self, name)
            if isinstance(rows, (list, tuple)):
                object.__setattr__(self, name, tuple(tuple(row) if isinstance(row, list) else row for row in rows))

    @cached_property
    def sweeps(self):
        # Callers still check this snapshot's original age and revocation on
        # every use. Caching prefixes must never renew quote authority.
        return DepthSweep(self.bids, bids=True), DepthSweep(self.asks, bids=False)

    @classmethod
    def from_response(cls, data, *, requested_at, now=None):
        now = time.time() if now is None else now
        if not isinstance(data, dict) or "bids" not in data or "asks" not in data:
            raise TradingError("深度响应无效")
        event_time = float(positive(data.get("E"))) / 1000
        if not -1 <= now - event_time <= DEPTH_MAX_AGE:
            raise TradingError("深度报价已过期，等待更新")
        bids, asks = _levels(data["bids"], bids=True), _levels(data["asks"], bids=False)
        if bids and asks and bids[0][0] > asks[0][0]:
            raise TradingError("深度买卖价格交叉")
        # E is the snapshot output time. T may predate it on an unchanged book.
        snapshot = cls(bids, asks, min(event_time, requested_at))
        snapshot.require_fresh(now)
        return snapshot

    def age(self, now=None):
        age = (time.time() if now is None else now) - self.timestamp
        if self.monotonic_timestamp is not None:
            age = max(age, time.monotonic() - self.monotonic_timestamp)
        return age

    def require_fresh(self, now=None):
        if self.validity is not None and not self.validity():
            raise TradingError("深度连接或更新序号已失效，等待重新同步")
        if (time.time() if now is None else now) - self.timestamp < -1:
            raise TradingError("深度报价时间无效，等待更新")
        age = self.age(now)
        if not -1 <= age <= DEPTH_MAX_AGE:
            raise TradingError("深度报价已过期，等待更新")

    def display(self):
        spreads = {}
        for notional in DEPTH_NOTIONALS:
            buy, sell = _average(self.asks, notional), _average(self.bids, notional)
            spread = None if buy is None or sell is None else (buy - sell) / ((buy + sell) / 2)
            spreads[str(notional)] = {
                "status": "insufficient" if spread is None else "ok",
                "buy_average": None if buy is None else wire(decimal_value(buy)),
                "sell_average": None if sell is None else wire(decimal_value(sell)),
                "spread": None if spread is None else wire(decimal_value(spread)),
            }
        return {"timestamp": self.timestamp, "levels_limit": DEPTH_LIMIT, "spreads": spreads}
