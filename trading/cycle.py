"""Independent, exact planning for configured same-symbol hedge cycles."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
import math
import time

from .migration import _Sweep, migration_symbols
from .models import (SYMBOLS, TradingError, cycle_margin_limit, dec, decimal_value, leverage_cap,
                     positive, wire)


SIDES = ("LONG", "SHORT")
CYCLE_DEPTH_MAX_AGE = 3
DEFAULT_CYCLE = {
    "enabled": False, "symbol": "XAUUSD1", "leverage": 2,
    "spread_notional": "10000", "spread_limit_bp": "0.1",
    "min_notional": "0", "max_notional": "10000",
    "notional_scope": "per_side", "hold_seconds": 60, "daily_volume_limit": "0",
}


class CyclePositionError(TradingError):
    """Actual holdings no longer match the cycle's exclusive tracked position."""


class DailyVolumeLimitError(TradingError):
    """A new cycle must wait for sufficient volume allowance."""


class RollingVolumeLimitError(DailyVolumeLimitError):
    """Recent fills must leave the rolling 24-hour window before reopening."""


@dataclass(frozen=True)
class CyclePlan:
    phase: str
    symbol: str
    qty: Decimal
    leverage: int
    long_notional: Decimal
    short_notional: Decimal
    spread_bp: Decimal
    projected_ratio: Decimal | None = None


def validate_cycle(config=None):
    if config is None:
        config = {}
    if not isinstance(config, dict) or set(config) - set(DEFAULT_CYCLE):
        raise TradingError("循环配置字段无效")
    result = {**DEFAULT_CYCLE, **config}
    if type(result["enabled"]) is not bool:
        raise TradingError("循环开关必须为布尔值")
    if result["symbol"] not in SYMBOLS:
        raise TradingError("循环品种仅支持 XAUUSD1、SPCXUSD1、CLUSD1")
    if type(result["leverage"]) is not int or not 1 <= result["leverage"] <= 125:
        raise TradingError("循环杠杆必须为 1 至 125 的整数")
    if type(result["hold_seconds"]) is not int or not 1 <= result["hold_seconds"] <= 604800:
        raise TradingError("循环持仓时间必须为 1 至 604800 秒的整数")
    if result["notional_scope"] not in ("per_side", "gross"):
        raise TradingError("循环金额口径必须为单边金额或多空合计金额")
    for key in ("spread_notional", "spread_limit_bp", "min_notional", "max_notional", "daily_volume_limit"):
        if not isinstance(result[key], str):
            raise TradingError("循环金额及价差必须为十进制字符串")
        result[key] = wire(dec(result[key]))
    if not 0 <= dec(result["spread_limit_bp"]) <= 100:
        raise TradingError("循环价差阈值必须为 0 至 100 bp")
    if not 0 < dec(result["spread_notional"]) <= 1000000:
        raise TradingError("循环价差采样金额必须大于 0 且不超过 1000000 USD1")
    minimum, maximum = (dec(result[key]) for key in ("min_notional", "max_notional"))
    if not 0 < maximum <= 1000000 or not 0 <= minimum <= maximum:
        raise TradingError("循环金额范围必须满足 0 ≤ 最小金额 ≤ 最大金额 ≤ 1000000，且最大金额大于 0")
    if not 0 <= dec(result["daily_volume_limit"]) <= 1000000000000:
        raise TradingError("循环每日成交量上限必须为 0 至 1000000000000 USD1，0 表示不限")
    return result


def cycle_symbols(account):
    config = validate_cycle(account.get("cycle"))
    return [config["symbol"]] if config["enabled"] else migration_symbols(account)


def _state(account, progress):
    if progress is None:
        progress = {}
    if not isinstance(progress, dict):
        raise CyclePositionError("循环进度无效，需核对后恢复")
    phase = progress.get("phase", "waiting_open")
    if phase not in ("waiting_open", "holding", "waiting_close"):
        raise CyclePositionError("循环阶段无效，需先核对未完成批次")
    config = validate_cycle(account.get("cycle"))
    if phase != "waiting_open":
        if not isinstance(progress.get("config"), dict):
            raise CyclePositionError("循环持仓缺少冻结配置，需核对后恢复")
        config = validate_cycle(progress["config"])
    quantities = progress.get("quantities", {})
    if not isinstance(quantities, dict) or set(quantities) - set(SIDES):
        raise CyclePositionError("循环记录的多空数量无效")
    try:
        tracked = {side: positive(quantities.get(side, "0"), True) for side in SIDES}
    except TradingError as exc:
        raise CyclePositionError("循环记录的多空数量无效") from exc
    if phase == "waiting_open":
        if any(tracked.values()):
            raise CyclePositionError("等待开仓阶段仍有循环数量记录，需核对后恢复")
    elif not tracked["LONG"] or tracked["LONG"] != tracked["SHORT"]:
        raise CyclePositionError("循环记录必须为数量完全相同的多空仓位")
    return config, phase, tracked


