"""Depth snapshots and fixed-notional display sweeps.

Display and execution may share immutable, validated order-book snapshots.
Execution applies its stricter freshness checks; display aggregates are never
order inputs.
"""
from dataclasses import dataclass, field
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
