"""Independent, exact planning for configured same-symbol hedge cycles."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
import math
import time

from .cycle_diagnostics import CycleConditionError, diagnostic_error, diagnostic_number
from .migration import _Sweep, migration_symbols
from .models import (SYMBOLS, TradingError, cycle_margin_limit, dec, decimal_value, leverage_cap,
                     positive, wire)


SIDES = ("LONG", "SHORT")
CYCLE_DEPTH_MAX_AGE = 3
CYCLE_POSITION_MISMATCH = "循环实际多空数量与记录不一致，已停止自动交易，需核对"
DEFAULT_CYCLE = {
    "enabled": False, "symbol": "XAUUSD1", "leverage": 2,
    "spread_notional": "10000", "spread_limit_bp": "0.1",
    "min_notional": "0", "max_notional": "10000",
    "capacity_multiplier": "1",
    "notional_scope": "per_side", "hold_seconds": 60, "daily_volume_limit": "0",
}


class CyclePositionError(TradingError):
    """Actual holdings no longer match the cycle's exclusive tracked position."""


def cycle_recovery_available(account, progress, pending=None):
    return bool(not account.get("enabled") and account.get("cycle", {}).get("enabled")
                and account.get("pause_reason") == CYCLE_POSITION_MISMATCH and not pending
                and isinstance(progress, dict) and progress.get("phase") in ("holding", "waiting_close"))


class DailyVolumeLimitError(CycleConditionError):
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
    capacity_notional: Decimal | None = None


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
    for key in ("spread_notional", "spread_limit_bp", "min_notional", "max_notional", "daily_volume_limit", "capacity_multiplier"):
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
    if not 1 <= dec(result["capacity_multiplier"]) <= 100:
        raise TradingError("循环额度倍数必须为 1 至 100，可使用小数")
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
    """Track only the increment above the frozen original position."""
    config, phase, tracked = _state(account, progress)
    try:
        pair = snapshot.pair(config["symbol"])
    except TradingError as exc:
        raise CyclePositionError("循环多空仓位数据无效：" + str(exc)) from exc
    if phase == "waiting_open":
        return pair
    baseline = cycle_baseline(progress)
    if any(Fraction(position.qty) != Fraction(baseline[side]) + Fraction(tracked[side])
           for side, position in zip(SIDES, pair)):
        raise CyclePositionError(CYCLE_POSITION_MISMATCH)
    if phase != "waiting_open" and any(position.leverage != config["leverage"] for position in pair):
        raise CyclePositionError("循环持仓期间实际杠杆发生变化，需核对后恢复")
    return pair


def cycle_baseline(progress):
    # Pre-upgrade active cycles started flat; retain their zero baseline solely
    # for closing/recovery. New openings always persist an explicit baseline.
    values = (progress or {}).get("baseline", dict.fromkeys(SIDES, "0"))
    if not isinstance(values, dict) or set(values) != set(SIDES):
        raise CyclePositionError("循环原始持仓记录无效，需核对后恢复")
    try:
        return {side: positive(values[side], True) for side in SIDES}
    except TradingError as exc:
        raise CyclePositionError("循环原始持仓记录无效，需核对后恢复") from exc


def cycle_config(account, snapshot, progress=None):
    config, phase, _ = _state(account, progress)
    if phase == "waiting_open":
        pair = validate_cycle_positions(account, snapshot, progress)
        config = {**config, "leverage": pair[0].leverage}
    return config


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


def _check(code, label, actual, required, unit, passed):
    return {"code": code, "label": label, "actual": actual, "required": required,
            "unit": unit, "passed": passed}


