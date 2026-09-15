"""Exact reporting costs from actual cycle fills; no trading side effects."""
from collections import deque
from fractions import Fraction
import math

from .cycle_volume import account_identifier, identifier, utc_day
from .models import SYMBOLS, TradingError, decimal_value, positive


TAKER_RATE = Fraction(1, 8000)


def _wire(value):
    # Fees may have more decimal places than the admitted fill values. Preserve
    # the exact reporting result without reapplying order-input scale limits.
    return format(decimal_value(value, exact=True), "f")


def _timestamp(value):
    if type(value) not in (int, float) or not 0 <= value <= 253402300799 or not math.isfinite(value):
        raise TradingError("循环成本统计时间必须为有效时间戳")
    return float(value)


def _fill(row):
    if not isinstance(row, dict):
        raise TradingError("循环成本成交记录无效")
    result = {"account_id": account_identifier(row.get("account_id")),
              "intent_id": identifier(row.get("intent_id"), "循环成本批次标识"),
              "trade_id": identifier(row.get("trade_id"), "循环成本成交标识"),
              "symbol": row.get("symbol"), "side": row.get("side"),
              "executed_at": _timestamp(row.get("executed_at"))}
    if result["symbol"] not in SYMBOLS or result["side"] not in ("BUY", "SELL"):
        raise TradingError("循环成本成交品种或方向无效")
    for name in ("quantity", "price", "notional"):
        if not isinstance(row.get(name), str):
            raise TradingError("循环成本成交价量必须为十进制字符串")
        result[name] = Fraction(positive(row[name]))
    if result["quantity"] * result["price"] != result["notional"]:
        raise TradingError("循环成本成交金额必须等于数量乘成交价格")
    return result


def _summary(rows):
    fee, spread, unmatched = Fraction(0), Fraction(0), Fraction(0)
    unmatched_count = 0
    for row in rows:
        fee += row["fee"]
        spread += row["spread"]
        if row["remaining"]:
            unmatched += row["remaining"] * row["price"]
            unmatched_count += 1
    return {"taker_rate": _wire(TAKER_RATE), "taker_rate_percent": _wire(100 * TAKER_RATE),
            "taker_fee": _wire(fee), "spread_cost": _wire(spread), "total_cost": _wire(fee + spread),
            "unmatched_notional": _wire(unmatched), "unmatched_fill_count": unmatched_count,
            "complete": unmatched_count == 0}


def calculate_cycle_costs(fills, now, *, symbol=None):
    """Report one account's costs as of now using complete related intents.

    BUY/SELL quantities pair FIFO within account, intent and symbol. A match's
    signed spread belongs only to the later fill. Counterparts outside a report
    window can resolve matching, but only fills inside that window contribute
    fees, spread and still-unmatched notional. Unseen fills remain the caller's
    synchronization responsibility; this function only describes supplied data.
    """
    now = _timestamp(now)
    if symbol is not None and symbol not in SYMBOLS:
        raise TradingError("循环成本统计品种无效")
    date, day_start, _ = utc_day(now)
    if not isinstance(fills, (list, tuple)):
        raise TradingError("循环成本成交列表无效")
    unique, accounts = {}, set()
    for source in fills:
        row = _fill(source)
        if row["executed_at"] > now:
            continue
        accounts.add(row["account_id"])
        if len(accounts) > 1:
            raise TradingError("循环成本统计不能混合不同账户")
        key = row["account_id"], row["symbol"], row["trade_id"]
        previous = unique.get(key)
        if previous is not None and previous != row:
            raise TradingError("同一循环成交标识的成本价量或批次发生冲突")
        unique[key] = row
    rows = sorted(unique.values(), key=lambda row: (row["executed_at"], row["symbol"], row["trade_id"]))
    queues = {}
    for row in rows:
        row.update(fee=row["notional"] * TAKER_RATE, spread=Fraction(0),
                   matched=Fraction(0), remaining=row["quantity"])
        key = row["account_id"], row["intent_id"], row["symbol"]
        sides = queues.setdefault(key, {"BUY": deque(), "SELL": deque()})
        opposite = sides["SELL" if row["side"] == "BUY" else "BUY"]
        while row["remaining"] and opposite:
            earlier = opposite[0]
            quantity = min(row["remaining"], earlier["remaining"])
            buy, sell = (row, earlier) if row["side"] == "BUY" else (earlier, row)
            row["spread"] += (buy["price"] - sell["price"]) * quantity
            row["matched"] += quantity
            earlier["matched"] += quantity
            row["remaining"] -= quantity
            earlier["remaining"] -= quantity
            if not earlier["remaining"]:
                opposite.popleft()
        if row["remaining"]:
            sides[row["side"]].append(row)
    summary_rows = rows if symbol is None else [row for row in rows if row["symbol"] == symbol]
    daily = {**_summary(row for row in summary_rows if day_start <= row["executed_at"] <= now), "utc_date": date}
    rolling = {**_summary(row for row in summary_rows if now - 86400 < row["executed_at"] <= now),
               "window_start": now - 86400, "window_end": now}
    trades = [{"symbol": row["symbol"], "trade_id": row["trade_id"],
               "taker_fee": _wire(row["fee"]), "spread_cost": _wire(row["spread"]),
               "total_cost": _wire(row["fee"] + row["spread"]),
               "matched_quantity": _wire(row["matched"]), "unmatched_quantity": _wire(row["remaining"]),
               "cost_complete": not row["remaining"]} for row in rows]
    return {"daily": daily, "rolling": rolling, "trades": trades}
