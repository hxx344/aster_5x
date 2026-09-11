"""Exact decimal risk calculations. No network or order side effects."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import time


ZERO = Decimal("0")
HEDGE_TOLERANCE = Decimal("0.001")
TIERS = (5, 10, 20)
MIN_OPEN_LEVERAGE = TIERS[0]
MIN_BATCH_NOTIONAL = Decimal("500")
TAKER_FEE_ESTIMATE = Decimal("0.0004")
SYMBOLS = ("XAUUSD1", "SPCXUSD1", "CLUSD1")


class TradingError(Exception):
    pass


class AccountModeError(TradingError):
    """A fixed account mode differs from the strategy's required configuration."""


def dec(value):
    if isinstance(value, bool) or value is None:
        raise TradingError("缺少有效数值")
    text = str(value)
    if len(text) > 128:
        raise TradingError("数值长度超出范围")
    try:
        result = Decimal(text)
    except (InvalidOperation, ValueError):
        raise TradingError("数值格式无效") from None
    if not result.is_finite():
        raise TradingError("数值不是有限数")
    if abs(result.as_tuple().exponent) > 100:
        raise TradingError("数值精度或数量级超出范围")
    return result


def positive(value, allow_zero=False):
    result = dec(value)
    if result < 0 or (not allow_zero and result == 0):
        raise TradingError("数值必须为正数")
    return result


def minimum_open_leverage(policy):
    """Read the supported opening floor, defaulting to the lowest tier."""
    value = policy.get("min_open_leverage", MIN_OPEN_LEVERAGE)
    if type(value) is not int or value not in TIERS:
        raise TradingError("最低开仓杠杆仅支持 5x、10x、20x")
    return value


def leverage_candidates(min_open_leverage=MIN_OPEN_LEVERAGE):
    """The opening floor does not authorize unsupported upgrade targets."""
    minimum_open_leverage({"min_open_leverage": min_open_leverage})
    return TIERS


def require_supported_leverage(leverage):
    if type(leverage) is not int or leverage not in TIERS:
        raise TradingError("新增开仓和杠杆调整仅支持 5x、10x、20x")


def floor_step(value, step):
    amount = value if isinstance(value, Fraction) else Fraction(dec(value))
    quantum = Fraction(positive(step))
    return decimal_value(int(amount / quantum) * quantum, exact=True)


def wire(value):
    if isinstance(value, Fraction):
        value = decimal_value(value, exact=True)
    return format(dec(value), "f")


def hedge_balanced(long_qty, short_qty):
    """Allow at most 0.1% of the larger side, including the exact boundary."""
    long_qty = long_qty if isinstance(long_qty, Fraction) else Fraction(positive(long_qty, True))
    short_qty = short_qty if isinstance(short_qty, Fraction) else Fraction(positive(short_qty, True))
    if long_qty < 0 or short_qty < 0:
        raise TradingError("持仓数量必须非负")
    return abs(long_qty - short_qty) <= max(long_qty, short_qty) * Fraction(HEDGE_TOLERANCE)


def decimal_value(value, exact=False):
    """Convert a rational for display, or preserve a terminating order quantity."""
    if not exact:
        return Decimal(value.numerator) / value.denominator
    denominator, twos, fives = value.denominator, 0, 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        raise TradingError("数量无法精确表示为有限小数")
    scale = max(twos, fives)
    coefficient = value.numerator * 2 ** (scale - twos) * 5 ** (scale - fives)
    return Decimal((int(coefficient < 0), tuple(int(d) for d in str(abs(coefficient))), -scale))


@dataclass
class Rules:
    symbol: str
    step: Decimal
    tick: Decimal
    min_qty: Decimal
    max_qty: Decimal
    min_notional: Decimal
    margin_asset: str = "USD1"


@dataclass
class Book:
    bid: Decimal
    ask: Decimal
    bid_qty: Decimal
    ask_qty: Decimal
    mark: Decimal
    timestamp: float

    @property
    def spread(self):
        return decimal_value(self.spread_exact)

    @property
    def spread_exact(self):
        if self.bid <= 0 or self.ask < self.bid:
            raise TradingError("BBO 价格无效")
        bid, ask = Fraction(self.bid), Fraction(self.ask)
        return (ask - bid) / ((ask + bid) / 2)

    def require_fresh(self, now=None, max_age=3):
        age = (time.time() if now is None else now) - self.timestamp
        if not -1 <= age <= max_age:
            raise TradingError("BBO 已过期，等待新报价")
        if self.mark <= 0 or self.bid_qty <= 0 or self.ask_qty <= 0:
            raise TradingError("盘口或标记价格无效")
        self.spread


