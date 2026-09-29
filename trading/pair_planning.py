"""Exact XAU plans with separate collateral budgets for the two subaccounts."""
from fractions import Fraction
import time

from .cycle import CyclePlan, plan_cycle
from .models import (AccountSnapshot, TradingError, MIN_BATCH_NOTIONAL, TIERS, dec,
                     decimal_value, leverage_cap, opening_margin_limit, cycle_margin_limit,
                     positive, wire)

SYMBOL = "XAUUSD1"
SIDES = (("long", "LONG"), ("short", "SHORT"))


class PairPositionError(TradingError):
    """An unowned or externally changed position cannot authorize a new order."""


class PairRecoveryConflict(TradingError):
    """A newer durable state must never be overwritten by a stale recovery."""


def positions(snapshots, *, equal_leverage=True):
    result = {}
    for key, side in SIDES:
        snapshot = snapshots[key]
        snapshot.require_fresh()
        snapshot.require_modes([SYMBOL])
        if not snapshot.can_trade or snapshot.open_orders:
            raise PairPositionError("子账户没有交易权限或仍有外部挂单")
        long, short = snapshot.pair(SYMBOL)
        selected, opposite = (long, short) if side == "LONG" else (short, long)
        if opposite.qty or any(p.qty and p.symbol != SYMBOL for p in snapshot.positions):
            raise PairPositionError(f"{key} 子账户存在非本组方向或非 XAU 仓位，请先核对")
        result[side] = selected
    if equal_leverage and result["LONG"].leverage != result["SHORT"].leverage:
        raise PairPositionError(f"两个子账户的 XAU 实际杠杆不一致（做多账户 {result['LONG'].leverage}x、做空账户 {result['SHORT'].leverage}x），停止新交易，请核对")
    return result


def require_quantities(snapshots, quantities, *, equal_leverage=True):
    pair = positions(snapshots, equal_leverage=equal_leverage)
    if any(pair[side].qty != positive(quantities[side], True) for _, side in SIDES):
        raise PairPositionError("实际仓位与配对组账本不一致，停止新交易并等待核对")
    return pair


def current_cap(snapshot, leverage):
    row = snapshot.current_leverage_caps.get(SYMBOL)
    if row is not None:
        if len(row) != 2 or type(row[0]) is not int or row[0] != leverage:
            raise TradingError("子账户可开额度与实际杠杆不匹配")
        return Fraction(positive(row[1], True))
    return Fraction(leverage_cap(snapshot.brackets.get(SYMBOL, []), leverage))


def combine(snapshots, *, opening=True):
    held = positions(snapshots)
    long, short = (snapshots[key] for key, _ in SIDES)
    return AccountSnapshot(
        long.equity + short.equity, long.maintenance + short.maintenance,
        long.available + short.available, long.wallet + short.wallet,
        long.unrealized + short.unrealized, [held["LONG"], held["SHORT"]], [],
        True, False, True, min(long.timestamp, short.timestamp),
        fees={SYMBOL: max(positive(s.fees.get(SYMBOL), True) for s in snapshots.values())} if opening else {},
        current_leverage_caps={SYMBOL: (held["LONG"].leverage, decimal_value(
            sum((current_cap(s, held["LONG"].leverage) for s in snapshots.values()), Fraction(0)), exact=True))} if opening else {},
    )


def resource_gate(pair, snapshots, book, *, cycling=False, daily_remaining=None):
    held = positions(snapshots)
    leverage = held["LONG"].leverage
    policy = pair["ordinary"]
    limit = Fraction(cycle_margin_limit(policy) if cycling else opening_margin_limit(policy, leverage))
    mark = Fraction(positive(book.mark))
    # Capture validated exact values once, rather than parsing them in each search step.
    budgets = []
    for key, side in SIDES:
        snap, pos = snapshots[key], held[side]
        fee = Fraction(positive(snap.fees.get(SYMBOL), True))
        if fee > 1:
            raise TradingError("子账户手续费率无效")
        old_qty, old_mark = Fraction(pos.qty), Fraction(pos.mark)
        pnl_change = old_qty * (mark - old_mark) * (1 if side == "LONG" else -1)
        loss = max(Fraction(0), -pnl_change)
        budget = None if daily_remaining is None else daily_remaining.get(key)
        budgets.append((side, snap, pos, fee, loss, current_cap(snap, leverage),
                        None if budget is None else Fraction(positive(budget, True))))

    def allowed(qty, buy, sell, high_price):
        for side, snap, pos, fee, loss, cap, daily in budgets:
            amount = buy if side == "LONG" else sell
            if daily is not None and 2 * amount > daily:
                return False
            high = max(high_price, Fraction(pos.mark))
            occupied_delta = max(Fraction(0), Fraction(pos.qty) * high / leverage - pos.occupied_margin_exact)
            if (Fraction(pos.qty) + qty) * high > cap:
                return False
            fill_loss = max(Fraction(0), buy - qty * mark) if side == "LONG" else max(Fraction(0), qty * mark - sell)
            cost = amount * fee + fill_loss
            equity = Fraction(snap.equity) - loss - cost
            additional = qty * high / leverage
            if (equity <= 0 or Fraction(snap.available) - loss < occupied_delta + additional + cost
                    or snap.occupied_margin_exact + occupied_delta + additional > limit * equity):
                return False
        return True
    return allowed


