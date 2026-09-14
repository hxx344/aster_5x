"""Account-local ordinary-add protection for a selected volume-cycle market."""
from __future__ import annotations

from .models import SYMBOLS, TradingError


_DEFAULT_SYMBOL = "XAUUSD1"


def _cycle_selection(account):
    """Validate only the mode selector; quantities and leverage do not grant access."""
    if not isinstance(account, dict):
        raise TradingError("本账户循环配置无法核对，已停止普通加仓")
    cycle = account.get("cycle")
    if cycle is None:
        cycle = {}
    if not isinstance(cycle, dict):
        raise TradingError("本账户循环配置格式无效，已停止普通加仓")
    enabled = cycle.get("enabled", False)
    symbol = cycle.get("symbol", _DEFAULT_SYMBOL)
    if type(enabled) is not bool:
        raise TradingError("本账户循环开关无效，已停止普通加仓")
    if not isinstance(symbol, str) or symbol not in SYMBOLS:
        raise TradingError("本账户循环品种无效，已停止普通加仓")
    return enabled, symbol


def _reason(symbol):
    return f"本账户 {symbol} 已启用成交量循环，禁止该品种 5x / 10x / 20x 普通加仓"


def ordinary_add_block_reason(account, symbol):
    """Return this account's block reason, or None; malformed selectors fail closed.

    Pausing the account or exhausting a volume allowance does not deselect cycle
    mode. Other accounts and unrelated markets are never consulted or blocked.
    """
    if not isinstance(symbol, str) or symbol not in SYMBOLS:
        raise TradingError("本账户普通加仓品种无效，已停止普通加仓")
    enabled, selected = _cycle_selection(account)
    return _reason(symbol) if enabled and selected == symbol else None


def ordinary_add_blocks(account):
    """Return only protected market entries, using the same selector as execution."""
    enabled, selected = _cycle_selection(account)
    return {selected: _reason(selected)} if enabled else {}