@dataclass
class Position:
    symbol: str
    side: str
    qty: Decimal
    entry: Decimal
    mark: Decimal
    leverage: int
    unrealized: Decimal = ZERO
    liquidation: Decimal = ZERO
    maintenance: Decimal = ZERO
    isolated: bool = False

    @property
    def notional(self):
        return decimal_value(Fraction(dec(self.qty).copy_abs()) * Fraction(self.mark), exact=True)

    @property
    def occupied_margin(self):
        return decimal_value(self.occupied_margin_exact)

    @property
    def occupied_margin_exact(self):
        if type(self.leverage) is not int or not 1 <= self.leverage <= 125:
            raise TradingError("计算占用保证金需要有效的实际杠杆")
        return Fraction(positive(dec(self.qty).copy_abs(), True)) * Fraction(positive(self.mark, True)) / self.leverage


@dataclass
class AccountSnapshot:
    equity: Decimal
    maintenance: Decimal
    available: Decimal
    wallet: Decimal
    unrealized: Decimal
    positions: list[Position]
    open_orders: list[dict] | None  # None means the broker did not query external orders.
    hedge_mode: bool
    multi_assets: bool
    can_trade: bool
    timestamp: float
    fees: dict[str, Decimal] = field(default_factory=dict)
    brackets: dict[str, list[dict]] = field(default_factory=dict)

    @property
    def total_notional(self):
        # Gross exposure: both directions and every account position count.
        return decimal_value(sum((Fraction(p.notional) for p in self.positions), Fraction(0)), exact=True)

    @property
    def margin_ratio(self):
        if self.equity <= 0:
            raise TradingError("USD1 账户总权益不足")
        return decimal_value(Fraction(self.maintenance) / Fraction(self.equity))

    @property
    def occupied_margin(self):
        # Sum every position separately, including opposite sides and other markets.
        return decimal_value(self.occupied_margin_exact)

    @property
    def occupied_margin_exact(self):
        return sum((p.occupied_margin_exact for p in self.positions), Fraction(0))

    @property
    def ratio(self):
        if self.equity <= 0:
            raise TradingError("USD1 账户总权益不足")
        return decimal_value(self.occupied_margin_exact / Fraction(self.equity))

    def margin_exceeds(self, limit, include_equal=False):
        if self.equity <= 0:
            raise TradingError("USD1 账户总权益不足")
        occupied = self.occupied_margin_exact
        bound = Fraction(self.equity) * Fraction(positive(limit))
        return occupied >= bound if include_equal else occupied > bound

    def pair(self, symbol):
        rows = [p for p in self.positions if p.symbol == symbol]
        by_side = {p.side: p for p in rows}
        if set(by_side) != {"LONG", "SHORT"} or len(rows) != 2:
            raise TradingError(f"{symbol} 缺少双向持仓信息")
        if len({p.leverage for p in rows}) != 1:
            raise TradingError("多空杠杆不一致")
        if any(type(p.leverage) is not int or not 1 <= p.leverage <= 125 or p.qty < 0 for p in rows):
            raise TradingError("持仓数量或杠杆无效")
        return by_side["LONG"], by_side["SHORT"]

    def require_fresh(self, now=None, max_age=8):
        age = (time.time() if now is None else now) - self.timestamp
        if not -1 <= age <= max_age:
            raise TradingError("账户快照已过期")

    def require_ready(self, symbol, now=None):
        self.require_fresh(now)
        self.require_modes([symbol])
        if not self.can_trade:
            raise TradingError("账户没有交易权限")
        if self.open_orders:
            raise TradingError("账户存在未完成挂单，等待核对")
        self.ratio
        return self.pair(symbol)

    def mode_checks(self, symbols):
        relevant = [p for p in self.positions if p.qty or p.symbol in symbols]
        return {
            "cross": bool(relevant) and set(symbols).issubset({p.symbol for p in relevant})
                     and all(p.isolated is False for p in relevant),
            "hedge": self.hedge_mode is True,
            "single_asset": self.multi_assets is False,
        }

    def require_modes(self, symbols):
        labels = {"cross": "全仓保证金模式", "hedge": "双向持仓模式", "single_asset": "单币保证金模式（USD1）"}
        failed = [labels[key] for key, valid in self.mode_checks(symbols).items() if not valid]
        if failed:
            raise AccountModeError("账户固定模式不符合要求：" + "、".join(failed) + "；策略已暂停，程序不会修改账户模式")