def ordinary_leverage_reason(pair, snapshots, book, capacities, held):
    """Describe upgrade blockers from the same inputs, without another API read."""
    old, policy = held["LONG"].leverage, pair["ordinary"]
    minimum = policy["min_open_leverage"]
    state = (f"两账户当前均为 {old}x，低于普通开仓最低 {minimum}x" if old < minimum else
             f"两账户当前均为 {old}x，不在普通开仓支持档位（5x、10x、20x）")
    targets = [tier for tier in TIERS if tier > old and tier >= minimum]
    if not targets:
        return state + "；停止新增，程序不会自动降杠杆，请暂停配对组后核对实际杠杆设置"
    threshold = Fraction(dec(policy["threshold"]))
    details = []
    for target in targets:
        if target not in capacities:
            detail = "缺少公开额度数据"
        elif Fraction(positive(capacities[target], True)) <= threshold:
            detail = f"公开额度未严格超过 {wire(threshold)} USD1"
        else:
            blocked = ["做多账户" if key == "long" else "做空账户" for key, side in SIDES
                       if Fraction(held[side].qty) * max(Fraction(held[side].mark), Fraction(book.mark))
                       > Fraction(leverage_cap(snapshots[key].brackets.get(SYMBOL, []), target))]
            detail = ("公开额度已满足，但" + "、".join(blocked) + "的现有持仓超过该档账户持仓上限"
                      if blocked else "公开额度及账户持仓上限已满足，可申请自动升档")
        details.append(f"{target}x：{detail}")
    return state + "；" + "；".join(details) + "。程序会自动选择满足条件的更高档位，两账户升档确认后再检查开仓条件"


def plan_ordinary(pair, snapshots, book, rule, capacities):
    held = positions(snapshots)
    leverage, policy = held["LONG"].leverage, pair["ordinary"]
    book.require_fresh()
    if leverage not in TIERS or leverage < policy["min_open_leverage"]:
        raise TradingError(ordinary_leverage_reason(pair, snapshots, book, capacities, held))
    if held["LONG"].qty != held["SHORT"].qty:
        raise PairPositionError("配对组已有数量不等，停止新增")
    if book.spread_exact > Fraction(dec(policy["spread_limit"])):
        raise TradingError(f"XAU 买一卖一价差超过普通开仓设置上限 {wire(Fraction(dec(policy['spread_limit'])) * 10000)} bp，等待价差回落")
    capacity = Fraction(positive(capacities.get(leverage), True))
    if capacity <= Fraction(dec(policy["threshold"])):
        raise TradingError(f"当前 {leverage}x 公开可用额度未严格超过普通开仓门槛 {wire(policy['threshold'])} USD1，等待公开额度恢复")
    bid, ask, mark = map(Fraction, (book.bid, book.ask, book.mark))
    high = max(ask, mark)
    upper = min(Fraction(rule.max_qty), Fraction(book.bid_qty), Fraction(book.ask_qty),
                Fraction(dec(policy["order_notional"])) / high, capacity / (2 * high))
    step = Fraction(positive(rule.step))
    gate = resource_gate(pair, snapshots, book)
    low, maximum = 0, int(upper // step)
    while low < maximum:
        mid = (low + maximum + 1) // 2
        qty = mid * step
        if gate(qty, qty * ask, qty * bid, high):
            low = mid
        else:
            maximum = mid - 1
    qty = low * step
    if qty < Fraction(rule.min_qty) or qty * mark < max(rule.min_notional, MIN_BATCH_NOTIONAL) or not qty:
        raise TradingError(f"按两账户各自余额、持仓上限、保证金占用、公开额度、盘口及配置的单批金额上限计算后，共同可开数量不足以满足最小下单量及按标记价计算的每边名义金额至少 {wire(max(rule.min_notional, MIN_BATCH_NOTIONAL))} USD1，暂不新增")
    return CyclePlan("open", SYMBOL, decimal_value(qty, exact=True), leverage,
                     decimal_value(qty * ask, exact=True), decimal_value(qty * bid, exact=True),
                     decimal_value(book.spread_exact * 10000), capacity_notional=decimal_value(2 * qty * high, exact=True))


def plan_paired_cycle(pair, snapshots, book, depth, rule, progress, *, capacity=None, daily_remaining=None, paused=False):
    config = {**pair["cycle"], "enabled": True}
    progress = dict(progress)
    if paused and progress.get("phase") in ("holding", "waiting_close"):
        # Pausing still reduces the recorded cycle increment, without waiting its timer.
        progress["config"] = {**progress["config"], "hold_seconds": 1}
        progress["opened_at"] = min(progress["opened_at"], time.time() - 2)
    account = {"id": pair["id"], "policy": pair["ordinary"], "cycle": config}
    opening = progress.get("phase", "waiting_open") == "waiting_open"
    snapshot = combine(snapshots, opening=opening)
    gate = resource_gate(pair, snapshots, book, cycling=True, daily_remaining=daily_remaining) if opening else None
    return plan_cycle(account, snapshot, book, depth, rule, progress,
                      market_capacity=capacity, opening_gate=gate)


def ordinary_upgrade(pair, snapshots, book, capacities):
    held = positions(snapshots)
    old = held["LONG"].leverage
    threshold = Fraction(dec(pair["ordinary"]["threshold"]))
    for target in TIERS:
        if target <= old or target < pair["ordinary"]["min_open_leverage"]:
            continue
        if target not in capacities or Fraction(positive(capacities[target], True)) <= threshold:
            continue
        if all(Fraction(p.qty) * max(Fraction(p.mark), Fraction(book.mark))
               <= Fraction(leverage_cap(snapshots[key].brackets.get(SYMBOL, []), target))
               for key, side in SIDES for p in [held[side]]):
            return target
    return None
