"""Opening-only admission from the exact leverage's shared public hot data."""
from fractions import Fraction
import math

from .cycle import validate_cycle
from .cycle_diagnostics import diagnostic_error, diagnostic_number
from .models import TradingError, dec, positive, wire


CYCLE_CAPACITY_MAX_AGE = 1


def capacity_target(config):
    config = validate_cycle(config)
    target = Fraction(dec(config["max_notional"])) * (2 if config["notional_scope"] == "per_side" else 1)
    return target, target * Fraction(dec(config["capacity_multiplier"]))


def require_cycle_capacity(config, leverage, row, *, now, minimum_notional=0):
    """Missing, failed or stale samples never authorize new exposure.

    The target is the configured upper amount, expressed as both legs combined;
    account risk checks may subsequently size the order below that target.
    """
    target, required = capacity_target(config)
    required = max(required, Fraction(positive(minimum_notional, True)))
    stamp = row.get("capacity_checked_at", {}).get(str(leverage), row.get("checked_at"))
    fresh = (type(stamp) in (int, float) and math.isfinite(stamp)
             and -1 <= now - stamp <= CYCLE_CAPACITY_MAX_AGE)
    known = type(leverage) is int and 1 <= leverage <= 125
    value = None
    if known and fresh and row.get("status") == "ok":
        try:
            value = positive(row.get("capacities", {}).get(str(leverage)), True)
        except TradingError:
            pass
    if value is None or Fraction(value) < required:
        title = ("循环公共额度未就绪或已过期，等待对应实际杠杆的热数据" if value is None
                 else "循环公共额度不足目标名义价值的设定倍数，等待额度释放")
        raise diagnostic_error("cycle_market_capacity", title, symbol=config["symbol"], phase="open", checked_at=now,
            checks=[{"code": "market_capacity", "label": f"{leverage}x 公共剩余额度" if known else "实际杠杆公共剩余额度",
                     "actual": diagnostic_number(value) if value is not None else None,
                     "required": "≥ " + diagnostic_number(required), "unit": "USD1",
                     "passed": False if value is not None else None}],
            context=[{"label": "本轮目标名义价值（多空合计）", "value": wire(target), "unit": "USD1"},
                     {"label": "额度倍数", "value": config.get("capacity_multiplier", "1"), "unit": "倍"}],
            note="先检查对应实际杠杆的公共额度，再检查热差价。账户档位、保证金与成交量限制仍需满足；减仓和补救不检查公共额度。")
    return value