def maintenance_for(notional, brackets):
    notional = positive(notional, allow_zero=True)
    if notional == 0:
        return ZERO
    matches = [b for b in brackets if dec(b["notionalFloor"]) <= notional <= dec(b["notionalCap"])]
    if not matches:
        raise TradingError("持仓超出已知风控档位")
    # At boundaries use the larger requirement, including inconsistent API tiers.
    return max(max(ZERO, notional * positive(b["maintMarginRatio"]) - positive(b["cum"], True)) for b in matches)


def leverage_cap(brackets, leverage):
    caps = [dec(b["notionalCap"]) for b in brackets if int(b["initialLeverage"]) >= leverage]
    if not caps:
        raise TradingError("目标杠杆缺少账户风控档位")
    return max(caps)


def validate_brackets(brackets):
    if not isinstance(brackets, list) or not brackets:
        raise TradingError("缺少账户风控档位")
    previous = None
    for row in sorted(brackets, key=lambda b: dec(b["notionalFloor"])):
        floor = positive(row["notionalFloor"], True)
        cap = positive(row["notionalCap"])
        rate = positive(row["maintMarginRatio"])
        cum = positive(row["cum"], True)
        if cap <= floor or rate > 1 or cum > floor * rate or not 1 <= int(row["initialLeverage"]) <= 125:
            raise TradingError("账户风控档位数值异常")
        if previous:
            if floor != dec(previous["notionalCap"]) or floor * rate - cum < floor * dec(previous["maintMarginRatio"]) - dec(previous["cum"]):
                raise TradingError("账户风控档位不连续或维持保证金递减")
        elif floor != 0:
            raise TradingError("账户首个风控档位不是从零开始")
        previous = row
    return brackets


@dataclass
class Plan:
    qty: Decimal = ZERO
    projected_ratio: Decimal | None = None
    reason: str = "等待"


def opening_margin_limit(policy, leverage):
    """10x and 20x share five percentage points above the account base limit."""
    base = Fraction(dec(policy["margin_limit"]))
    bonus = Fraction(1, 20) if leverage in (10, 20) else Fraction(0)
    return decimal_value(min(Fraction(1), base + bonus), exact=True)


