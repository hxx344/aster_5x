"""Display-only cycle condition details; never decide whether an order is allowed."""
from __future__ import annotations

from copy import deepcopy
from decimal import Context, Decimal
from fractions import Fraction

from .models import TradingError, decimal_value


class CycleConditionError(TradingError):
    """A cycle condition failed, optionally with a structured explanation."""

    def __init__(self, message, diagnostic=None):
        super().__init__(message)
        self.diagnostic = diagnostic


def diagnostic_number(value, scale=1):
    """Format exact finite decimals; mark independently rounded rationals with ≈.

    ``scale`` multiplies the value (for example, 100 displays a ratio as percent).
    Display rounding must never feed back into a condition's exact comparison.
    """
    number = Fraction(value) * Fraction(scale)
    denominator = number.denominator
    for prime in (2, 5):
        while denominator % prime == 0:
            denominator //= prime
    if denominator == 1:
        result = format(decimal_value(number, exact=True), "f")
        return result.rstrip("0").rstrip(".") if "." in result else result
    context = Context(prec=36)
    result = context.divide(Decimal(number.numerator), Decimal(number.denominator))
    return "≈" + format(result, "g")


def diagnostic_error(code, title, *, symbol, phase, checked_at, checks,
                     context=None, note=None, error_type=CycleConditionError):
    """Return the requested exception type with a JSON-ready diagnostic."""
    diagnostic = {"code": code, "title": title, "checked_at": checked_at,
                  "symbol": symbol, "phase": phase, "checks": deepcopy(checks)}
    if context:
        diagnostic["context"] = deepcopy(context)
    if note:
        diagnostic["note"] = note
    failed = [check for check in checks if check["passed"] is False]
    details = []
    for check in failed[:4]:
        unit = " " + check["unit"] if check["unit"] else ""
        actual = check["actual"] if check["actual"] is not None else "未取得"
        required = check["required"] if check["required"] is not None else "待核对"
        details.append(f"{check['label']} {actual}{unit}（要求 {required}{unit}）")
    if len(failed) > 4:
        details.append(f"另有 {len(failed) - 4} 项条件未满足")
    for item in (context or [])[:3]:
        unit = " " + item["unit"] if item.get("unit") else ""
        details.append(f"{item['label']} {item['value']}{unit}")
    message = title + ("：" + "；".join(details) if details else "")
    return error_type(message, diagnostic=diagnostic)
