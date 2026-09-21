"""Deletion eligibility shared by the dashboard and transactional removal."""
from .models import TradingError, dec


def deletion_block(account, pending, post_fill, cycle):
    if account["enabled"]:
        return "请先暂停账户，再删除"
    if pending or post_fill:
        return "账户仍有未完成交易或成交后核对，请处理完成后再删除"
    if cycle:
        try:
            if not isinstance(cycle, dict):
                raise TradingError("循环记录无效")
            quantities = cycle.get("quantities", {})
            if (not isinstance(quantities, dict) or set(quantities) - {"LONG", "SHORT"}
                    or cycle.get("phase", "waiting_open") not in ("waiting_open", "holding", "waiting_close")):
                raise TradingError("循环数量无效")
            if (cycle.get("opened_at") is not None or cycle.get("phase") in ("holding", "waiting_close")
                    or any(dec(value) != 0 for value in quantities.values())):
                return "账户仍有未结束的循环持仓，请结束本轮并核对后再删除"
        except TradingError:
            return "循环记录异常，请核对后再删除"
    return None