def validate_cycle_positions(account, snapshot, progress=None):
    """Never adopt an existing position or close quantity outside this run."""
    config, phase, tracked = _state(account, progress)
    try:
        pair = snapshot.pair(config["symbol"])
    except TradingError as exc:
        raise CyclePositionError("循环多空仓位数据无效：" + str(exc)) from exc
    if any(position.qty != tracked[side] for side, position in zip(SIDES, pair)):
        if phase == "waiting_open":
            raise CyclePositionError("循环品种已有仓位；仅允许从多空均为空仓开始")
        raise CyclePositionError("循环实际多空数量与记录不一致，已停止自动交易，需核对")
    if phase != "waiting_open" and any(position.leverage != config["leverage"] for position in pair):
        raise CyclePositionError("循环持仓期间实际杠杆发生变化，需核对后恢复")
    return pair


def _depth_sweeps(depth, now):
    depth.require_fresh(now)
    if not -1 <= depth.age(now) <= CYCLE_DEPTH_MAX_AGE:
        raise TradingError("循环交易深度已过期，等待 3 秒内的新快照")
    try:
        bids, asks = _Sweep(depth.bids, bids=True), _Sweep(depth.asks, bids=False)
    except TradingError as exc:
        raise TradingError(str(exc).replace("迁移", "循环交易")) from exc
    if bids.levels[0][0] > asks.levels[0][0]:
        raise TradingError("循环交易深度买卖价格交叉")
    return bids, asks


def _spread(buy, sell):
    return (buy - sell) * 20000 / (buy + sell)


