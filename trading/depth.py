"""Display-only order-book sweeps; never used to size or execute orders."""
from dataclasses import dataclass
from fractions import Fraction
import time

from .models import TradingError, decimal_value, positive, wire


DEPTH_NOTIONALS = (10000, 50000)
DEPTH_LIMIT = 1000
DEPTH_WEIGHT = 20
DEPTH_POLL_INTERVAL = 10
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

    def require_fresh(self, now=None):
        age = (time.time() if now is None else now) - self.timestamp
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
