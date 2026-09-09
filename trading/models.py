"""Exact decimal risk calculations. No network or order side effects."""
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import time


ZERO = Decimal("0")
TIERS = (4, 5, 10, 20)
SYMBOLS = ("XAUUSD1", "SPCXUSD1", "CLUSD1")


class TradingError(Exception):
    pass


class AccountModeError(TradingError):
    """A fixed account mode differs from the strategy's required configuration."""


def dec(value):
    if isinstance(value, bool) or value is None:
        raise TradingError("缺少有效数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise TradingError("数值格式无效") from None
    if not result.is_finite():
        raise TradingError("数值不是有限数")
    return result


def positive(value, allow_zero=False):
    result = dec(value)
    if result < 0 or (not allow_zero and result == 0):
        raise TradingError("数值必须为正数")
    return result


def floor_step(value, step):
    return (dec(value) / positive(step)).to_integral_value(rounding=ROUND_DOWN) * step


def wire(value):
    return format(dec(value), "f")


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
        if self.bid <= 0 or self.ask < self.bid:
            raise TradingError("BBO 价格无效")
        return (self.ask - self.bid) / ((self.ask + self.bid) / 2)

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
        return abs(self.qty) * self.mark

    @property
    def occupied_margin(self):
        if type(self.leverage) is not int or not 1 <= self.leverage <= 125:
            raise TradingError("计算占用保证金需要有效的实际杠杆")
        return positive(self.notional, True) / self.leverage


@dataclass
class AccountSnapshot:
    equity: Decimal
    maintenance: Decimal
    available: Decimal
    wallet: Decimal
    unrealized: Decimal
    positions: list[Position]
    open_orders: list[dict]
    hedge_mode: bool
    multi_assets: bool
    can_trade: bool
    timestamp: float
    fees: dict[str, Decimal] = field(default_factory=dict)
    brackets: dict[str, list[dict]] = field(default_factory=dict)

    @property
    def occupied_margin(self):
        # Sum every position separately, including opposite sides and other markets.
        return sum((p.occupied_margin for p in self.positions), ZERO)

    @property
    def ratio(self):
        if self.equity <= 0:
            raise TradingError("USD1 账户总权益不足")
        return self.occupied_margin / self.equity

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

    def require_ready(self, symbol, now=None):
        age = (time.time() if now is None else now) - self.timestamp
        if not -1 <= age <= 8:
            raise TradingError("账户快照已过期")
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


def plan_pair(snapshot, book, rules, capacities, policy, now=None):
    """Size both legs against total occupied margin / equity, cash and capacity."""
    long, short = snapshot.require_ready(rules.symbol, now)
    book.require_fresh(now)
    limit = dec(policy["margin_limit"])
    if snapshot.ratio >= limit:
        return Plan(reason="保证金占用率已达到上限，等待升杠杆或释放占用")
    if book.spread > dec(policy["spread_limit"]):
        return Plan(reason="BBO 价差超过万 5")
    if long.qty != short.qty:
        return Plan(reason="已有多空仓位不平衡，等待人工核对")
    leverage = long.leverage
    if positive(capacities.get(leverage), True) <= dec(policy["threshold"]):
        return Plan(reason=f"{leverage}x 额度未超过阈值")
    brackets = snapshot.brackets.get(rules.symbol)
    fee = snapshot.fees.get(rules.symbol)
    if not brackets or fee is None:
        raise TradingError("缺少账户风控档位或手续费率")
    # Use gross exposure for capacity checks; never offset LONG against SHORT.
    gross = (long.qty + short.qty) * book.mark
    current_remaining = positive(capacities.get(leverage), True)
    cap_room = max(ZERO, leverage_cap(brackets, leverage) - gross)
    public_room = min(current_remaining, cap_room)
    high_price = max(book.ask, book.mark)
    loss_span = max(ZERO, book.ask - book.mark) + max(ZERO, book.mark - book.bid)
    max_qty = floor_step(min(
        dec(policy["order_notional"]) / high_price,
        rules.max_qty, book.ask_qty, book.bid_qty,
        public_room / (2 * high_price),
        max(ZERO, snapshot.available) / (2 * high_price / leverage + (book.ask + book.bid) * fee + loss_span),
    ), rules.step)
    min_qty = max(rules.min_qty, rules.min_notional / book.bid)
    occupied = snapshot.occupied_margin
    current_pair_margin = long.occupied_margin + short.occupied_margin
    # Conservatively revalue the existing pair if the latest quote is higher.
    price_adjustment = max(ZERO, (long.qty + short.qty) * high_price / leverage - current_pair_margin)

    def projected(qty):
        # Charge the complete spread and both taker fees; no unrealized gain credit.
        cost = qty * loss_span + qty * (book.ask + book.bid) * fee
        equity = snapshot.equity - cost
        total_occupied = occupied + price_adjustment + 2 * qty * high_price / leverage
        return total_occupied / equity if equity > 0 else Decimal("Infinity")

    low, high = 0, int(max_qty / rules.step)
    while low < high:
        mid = (low + high + 1) // 2
        if projected(mid * rules.step) <= limit:
            low = mid
        else:
            high = mid - 1
    qty = low * rules.step
    if qty < min_qty or qty == 0:
        return Plan(reason="风险、余额或额度不足以继续最小一笔")
    return Plan(qty, projected(qty), "可以分批双向开仓")


def require_non_decreasing_leverage(current, target):
    if any(type(v) is not int or not 1 <= v <= 125 for v in (current, target)):
        raise TradingError("杠杆档位无效")
    if target < current:
        raise TradingError(f"全局禁止降杠杆：当前 {current}x，目标 {target}x")


def next_leverage(snapshot, symbol, capacities, mark=None, threshold=ZERO):
    long, short = snapshot.pair(symbol)
    if long.qty != short.qty:
        return None
    gross = (long.qty + short.qty) * positive(mark) if mark is not None else long.notional + short.notional
    # Select the lowest usable higher tier; unavailable intermediate tiers do not block.
    required = max(gross, positive(threshold, True))
    for target in TIERS:
        if target <= long.leverage or target not in capacities:
            continue
        if positive(capacities[target], True) > required and leverage_cap(snapshot.brackets[symbol], target) >= gross:
            return target
    return None