def _minimum_diagnostic(title, *, config, now, bids, asks, step, minimum_qty,
                        maximum_qty, mark, exchange_minimum, cap, fee, margin_limit,
                        occupied, available, equity, minimum, maximum, limit_bp,
                        daily_budget, rolling_budget, executable_qty,
                        error_type=CycleConditionError):
    """Explain the minimum required order using original inputs, not a search probe.

    This runs only after rejection. Unknown prices stay unknown when the supplied
    book cannot cover the minimum; no notional-to-quantity extrapolation is used.
    """
    number = diagnostic_number
    base = max(step, minimum_qty, exchange_minimum / mark)
    base = -(-base // step) * step
    depth_steps = int(min(bids.quantity, asks.quantity) // step)

    def amount_at(qty):
        buy, sell = asks.amount(qty), bids.amount(qty)
        return min(buy, sell) if config["notional_scope"] == "per_side" else buy + sell

    depth_amount = amount_at(depth_steps * step) if depth_steps else Fraction(0)
    target_known = depth_amount >= minimum
    target = base
    if minimum and target_known:
        low, high = 0, depth_steps
        while low < high:
            mid = (low + high) // 2
            if amount_at(mid * step) >= minimum:
                high = mid
            else:
                low = mid + 1
        target = max(base, low * step)

    target_text = number(target) if target_known else "≥ " + number(base)
    checks = [
        _check("exchange_min_qty", "交易所最小数量", target_text, "≥ " + number(minimum_qty), "",
               target >= minimum_qty if target_known else None),
        _check("exchange_min_notional", "交易所最小名义额（按标记价）", number(target * mark) if target_known else None,
               "≥ " + number(exchange_minimum), "USD1", target * mark >= exchange_minimum if target_known else None),
        _check("quantity_step", "交易所数量步长", target_text, number(step) + " 的正整数倍", "",
               target > 0 and target % step == 0 if target_known else None),
        _check("exchange_max_qty", "交易所最大数量", target_text, "≤ " + number(maximum_qty), "",
               target <= maximum_qty if target_known else False if base > maximum_qty else None),
        _check("depth_buy_quantity", "买入侧完整深度数量", number(asks.quantity), "≥ " + number(target), "",
               asks.quantity >= target if target_known else False if asks.quantity < base else None),
        _check("depth_sell_quantity", "卖出侧完整深度数量", number(bids.quantity), "≥ " + number(target), "",
               bids.quantity >= target if target_known else False if bids.quantity < base else None),
    ]
    priced = target_known and target <= min(bids.quantity, asks.quantity)
    buy = sell = gross = cost = added_margin = projected_equity = ratio = reserve = None
    scope = "每边" if config["notional_scope"] == "per_side" else "多空合计"
    if priced:
        buy, sell = asks.amount(target), bids.amount(target)
        gross = 2 * target * max(mark, asks.last_price(target))
        cost = ((buy + sell) * fee + max(Fraction(0), buy - target * mark)
                + max(Fraction(0), target * mark - sell))
        added_margin = gross / config["leverage"]
        projected_equity = equity - cost
        ratio = (occupied + added_margin) / projected_equity if projected_equity > 0 else None
        reserve = 2 * (buy + sell)
    maximum_value = (max(buy, sell) if scope == "每边" else buy + sell) if priced else None
    minimum_value = (min(buy, sell) if scope == "每边" else buy + sell) if priced else None
    checks.extend([
        _check("configured_min_notional", f"配置最小金额（{scope}）",
               number(minimum_value) if priced else number(depth_amount) if not target_known else None,
               "≥ " + number(minimum), "USD1", minimum_value >= minimum if priced else False if not target_known else None),
        _check("configured_max_notional", f"配置最大金额（{scope}）", number(maximum_value) if priced else None,
               "≤ " + number(maximum), "USD1", maximum_value <= maximum if priced else None),
        _check("order_spread", "最小委托实际深度价差", number(_spread(buy, sell)) if priced else None,
               "≤ " + number(limit_bp), "bp", _spread(buy, sell) <= limit_bp if priced else None),
        _check("leverage_cap", f"{config['leverage']}x 新增多空合计名义额（额度已扣原仓）", number(gross) if priced else None,
               "≤ " + number(cap), "USD1", gross <= cap if priced else None),
        _check("projected_equity", "扣除手续费与不利价差后的权益", number(projected_equity) if priced else None,
               "> 0", "USD1", projected_equity > 0 if priced else None),
        _check("available_margin", "可用余额", number(available),
               "≥ " + number(added_margin + cost) if priced else None, "USD1",
               available >= added_margin + cost if priced else None),
        _check("projected_margin_ratio", "全账户预计保证金占比", number(ratio, 100) if ratio is not None else None,
               "≤ " + number(margin_limit, 100), "%", ratio <= margin_limit if ratio is not None else None),
    ])
    for code, label, budget in (("daily_volume", "UTC 日剩余成交额度", daily_budget),
                                 ("rolling_volume", "滚动 24 小时剩余成交额度", rolling_budget)):
        if budget is not None:
            checks.append(_check(code, label, number(budget), "≥ " + number(reserve) if priced else None,
                                 "USD1", budget >= reserve if priced else None))
    context = [
        {"label": "最小要求数量", "value": target_text},
        {"label": "当前可执行数量", "value": number(executable_qty)},
        {"label": "标记价格", "value": number(mark), "unit": "USD1"},
        {"label": "当前全账户保证金", "value": number(occupied), "unit": "USD1"},
        {"label": "当前权益", "value": number(equity), "unit": "USD1"},
        {"label": "风控手续费预留率", "value": number(fee, 100), "unit": "%"},
    ]
    if priced:
        context.extend([
            {"label": "最小要求买入均价", "value": number(buy / target), "unit": "USD1"},
            {"label": "最小要求卖出均价", "value": number(sell / target), "unit": "USD1"},
            {"label": "最小要求新增保证金", "value": number(added_margin), "unit": "USD1"},
            {"label": "预留手续费与不利价差", "value": number(cost), "unit": "USD1"},
            {"label": "可用余额缺口", "value": number(max(Fraction(0), added_margin + cost - available)), "unit": "USD1"},
            {"label": "全账户保证金超额", "value": number(max(Fraction(0), occupied + added_margin - margin_limit * projected_equity)), "unit": "USD1"},
        ])
    note = ("按满足交易所最小委托及配置最小金额的最低步长数量逐项检查；不代表将按该数量下单。"
            if priced else "完整深度不足以计算全部最小要求；未取得的价格、费用及风险检查不判定通过。"
            + ("配置最小金额的当前值为现有双边深度按步长可达金额；最小要求数量仅为交易所要求的下界。" if not target_known else ""))
    return diagnostic_error("cycle_minimum_order", title, symbol=config["symbol"], phase="open", checked_at=now,
                            checks=checks, context=context, note=note, error_type=error_type)


def plan_cycle(account, snapshot, book, depth, rule, progress=None, now=None, *, daily_remaining=None,
               rolling_remaining=None, market_capacity=None):
    """Maximize one exact pair, or close precisely the recorded completed pair.

    The configured reference amount sweeps each side separately. Its VWAP
    spread and the actual equal-quantity order's spread must both pass in bp.
    Opening margin uses gross exposure at the conservative marginal price;
    reductions remain possible when account equity or opening capacity falls.
    """
    config, phase, tracked = _state(account, progress)
    config = cycle_config(account, snapshot, progress)
    if not validate_cycle(account.get("cycle"))["enabled"] or not config["enabled"]:
        raise TradingError("独立多空循环未开启")
    if rule.symbol != config["symbol"] or rule.margin_asset != "USD1":
        raise TradingError("循环交易规则必须对应所选 USD1 保证金品种")
    started_at = time.monotonic()
    now = time.time() if now is None else now
    pair = validate_cycle_positions(account, snapshot, progress)
    # All cycle orders are tracked MARKET batches. The durable intent/recovery
    # gate owns in-flight orders; an external open-order inventory is not read.
    snapshot.require_fresh(now)
    snapshot.require_modes([config["symbol"]])
    if not snapshot.can_trade:
        raise TradingError("账户没有交易权限")
    if phase == "waiting_open":
        snapshot.ratio
    else:
        # require_ready also requires positive equity; that opening requirement
        # must not prevent a fully tracked position from being reduced.
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
    diagnostic_phase = "open" if phase == "waiting_open" else "close"
    if min(bids.notional, asks.notional) < reference:
        raise diagnostic_error("reference_depth", "循环价差采样金额的双边完整深度不足",
            symbol=rule.symbol, phase=diagnostic_phase, checked_at=now,
            checks=[_check("reference_buy_depth", "买入侧完整深度金额", diagnostic_number(asks.notional),
                           "≥ " + diagnostic_number(reference), "USD1", asks.notional >= reference),
                    _check("reference_sell_depth", "卖出侧完整深度金额", diagnostic_number(bids.notional),
                           "≥ " + diagnostic_number(reference), "USD1", bids.notional >= reference),
                    _check("reference_spread", "采样深度价差", None, "≤ " + diagnostic_number(limit_bp), "bp", None)],
            note="采样深度不足，未外推成交均价或判定价差通过。")
    reference_buy = reference / asks.quantity_for(reference)
    reference_sell = reference / bids.quantity_for(reference)
    reference_spread = _spread(reference_buy, reference_sell)
    if reference_spread > limit_bp:
        raise diagnostic_error("reference_spread", "循环采样深度价差超过配置阈值（bp）",
            symbol=rule.symbol, phase=diagnostic_phase, checked_at=now,
            checks=[_check("reference_spread", "采样深度价差", diagnostic_number(reference_spread),
                           "≤ " + diagnostic_number(limit_bp), "bp", reference_spread <= limit_bp)],
            context=[{"label": "采样每边金额", "value": diagnostic_number(reference), "unit": "USD1"},
                     {"label": "买入均价", "value": diagnostic_number(reference_buy), "unit": "USD1"},
                     {"label": "卖出均价", "value": diagnostic_number(reference_sell), "unit": "USD1"},
                     {"label": "价差超出", "value": diagnostic_number(reference_spread - limit_bp), "unit": "bp"}])
    step = Fraction(positive(rule.step))
    minimum_qty, maximum_qty = Fraction(positive(rule.min_qty)), Fraction(positive(rule.max_qty))
    mark = Fraction(positive(book.mark))
    exchange_minimum = Fraction(positive(rule.min_notional, True))

    if phase != "waiting_open":
        qty = Fraction(tracked["LONG"])
        if qty % step or not minimum_qty <= qty <= maximum_qty:
            raise diagnostic_error("close_quantity", "循环全部平仓数量不满足交易所数量步长或限额",
                symbol=rule.symbol, phase="close", checked_at=now,
                checks=[_check("close_min_qty", "全部平仓数量", diagnostic_number(qty), "≥ " + diagnostic_number(minimum_qty), "", qty >= minimum_qty),
                        _check("close_max_qty", "全部平仓数量", diagnostic_number(qty), "≤ " + diagnostic_number(maximum_qty), "", qty <= maximum_qty),
                        _check("close_step", "全部平仓数量步长", diagnostic_number(qty), diagnostic_number(step) + " 的整数倍", "", qty % step == 0)])
        if qty > min(bids.quantity, asks.quantity):
            raise diagnostic_error("close_depth", "循环全部平仓所需双边深度不足", symbol=rule.symbol, phase="close", checked_at=now,
                checks=[_check("close_buy_depth", "买入侧完整深度数量", diagnostic_number(asks.quantity), "≥ " + diagnostic_number(qty), "", asks.quantity >= qty),
                        _check("close_sell_depth", "卖出侧完整深度数量", diagnostic_number(bids.quantity), "≥ " + diagnostic_number(qty), "", bids.quantity >= qty)],
                note="深度不足，未外推全部平仓的成交均价。")
        long_amount, short_amount = bids.amount(qty), asks.amount(qty)
        if _spread(short_amount, long_amount) > limit_bp:
            raise diagnostic_error("close_spread", "循环实际平仓数量的深度价差超过配置阈值（bp）",
                symbol=rule.symbol, phase="close", checked_at=now,
                checks=[_check("close_spread", "全部平仓深度价差", diagnostic_number(_spread(short_amount, long_amount)),
                               "≤ " + diagnostic_number(limit_bp), "bp", _spread(short_amount, long_amount) <= limit_bp)],
                context=[{"label": "每边平仓数量", "value": diagnostic_number(qty)},
                         {"label": "买入均价", "value": diagnostic_number(short_amount / qty), "unit": "USD1"},
                         {"label": "卖出均价", "value": diagnostic_number(long_amount / qty), "unit": "USD1"}])
        require_search_time()
        return CyclePlan("close", rule.symbol, decimal_value(qty, exact=True), config["leverage"],
                         decimal_value(long_amount, exact=True), decimal_value(short_amount, exact=True),
                         decimal_value(reference_spread))

    current_cap = snapshot.current_leverage_caps.get(rule.symbol)
    fee = snapshot.fees.get(rule.symbol)
    if current_cap is not None:
        # A current-leverage limit cannot authorize a different leverage.
        if (not isinstance(current_cap, (tuple, list)) or len(current_cap) != 2
                or type(current_cap[0]) is not int or current_cap[0] != config["leverage"]):
            raise TradingError("循环账户额度与当前杠杆不匹配")
        cap = Fraction(positive(current_cap[1], True))
    else:
        brackets = snapshot.brackets.get(rule.symbol)
        if not brackets:
            raise TradingError("循环开仓缺少当前杠杆额度")
        cap = Fraction(positive(leverage_cap(brackets, config["leverage"])))
    # maxNotional is a total position ceiling, not additional room. Both held
    # legs consume it before a new pair can be added.
    cap = max(Fraction(0), cap - sum((Fraction(p.qty) * max(Fraction(p.mark), mark) for p in pair), Fraction(0)))
    if market_capacity is not None:
        cap = min(cap, Fraction(positive(market_capacity, True)))
    if fee is None:
        raise TradingError("循环开仓缺少手续费率")
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
        error_type = CycleConditionError
        if volume_budget is not None and not minimum_error(search(quota=False), quota=False):
            if rolling_budget is not None and (daily_budget is None or rolling_budget < daily_budget):
                error_type = RollingVolumeLimitError
                error = "滚动 24 小时剩余额度不足以完成下一轮开平仓，等待历史成交移出窗口后自动重试"
            else:
                error_type = DailyVolumeLimitError
                error = "今日剩余额度不足以完成下一轮开平仓，待 UTC 日额度和滚动 24 小时额度均满足后自动恢复"
        raise _minimum_diagnostic(error, config=config, now=now, bids=bids, asks=asks, step=step,
            minimum_qty=minimum_qty, maximum_qty=maximum_qty, mark=mark, exchange_minimum=exchange_minimum,
            cap=cap, fee=fee, margin_limit=margin_limit, occupied=occupied, available=available, equity=equity,
            minimum=minimum, maximum=maximum, limit_bp=limit_bp, daily_budget=daily_budget,
            rolling_budget=rolling_budget, executable_qty=qty, error_type=error_type)
    buy, sell, projected_ratio = resources(qty)
    require_search_time()
    return CyclePlan("open", rule.symbol, decimal_value(qty, exact=True), config["leverage"],
                     decimal_value(buy, exact=True), decimal_value(sell, exact=True),
                     decimal_value(reference_spread), decimal_value(projected_ratio),
                     decimal_value(2 * qty * max(mark, asks.last_price(qty)), exact=True))