def plan_pair(snapshot, book, rules, capacities, policy, now=None):
    """Size both legs against total occupied margin / equity, cash and capacity."""
    long, short = snapshot.require_ready(rules.symbol, now)
    book.require_fresh(now)
    minimum = minimum_open_leverage(policy)
    if long.leverage < minimum:
        return Plan(reason=f"当前 {long.leverage}x 低于 {minimum}x，禁止新增开仓，等待升杠杆")
    if long.leverage not in TIERS:
        return Plan(reason=f"当前 {long.leverage}x 不在支持档位，禁止新增开仓，等待升杠杆至 5x、10x、20x")
    if dec(policy["order_notional"]) < MIN_BATCH_NOTIONAL:
        return Plan(reason="单批每边上限低于固定最低批次金额 500 USD1，请修改策略设置")
    limit = Fraction(opening_margin_limit(policy, long.leverage))
    if snapshot.margin_exceeds(decimal_value(limit, exact=True), include_equal=True):
        return Plan(reason="保证金占用率已达到上限，等待升杠杆或释放占用")
    if book.spread_exact > Fraction(dec(policy["spread_limit"])):
        return Plan(reason="BBO 价差超过万 5")
    if not hedge_balanced(long.qty, short.qty):
        return Plan(reason="已有多空数量差超过 0.1%，等待人工核对")
    leverage = long.leverage
    if positive(capacities.get(leverage), True) <= dec(policy["threshold"]):
        return Plan(reason=f"{leverage}x 额度未超过阈值")
    brackets = snapshot.brackets.get(rules.symbol)
    fee = snapshot.fees.get(rules.symbol)
    if not brackets or fee is None:
        raise TradingError("缺少账户风控档位或手续费率")
    # Use gross exposure for capacity checks; never offset LONG against SHORT.
    bid, ask, mark = map(Fraction, (book.bid, book.ask, book.mark))
    long_qty, short_qty = map(Fraction, (long.qty, short.qty))
    fee = Fraction(fee)
    gross = (long_qty + short_qty) * mark
    current_remaining = Fraction(positive(capacities.get(leverage), True))
    cap_room = max(Fraction(0), Fraction(leverage_cap(brackets, leverage)) - gross)
    public_room = min(current_remaining, cap_room)
    high_price = max(ask, mark)
    loss_span = max(Fraction(0), ask - mark) + max(Fraction(0), mark - bid)
    occupied = snapshot.occupied_margin_exact
    current_pair_margin = long.occupied_margin_exact + short.occupied_margin_exact
    # Conservatively revalue the existing pair if the latest quote is higher.
    price_adjustment = max(Fraction(0), (long_qty + short_qty) * high_price / leverage - current_pair_margin)
    # A tolerated net position can lose equity between the account and book reads.
    existing_pnl_change = long_qty * (mark - Fraction(long.mark)) - short_qty * (mark - Fraction(short.mark))
    existing_loss = max(Fraction(0), -existing_pnl_change)
    max_qty = min(
        Fraction(dec(policy["order_notional"])) / high_price,
        Fraction(rules.max_qty), Fraction(book.ask_qty), Fraction(book.bid_qty),
        public_room / (2 * high_price),
        max(Fraction(0), Fraction(snapshot.available) - existing_loss - price_adjustment)
        / (2 * high_price / leverage + (ask + bid) * fee + loss_span),
    )
    # MARKET orders use the mark price for the exchange's minimum notional.
    min_qty = max(Fraction(rules.min_qty), Fraction(max(rules.min_notional, MIN_BATCH_NOTIONAL)) / mark)
    step = Fraction(rules.step)

    def projected(qty):
        # Charge the complete spread and both taker fees; no unrealized gain credit.
        cost = qty * loss_span + qty * (ask + bid) * fee
        equity = Fraction(snapshot.equity) - existing_loss - cost
        total_occupied = occupied + price_adjustment + 2 * qty * high_price / leverage
        return total_occupied, equity

    low, high = 0, max_qty // step
    while low < high:
        mid = (low + high + 1) // 2
        total_occupied, equity = projected(mid * step)
        if equity > 0 and total_occupied <= limit * equity:
            low = mid
        else:
            high = mid - 1
    qty = low * step
    if qty < min_qty or qty == 0:
        return Plan(reason="风险、余额或额度不足以继续最小一笔（每边至少 500 USD1）")
    total_occupied, equity = projected(qty)
    return Plan(decimal_value(qty, exact=True), decimal_value(total_occupied / equity), "可以分批双向开仓")


def require_non_decreasing_leverage(current, target):
    if any(type(v) is not int or not 1 <= v <= 125 for v in (current, target)):
        raise TradingError("杠杆档位无效")
    if target < current:
        raise TradingError(f"全局禁止降杠杆：当前 {current}x，目标 {target}x")


def next_leverage(snapshot, symbol, capacities, mark=None, threshold=ZERO, min_open_leverage=MIN_OPEN_LEVERAGE):
    long, short = snapshot.pair(symbol)
    if not hedge_balanced(long.qty, short.qty):
        return None
    gross = (Fraction(long.qty) + Fraction(short.qty)) * Fraction(positive(mark)) if mark is not None else \
        Fraction(long.qty) * Fraction(long.mark) + Fraction(short.qty) * Fraction(short.mark)
    # Select the lowest usable higher tier; unavailable intermediate tiers do not block.
    required = max(gross, positive(threshold, True))
    for target in leverage_candidates(min_open_leverage):
        if target <= long.leverage or target not in capacities:
            continue
        if positive(capacities[target], True) > required and leverage_cap(snapshot.brackets[symbol], target) >= gross:
            return target
    return None
