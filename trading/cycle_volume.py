"""Exact UTC-day accounting helpers for individual cycle execution fills."""
from datetime import datetime, timedelta, timezone
from fractions import Fraction
import math
import re
import time

from .models import SYMBOLS, TradingError, dec, positive, wire


FILL_FIELDS = frozenset({"trade_id", "order_id", "client_id", "symbol", "position_side", "side",
                         "quantity", "price", "notional", "executed_at", "time_source"})
TERMINAL = frozenset({"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"})
TIME_SOURCES = frozenset({"exchange", "paper", "legacy_estimated"})


def identifier(value, label="成交标识"):
    if (not isinstance(value, str) or not 1 <= len(value) <= 200 or value.strip() != value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise TradingError(label + "无效")
    return value


def account_identifier(value):
    if not isinstance(value, str) or re.fullmatch(r"[a-z0-9_-]{1,32}", value) is None:
        raise TradingError("循环交易量账户标识无效")
    return value


def utc_day(now=None):
    value = time.time() if now is None else now
    if type(value) not in (int, float) or value < 0 or value > 253402300799 or not math.isfinite(value):
        raise TradingError("循环成交时间必须为有效 UTC 时间戳")
    try:
        point = datetime.fromtimestamp(value, timezone.utc)
        start = point.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    except (OverflowError, OSError, ValueError):
        raise TradingError("循环成交时间超出有效范围") from None
    return start.strftime("%Y-%m-%d"), start.timestamp(), end.timestamp()


def normalize_fill(fill):
    if not isinstance(fill, dict) or set(fill) != FILL_FIELDS:
        raise TradingError("循环逐笔成交字段不完整或无效")
    result = dict(fill)
    for key in ("trade_id", "order_id", "client_id"):
        result[key] = identifier(result[key])
    if (any(not isinstance(result[key], str) for key in ("symbol", "position_side", "side", "time_source"))
            or result["symbol"] not in SYMBOLS or result["position_side"] not in ("LONG", "SHORT")
            or result["side"] not in ("BUY", "SELL") or result["time_source"] not in TIME_SOURCES):
        raise TradingError("循环逐笔成交品种、方向或时间来源无效")
    for key in ("quantity", "price", "notional"):
        if not isinstance(result[key], str):
            raise TradingError("循环逐笔成交价量必须为十进制字符串")
        result[key] = wire(Fraction(positive(result[key])))
    if Fraction(dec(result["quantity"])) * Fraction(dec(result["price"])) != Fraction(dec(result["notional"])):
        raise TradingError("循环逐笔成交金额必须等于实际成交数量乘成交价格")
    result["utc_date"] = utc_day(result["executed_at"])[0]
    result["executed_at"] = float(result["executed_at"])
    return result


def order_bindings(intent):
    """Derive eligible client IDs and directions from the durable cycle intent."""
    if (not isinstance(intent, dict) or intent.get("kind") != "cycle"
            or intent.get("phase") not in ("open", "close") or intent.get("symbol") not in SYMBOLS):
        raise TradingError("仅允许为独立多空循环委托记录交易量")
    receipts = intent.get("receipts")
    if not isinstance(receipts, dict):
        raise TradingError("循环成交回执记录无效")
    result = {}
    if not isinstance(intent.get("orders"), list) or not intent["orders"]:
        raise TradingError("循环原始委托记录缺失，不能推断零成交")
    for key, phase in (("orders", intent["phase"]), ("repairs", "repair")):
        orders = intent.get(key, [])
        if not isinstance(orders, list):
            raise TradingError("循环委托记录无效")
        for order in orders:
            if not isinstance(order, dict):
                raise TradingError("循环委托记录无效")
            cid = identifier(order.get("newClientOrderId"), "循环委托标识")
            if cid in result or order.get("symbol") != intent["symbol"]:
                raise TradingError("循环委托标识重复或品种不一致")
            position_side = order.get("positionSide")
            expected_side = ("BUY" if position_side == "LONG" else "SELL") if phase == "open" else \
                            ("SELL" if position_side == "LONG" else "BUY")
            if position_side not in ("LONG", "SHORT") or order.get("side") != expected_side:
                raise TradingError("循环委托增减仓方向与阶段不符")
            positive(order.get("quantity"))
            result[cid] = (order, receipts.get(cid), phase)
    return result


def receipt_quantity(order, receipt, *, require_terminal=False):
    if (not isinstance(receipt, dict)
            or any(receipt.get(key) != order.get(key) for key in ("symbol", "positionSide", "side"))
            or receipt.get("clientOrderId") != order["newClientOrderId"]):
        raise TradingError("循环成交回执与持久委托不匹配")
    if not isinstance(receipt.get("status"), str):
        raise TradingError("循环成交回执状态无效")
    if require_terminal and receipt.get("status") not in TERMINAL:
        raise TradingError("循环逐笔成交尚未全部核对，不能标记同步完成")
    if receipt.get("status") not in TERMINAL | {"NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"}:
        raise TradingError("循环成交回执状态无效")
    quantity = Fraction(positive(receipt.get("executedQty"), True))
    submitted = Fraction(positive(order["quantity"]))
    if quantity > submitted or receipt.get("status") == "FILLED" and quantity != submitted:
        raise TradingError("循环成交回执数量超过委托或状态不一致")
    return quantity


def validate_fill_binding(fill, bindings):
    binding = bindings.get(fill["client_id"])
    if binding is None:
        raise TradingError("循环逐笔成交不属于本批委托")
    order, receipt, phase = binding
    if (fill["symbol"] != order["symbol"] or fill["position_side"] != order["positionSide"]
            or fill["side"] != order["side"]):
        raise TradingError("循环逐笔成交品种或方向与委托不符")
    quantity = receipt_quantity(order, receipt)
    oid = receipt.get("orderId")
    if type(oid) not in (str, int) or not str(oid) or str(oid) != fill["order_id"]:
        raise TradingError("循环逐笔成交交易所订单号与回执不一致")
    return phase, quantity


def sort_key(fill):
    return fill["executed_at"], fill["symbol"], fill["trade_id"]


def event_message(fill):
    phase = {"open": "开仓", "close": "平仓", "repair": "减仓补偿"}[fill["phase"]]
    direction = "多头" if fill["position_side"] == "LONG" else "空头"
    estimated = "；历史成交时间为估算" if fill["time_source"] == "legacy_estimated" else ""
    return (f"{fill['symbol']} {direction}{phase}成交 {fill['quantity']}，本笔交易量 {fill['notional']} USD1；"
            f"UTC {fill['utc_date']} 该品种按成交时间累计 {fill['daily_volume']} USD1{estimated}")
