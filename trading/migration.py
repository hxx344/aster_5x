"""Exact, side-effect-free XAU migration sizing from executable depth."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
import time

from .models import (HEDGE_TOLERANCE, MIN_BATCH_NOTIONAL, SYMBOLS, TIERS, TradingError, dec,
                     decimal_value, hedge_balanced, leverage_cap,
                     minimum_open_leverage, opening_margin_limit, positive, wire)


SOURCE_SYMBOL = "XAUUSD1"
TARGET_SYMBOLS = ("SPCXUSD1", "CLUSD1")
MIGRATION_DEPTH_MAX_AGE = 3
DEFAULT_MIGRATION = {"enabled": False, "spread_limit_bp": "5",
                     "batch_notional": "1000", "notional_tolerance": "0.05"}
SIDES = ("LONG", "SHORT")


def validate_migration(config=None):
    if config is None:
        config = {}
    if not isinstance(config, dict) or set(config) - set(DEFAULT_MIGRATION):
        raise TradingError("迁移配置字段无效")
    result = {**DEFAULT_MIGRATION, **config}
    if type(result["enabled"]) is not bool:
        raise TradingError("迁移开关必须为布尔值")
    for key in ("spread_limit_bp", "batch_notional", "notional_tolerance"):
        if not isinstance(result[key], str):
            raise TradingError("迁移数值设置必须为十进制字符串")
        result[key] = wire(dec(result[key]))
    if not 0 < dec(result["spread_limit_bp"]) <= 100:
        raise TradingError("迁移价差上限必须大于 0 且不超过 100 bp")
    if not MIN_BATCH_NOTIONAL <= dec(result["batch_notional"]) <= 1000000:
        raise TradingError("迁移每批每边金额必须为 500 至 1000000 USD1")
    if not 0 <= dec(result["notional_tolerance"]) <= dec("0.5"):
        raise TradingError("迁移金额误差必须在 0% 至 50% 之间")
    return result


def migration_symbols(account):
    symbols = list(account["policy"]["symbols"])
    if validate_migration(account.get("migration"))["enabled"]:
        symbols = list(dict.fromkeys([*symbols, *SYMBOLS]))
    return symbols


@dataclass(frozen=True)
class MigrationPlan:
    source_symbol: str
    target_symbol: str
    source_quantities: dict
    target_qty: Decimal
    source_leverage: int
    target_leverage: int
    spread: Decimal
    source_notionals: dict
    target_notionals: dict
    source_mark: Decimal = dec(0)
    target_mark: Decimal = dec(0)
    projected_ratio: Decimal = dec(0)
    cost_budget: Decimal = dec(0)
    notional_tolerance: Decimal = dec("0.05")
    spread_limit: Decimal = dec("0.0005")
    is_tail: bool = False

    @property
    def spread_exact(self):
        buy, sell = (Fraction(self.target_notionals[s]) for s in SIDES)
        return (buy - sell) / ((buy + sell) / 2)


def _floor(value, step):
    return (value // step) * step


def _ceil(value, step):
    return -((-value) // step) * step


class _Sweep:
    """Piecewise exact quantity/notional conversions; no liquidity extrapolation."""

    def __init__(self, levels, *, bids):
        self.levels = []
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
                self.levels.append((price, quantity))
        if not self.levels:
            raise TradingError("迁移深度不足")
        self.quantity = sum((q for _, q in self.levels), Fraction(0))
        self.notional = sum((p * q for p, q in self.levels), Fraction(0))

    def amount(self, quantity):
        remaining, amount = quantity, Fraction(0)
        if quantity < 0 or quantity > self.quantity:
            raise TradingError("迁移深度不足以成交全部数量")
        for price, available in self.levels:
            filled = min(available, remaining)
            amount += filled * price
            remaining -= filled
            if not remaining:
                return amount
        return amount

    def quantity_for(self, amount):
        remaining, quantity = max(Fraction(0), amount), Fraction(0)
        for price, available in self.levels:
            filled = min(available, remaining / price)
            quantity += filled
            remaining -= filled * price
            if not remaining:
                break
        return quantity

    def last_price(self, quantity):
        for price, available in self.levels:
            quantity -= available
            if quantity <= 0:
                return price
        raise TradingError("迁移深度不足以成交全部数量")


def _depth(depth, now):
    depth.require_fresh(now)
    if not -1 <= now - depth.timestamp <= MIGRATION_DEPTH_MAX_AGE:
        raise TradingError("迁移交易深度已过期，等待 3 秒内的新快照")
    bids, asks = _Sweep(depth.bids, bids=True), _Sweep(depth.asks, bids=False)
    if bids.levels[0][0] > asks.levels[0][0]:
        raise TradingError("迁移深度买卖价格交叉")
    return bids, asks


def _paired_quantity_for(bids, asks, amount):
    """Invert the paired VWAP midpoint without approximating its price levels."""
    i = j = 0
    bid_left, ask_left = bids.levels[0][1], asks.levels[0][1]
    remaining, quantity = max(Fraction(0), amount), Fraction(0)
    while i < len(bids.levels) and j < len(asks.levels):
        price = (bids.levels[i][0] + asks.levels[j][0]) / 2
        available = min(bid_left, ask_left)
        filled = min(available, remaining / price)
        quantity += filled
        remaining -= filled * price
        if not remaining:
            break
        bid_left -= filled
        ask_left -= filled
        if not bid_left:
            i += 1
            if i < len(bids.levels):
                bid_left = bids.levels[i][1]
        if not ask_left:
            j += 1
            if j < len(asks.levels):
                ask_left = asks.levels[j][1]
    return quantity


def _progress(progress):
    progress = {} if progress is None else progress
    if not isinstance(progress, dict):
        raise TradingError("迁移累计进度无效")
    delta, migrated = {}, {}
    for side in SIDES:
        deltas = progress.get("cumulative_notional_delta", {})
        totals = progress.get("migrated_notional", {})
        if not isinstance(deltas, dict) or not isinstance(totals, dict):
            raise TradingError("迁移累计金额无效")
        delta[side] = Fraction(dec(deltas.get(side, "0")))
        migrated[side] = Fraction(positive(totals.get(side, "0"), True))
    return delta, migrated


def plan_migration(account, snapshot, source_book, target_book, source_depth,
                   target_depth, source_rule, target_rule, capacities,
                   origin_leverage, progress=None):
    """Maximize a quantized batch without spending margin from unfilled closes.

    Amount errors use each direction's execution notional. Source reductions can
    differ to retain the existing hedge and to clear both actual tail quantities.
    Unlike ordinary additions, migration tails have no application-level $500
    floor; every order still satisfies the exchange's own filters.
    """
    config = validate_migration(account.get("migration"))
    if not config["enabled"]:
        raise TradingError("XAU 仓位迁移未开启")
    if source_rule.symbol != SOURCE_SYMBOL or target_rule.symbol not in TARGET_SYMBOLS:
        raise TradingError("迁移仅支持 XAUUSD1 到 SPCXUSD1 或 CLUSD1")
    if source_rule.margin_asset != "USD1" or target_rule.margin_asset != "USD1":
        raise TradingError("迁移仅允许 USD1 保证金市场")
    now = time.time()
    snapshot.require_modes(migration_symbols(account))
    source_pair = snapshot.require_ready(SOURCE_SYMBOL, now)
    target_pair = snapshot.require_ready(target_rule.symbol, now)
    source_book.require_fresh(now)
    target_book.require_fresh(now)
    source_bids, source_asks = _depth(source_depth, now)
    target_bids, target_asks = _depth(target_depth, now)
    if not hedge_balanced(*[p.qty for p in source_pair]) or not hedge_balanced(*[p.qty for p in target_pair]):
        raise TradingError("迁移前已有多空数量差超过 0.1%，等待核对")
    if not source_pair[0].qty or not source_pair[1].qty:
        raise TradingError("XAU 已无可迁移的双向仓位")
    if type(origin_leverage) is not int or not 1 <= origin_leverage <= 125:
        raise TradingError("迁移原始杠杆无效")
    source_leverage, current_target = source_pair[0].leverage, target_pair[0].leverage
    minimum = max(origin_leverage, source_leverage, current_target,
                  minimum_open_leverage(account["policy"]))
    leverages = [v for v in TIERS if v >= minimum]
    if not leverages:
        raise TradingError("没有不低于源仓和目标现有杠杆的受支持迁移档位")
    if not isinstance(capacities, dict):
        raise TradingError("迁移目标额度无效")
    caps = {v: Fraction(positive(capacities.get(str(v), capacities.get(v, "0")), True)) for v in TIERS}
    if caps[5] <= 0:
        raise TradingError("目标没有 5x 杠杆额度，禁止迁移")
    source_symbol, target_symbol = source_rule.symbol, target_rule.symbol
    if not snapshot.brackets.get(target_symbol) or any(snapshot.fees.get(s) is None for s in (source_symbol, target_symbol)):
        raise TradingError("迁移缺少账户风控档位或手续费率")
    fee_source, fee_target = (Fraction(positive(snapshot.fees[s], True)) for s in (source_symbol, target_symbol))
    batch, tolerance = Fraction(dec(config["batch_notional"])), Fraction(dec(config["notional_tolerance"]))
    spread_limit = Fraction(dec(config["spread_limit_bp"])) / 10000
    source_mark, target_mark = Fraction(source_book.mark), Fraction(target_book.mark)
    source_step, target_step = Fraction(positive(source_rule.step)), Fraction(positive(target_rule.step))
    source_min = _ceil(max(Fraction(positive(source_rule.min_qty)), Fraction(positive(source_rule.min_notional, True)) / source_mark), source_step)
    target_min = _ceil(max(Fraction(positive(target_rule.min_qty)), Fraction(positive(target_rule.min_notional, True)) / target_mark), target_step)
    source_qty = dict(zip(SIDES, (Fraction(p.qty) for p in source_pair)))
    normal_minimum = Fraction(MIN_BATCH_NOTIONAL)
    final_two = max(source_qty.values()) * source_mark <= 2 * normal_minimum
    # Below-floor batches are reserved for clearing a tail or splitting its last
    # two pieces. Small available capacity must not shrink a normal batch below $500.
    source_batch_min = {s: max(source_min, _ceil(
        min(normal_minimum / source_mark,
            max(Fraction(0), source_qty[s] - normal_minimum / source_mark))
        if final_two else normal_minimum / source_mark, source_step)) for s in SIDES}
    if not final_two:
        target_min = max(target_min, _ceil(normal_minimum / target_mark, target_step))
    source_sweeps = {"LONG": source_bids, "SHORT": source_asks}
    target_sweeps = {"LONG": target_asks, "SHORT": target_bids}
    source_max = {side: _floor(min(source_qty[side], Fraction(positive(source_rule.max_qty)),
                                      source_sweeps[side].quantity, batch / source_mark,
                                      source_sweeps[side].quantity_for(batch)), source_step) for side in SIDES}
    deltas, migrated = _progress(progress)
    tail = max(source_qty.values()) * source_mark <= batch
    prefix = "迁移尾仓" if tail else "迁移批次"
    if any(source_max[side] < source_min for side in SIDES):
        raise TradingError(prefix + "不足以满足交易所最小数量、金额或深度")
    target_max = min(Fraction(positive(target_rule.max_qty)), batch / target_mark,
                     target_bids.quantity_for(batch), target_asks.quantity_for(batch))
    for side in SIDES:
        source_amount = source_sweeps[side].amount(source_max[side])
        target_max = min(target_max, target_sweeps[side].quantity_for(source_amount * (1 + tolerance)))
    if all(source_qty[s] == source_max[s] for s in SIDES):
        # A tail's error allowance absorbs rounding; it is not a reason to
        # deliberately open tolerance-percent more holdings after XAU is empty.
        desired_midpoint = sum((source_sweeps[s].amount(source_qty[s]) - deltas[s] for s in SIDES), Fraction(0)) / 2
        target_max = min(target_max, _ceil(_paired_quantity_for(target_bids, target_asks, desired_midpoint), target_step))
    target_max = _floor(target_max, target_step)
    if target_max < target_min:
        raise TradingError(prefix + "无法在允许金额误差内满足目标最小委托")
    h = Fraction(HEDGE_TOLERANCE)

    def source_ranges(target_amounts, upper_amounts=None):
        ranges = {}
        for side in SIDES:
            sweep = source_sweeps[side]
            required = target_amounts[side] / (1 + tolerance)
            upper = (upper_amounts or target_amounts)[side] / (1 - tolerance)
            if required > sweep.notional:
                return None
            low = _ceil(max(source_batch_min[side], sweep.quantity_for(required)), source_step)
            high = _floor(min(source_max[side], sweep.quantity_for(upper)), source_step)
            if low > high:
                return None
            choices = []
            # Never create an exchange-untradeable residual just to maximize a batch.
            normal_high = min(high, _floor(source_qty[side] - source_min, source_step))
            if low <= normal_high:
                choices.append((low, normal_high))
            if low <= source_qty[side] <= high and _floor(source_qty[side], source_step) == source_qty[side]:
                choices.append((source_qty[side], source_qty[side]))
            if not choices:
                return None
            ranges[side] = choices
        return ranges

    def choose_source(target_amounts):
        ranges = source_ranges(target_amounts)
        if ranges is None:
            return None
        desired = {s: source_sweeps[s].quantity_for(target_amounts[s] + deltas[s]) for s in SIDES}
        options = []
        for start_l, end_l in ranges["LONG"]:
            for low_s, high_s in ranges["SHORT"]:
                # Intersect the rectangle of allowed closes with the residual hedge band.
                low_l = max(start_l, _ceil(source_qty["LONG"] - (source_qty["SHORT"] - low_s) / (1 - h), source_step))
                high_l = min(end_l, _floor(source_qty["LONG"] - (source_qty["SHORT"] - high_s) * (1 - h), source_step))
                if low_l > high_l:
                    continue
                candidates_l = {low_l, high_l, max(low_l, min(high_l, _floor(desired["LONG"], source_step))),
                                max(low_l, min(high_l, _ceil(desired["LONG"], source_step)))}
                for qty_l in candidates_l:
                    residual_l = source_qty["LONG"] - qty_l
                    minimum_s = max(low_s, _ceil(source_qty["SHORT"] - residual_l / (1 - h), source_step))
                    maximum_s = min(high_s, _floor(source_qty["SHORT"] - residual_l * (1 - h), source_step))
                    if minimum_s > maximum_s:
                        continue
                    candidates_s = {minimum_s, maximum_s,
                                    max(minimum_s, min(maximum_s, _floor(desired["SHORT"], source_step))),
                                    max(minimum_s, min(maximum_s, _ceil(desired["SHORT"], source_step)))}
                    for qty_s in candidates_s:
                        quantities = {"LONG": qty_l, "SHORT": qty_s}
                        amounts = {s: source_sweeps[s].amount(quantities[s]) for s in SIDES}
                        if any(abs(target_amounts[s] - amounts[s]) > tolerance * amounts[s] for s in SIDES):
                            continue
                        normalized_error = sum((abs(deltas[s] + target_amounts[s] - amounts[s]) /
                                                (migrated[s] + amounts[s]) for s in SIDES), Fraction(0))
                        options.append((normalized_error,
                                        sum((abs(target_amounts[s] - amounts[s]) for s in SIDES), Fraction(0)),
                                        -qty_l - qty_s, qty_l, qty_s, quantities, amounts))
        if not options:
            return []
        return [o[-2:] for o in sorted(options, key=lambda o: o[:5])]

    target_existing = sum((Fraction(p.qty) for p in target_pair), Fraction(0))
    source_existing_high = max(source_mark, source_asks.levels[0][0], *[Fraction(p.mark) for p in source_pair])
    target_existing_mark = max(target_mark, *[Fraction(p.mark) for p in target_pair])
    other_margin = sum((p.occupied_margin_exact for p in snapshot.positions
                        if p.symbol not in (source_symbol, target_symbol)), Fraction(0))
    source_occupied = sum(source_qty.values(), Fraction(0)) * source_existing_high / source_leverage
    existing_loss = Fraction(0)
    for pair, mark in ((source_pair, source_mark), (target_pair, target_mark)):
        pnl_change = sum((Fraction(p.qty) * (mark - Fraction(p.mark)) * (1 if p.side == "LONG" else -1)
                          for p in pair), Fraction(0))
        existing_loss += max(Fraction(0), -pnl_change)

    def source_cost(quantities, amounts):
        return (sum(amounts.values(), Fraction(0)) * fee_source
                + max(Fraction(0), quantities["LONG"] * source_mark - amounts["LONG"])
                + max(Fraction(0), amounts["SHORT"] - quantities["SHORT"] * source_mark))

    first_failure = prefix + "无法同时满足金额误差、数量步长及对冲约束"
    attempted_tier = False
    for leverage in leverages:
        if caps[leverage] <= 0:
            if not attempted_tier:
                first_failure = f"目标 {leverage}x 没有新增额度，禁止降低迁移杠杆"
            continue
        attempted_tier = True
        cap = Fraction(leverage_cap(snapshot.brackets[target_symbol], leverage))
        limit = Fraction(opening_margin_limit(account["policy"], leverage))

        def resources(quantity, source_quantities, source_amounts):
            amounts = {s: target_sweeps[s].amount(quantity) for s in SIDES}
            high_price = max(target_existing_mark, target_asks.last_price(quantity))
            new_gross = 2 * quantity * high_price
            existing_gross = target_existing * high_price
            if new_gross > caps[5]:
                return None, "目标 5x 额度不足以容纳本批多空总金额"
            needed = new_gross + (existing_gross if leverage > current_target else 0)
            if needed > caps[leverage] or (leverage > current_target and caps[leverage] <= existing_gross):
                return None, f"目标 {leverage}x 额度不足以容纳现有仓位与迁移批次"
            if existing_gross + new_gross > cap:
                return None, "迁移超过目标账户风控档位容量"
            spread = (amounts["LONG"] - amounts["SHORT"]) / ((amounts["LONG"] + amounts["SHORT"]) / 2)
            if spread > spread_limit:
                return None, "目标本批交易深度价差超过迁移阈值"
            cost = (source_cost(source_quantities, source_amounts)
                    + sum(amounts.values(), Fraction(0)) * fee_target
                    + max(Fraction(0), amounts["LONG"] - quantity * target_mark)
                    + max(Fraction(0), quantity * target_mark - amounts["SHORT"]))
            baseline_occupied = other_margin + source_occupied + existing_gross / leverage
            added_margin = new_gross / leverage
            equity = Fraction(snapshot.equity) - existing_loss - cost
            available = Fraction(snapshot.available) - existing_loss - (baseline_occupied - snapshot.occupied_margin_exact)
            if available < added_margin + cost:
                return None, "可用余额不足以先开目标仓位并支付四腿成本"
            occupied = baseline_occupied + added_margin
            if equity <= 0 or occupied > limit * equity:
                return None, "迁移临时保证金占用超过风险上限，不能预支未平 XAU 的保证金"
            return (amounts, spread, cost, occupied / equity), None

        nodes = 0
        stack = [(int(target_min / target_step), int(target_max / target_step))]
        while stack:
            low, high = stack.pop()
            if low > high:
                continue
            nodes += 1
            if nodes > 8192:
                raise TradingError(prefix + "数量步长组合过多，需调整批次金额或误差后重新规划")
            low_qty, high_qty = low * target_step, high * target_step
            low_amounts = {s: target_sweeps[s].amount(low_qty) for s in SIDES}
            high_amounts = {s: target_sweeps[s].amount(high_qty) for s in SIDES}
            ranges = source_ranges(low_amounts, high_amounts)
            if ranges is None:
                continue
            # Monotone lower bounds prune a complete integer interval. Unlike a
            # plain feasibility binary search, holes between quantity grids cannot
            # hide a larger valid batch or cause a false "insufficient" result.
            lower_source = {s: min(r[0] for r in ranges[s]) for s in SIDES}
            lower_amounts = {s: source_sweeps[s].amount(lower_source[s]) for s in SIDES}
            _, failure = resources(low_qty, lower_source, lower_amounts)
            if failure:
                first_failure = failure
                continue
            choices = choose_source(high_amounts) or []
            for quantities, amounts in choices:
                facts, failure = resources(high_qty, quantities, amounts)
                if facts is not None:
                    target_amounts, spread, cost, ratio = facts
                    return MigrationPlan(source_symbol, target_symbol,
                        {s: decimal_value(quantities[s], exact=True) for s in SIDES}, decimal_value(high_qty, exact=True),
                        source_leverage, leverage, decimal_value(spread),
                        {s: decimal_value(amounts[s], exact=True) for s in SIDES},
                        {s: decimal_value(target_amounts[s], exact=True) for s in SIDES},
                        decimal_value(source_mark, exact=True), decimal_value(target_mark, exact=True),
                        decimal_value(ratio), decimal_value(cost, exact=True), decimal_value(tolerance, exact=True),
                        decimal_value(spread_limit, exact=True),
                        all(quantities[s] == source_qty[s] for s in SIDES) or
                        (final_two and all((source_qty[s] - quantities[s]) * source_mark <= normal_minimum for s in SIDES)))
                first_failure = failure
            if low == high:
                continue
            middle = (low + high) // 2
            stack.append((low, middle))
            stack.append((middle + 1, high - 1))  # The failed high endpoint was already checked.
    raise TradingError(first_failure if "尾仓" in first_failure or not tail else "迁移尾仓：" + first_failure)