def plan_cycle(account, snapshot, book, depth, rule, progress=None, now=None, *, daily_remaining=None,
               rolling_remaining=None):
    """Maximize one exact pair, or close precisely the recorded completed pair.

    The configured reference amount sweeps each side separately. Its VWAP
    spread and the actual equal-quantity order's spread must both pass in bp.
    Opening margin uses gross exposure at the conservative marginal price;
    reductions remain possible when account equity or opening capacity falls.
    """
    config, phase, tracked = _state(account, progress)
    if not validate_cycle(account.get("cycle"))["enabled"] or not config["enabled"]:
        raise TradingError("独立多空循环未开启")
    if rule.symbol != config["symbol"] or rule.margin_asset != "USD1":
        raise TradingError("循环交易规则必须对应所选 USD1 保证金品种")
    started_at = time.monotonic()
    now = time.time() if now is None else now
    pair = validate_cycle_positions(account, snapshot, progress)
    if snapshot.open_orders is None:
        raise TradingError("循环交易前必须查询账户未完成挂单")
    if phase == "waiting_open":
        snapshot.require_ready(config["symbol"], now)
    else:
        # require_ready also requires positive equity; that opening requirement
        # must not prevent a fully tracked position from being reduced.
        snapshot.require_fresh(now)
        snapshot.require_modes([config["symbol"]])
        if not snapshot.can_trade:
            raise TradingError("账户没有交易权限")
        if snapshot.open_orders:
            raise TradingError("账户存在未完成挂单，等待核对")
        opened_at = (progress or {}).get("opened_at")
        if (isinstance(opened_at, bool) or not isinstance(opened_at, (int, float))
                or not math.isfinite(opened_at) or not 0 < opened_at <= now):
            raise CyclePositionError("循环开仓完成时间无效，需核对后恢复")
        remaining = config["hold_seconds"] - (now - opened_at)
        if remaining > 0:
            raise TradingError(f"循环持仓计时中，剩余 {math.ceil(remaining)} 秒")
    if any(position.leverage != config["leverage"] for position in pair):
        raise TradingError(f"循环实际杠杆尚未达到配置的 {config['leverage']}x")
    book.require_fresh(now)
    bids, asks = _depth_sweeps(depth, now)
    deadline = started_at + CYCLE_DEPTH_MAX_AGE - depth.age(now)

    def require_search_time():
        if time.monotonic() > deadline:
            raise TradingError("循环交易深度已过期，等待新快照后重新计算")
        # A shared stream can invalidate an otherwise young immutable snapshot.
        if depth.validity is not None and not depth.validity():
            raise TradingError("循环交易深度已失效，等待重新同步")

    reference = Fraction(dec(config["spread_notional"]))
    limit_bp = Fraction(dec(config["spread_limit_bp"]))
    if min(bids.notional, asks.notional) < reference:
        raise TradingError("循环价差采样金额的双边完整深度不足")
    reference_spread = _spread(reference / asks.quantity_for(reference),
                               reference / bids.quantity_for(reference))
    if reference_spread > limit_bp:
        raise TradingError("循环采样深度价差超过配置阈值（bp）")
    step = Fraction(positive(rule.step))
    minimum_qty, maximum_qty = Fraction(positive(rule.min_qty)), Fraction(positive(rule.max_qty))
    mark = Fraction(positive(book.mark))
    exchange_minimum = Fraction(positive(rule.min_notional, True))

    if phase != "waiting_open":
        qty = Fraction(tracked["LONG"])
        if qty % step or not minimum_qty <= qty <= maximum_qty:
            raise TradingError("循环全部平仓数量不满足交易所数量步长或限额")
        if qty > min(bids.quantity, asks.quantity):
            raise TradingError("循环全部平仓所需双边深度不足")
        long_amount, short_amount = bids.amount(qty), asks.amount(qty)
        if _spread(short_amount, long_amount) > limit_bp:
            raise TradingError("循环实际平仓数量的深度价差超过配置阈值（bp）")
        require_search_time()
        return CyclePlan("close", rule.symbol, decimal_value(qty, exact=True), config["leverage"],
                         decimal_value(long_amount, exact=True), decimal_value(short_amount, exact=True),
                         decimal_value(reference_spread))

    brackets, fee = snapshot.brackets.get(rule.symbol), snapshot.fees.get(rule.symbol)
    if not brackets or fee is None:
        raise TradingError("循环开仓缺少账户风控档位或手续费率")
    cap = Fraction(positive(leverage_cap(brackets, config["leverage"])))
    fee = Fraction(positive(fee, True))
    margin_limit = Fraction(cycle_margin_limit(account["policy"]))
    if fee > 1 or margin_limit > 1:
        raise TradingError("循环开仓手续费率或账户保证金上限无效")
    occupied = snapshot.occupied_margin_exact
    available, equity = Fraction(snapshot.available), Fraction(snapshot.equity)
    minimum, maximum = (Fraction(dec(config[key])) for key in ("min_notional", "max_notional"))
    daily_budget = None if daily_remaining is None else Fraction(positive(daily_remaining, True))
    rolling_budget = None if rolling_remaining is None else Fraction(positive(rolling_remaining, True))
    budgets = [value for value in (daily_budget, rolling_budget) if value is not None]
    volume_budget = min(budgets) if budgets else None
    upper = min(maximum_qty, bids.quantity, asks.quantity)
    if config["notional_scope"] == "per_side":
        upper = min(upper, asks.quantity_for(maximum), bids.quantity_for(maximum))

    def resources(qty, *, quota=True):
        buy, sell = asks.amount(qty), bids.amount(qty)
        # Reserve both opening fills and their estimated closing fills. A later
        # price move or necessary repair may still consume more actual volume.
        if quota and volume_budget is not None and 2 * (buy + sell) > volume_budget:
            return None
        value = max(buy, sell) if config["notional_scope"] == "per_side" else buy + sell
        if value > maximum or _spread(buy, sell) > limit_bp:
            return None
        high_price = max(mark, asks.last_price(qty))
        gross = 2 * qty * high_price
        if gross > cap:
            return None
        cost = ((buy + sell) * fee + max(Fraction(0), buy - qty * mark)
                + max(Fraction(0), qty * mark - sell))
        added_margin = gross / config["leverage"]
        projected_equity = equity - cost
        total_occupied = occupied + added_margin
        if (projected_equity <= 0 or available < added_margin + cost
                or total_occupied > margin_limit * projected_equity):
            return None
        return buy, sell, total_occupied / projected_equity

    # Sorted books make notional, marginal-price margin, fees and spread
    # nondecreasing in quantity, so a logarithmic exact search is sufficient.
    def search(*, quota=True):
        low, high = 0, int(upper // step)
        while low < high:
            require_search_time()
            mid = (low + high + 1) // 2
            if resources(mid * step, quota=quota) is not None:
                low = mid
            else:
                high = mid - 1
        return low * step

    def minimum_error(qty, *, quota=True):
        if not qty or qty < minimum_qty or qty * mark < exchange_minimum:
            return "循环风险、余额或深度不足以满足交易所最小委托"
        buy, sell, _ = resources(qty, quota=quota)
        amount = min(buy, sell) if config["notional_scope"] == "per_side" else buy + sell
        return "循环可执行金额低于配置的最小金额" if amount < minimum else None

    qty = search()
    error = minimum_error(qty)
    if error:
        if volume_budget is not None and not minimum_error(search(quota=False), quota=False):
            if rolling_budget is not None and (daily_budget is None or rolling_budget < daily_budget):
                raise RollingVolumeLimitError("滚动 24 小时剩余额度不足以完成下一轮开平仓，等待历史成交移出窗口后自动重试")
            raise DailyVolumeLimitError("今日剩余额度不足以完成下一轮开平仓，待 UTC 日额度和滚动 24 小时额度均满足后自动恢复")
        raise TradingError(error)
    buy, sell, projected_ratio = resources(qty)
    require_search_time()
    return CyclePlan("open", rule.symbol, decimal_value(qty, exact=True), config["leverage"],
                     decimal_value(buy, exact=True), decimal_value(sell, exact=True),
                     decimal_value(reference_spread), decimal_value(projected_ratio))
