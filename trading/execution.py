"""Crash-recoverable hedge execution. All mutations have durable intent IDs."""
import time
import uuid

from .exchange import ExchangeError
from .models import TradingError, dec, floor_step, positive, wire

TERMINAL = {"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"}


class Executor:
    def __init__(self, store, broker, market):
        self.store, self.broker, self.market = store, broker, market

    def open_pair(self, account, snapshot, symbol, plan, book):
        long, short = snapshot.pair(symbol)
        if self.store.intent(account["id"]):
            raise TradingError("已有批次正在执行")
        token = uuid.uuid4().hex
        orders = [self.order(symbol, side, "BUY" if side == "LONG" else "SELL", plan.qty,
                            book.ask if side == "LONG" else book.bid, side[0] + token[:28]) for side in ("LONG", "SHORT")]
        intent = {"id": token, "kind": "pair", "account_id": account["id"], "symbol": symbol, "leverage": long.leverage,
                  "status": "pending", "created_at": time.time(), "baseline": {"LONG": wire(long.qty), "SHORT": wire(short.qty)},
                  "orders": orders, "receipts": {}, "repairs": [], "repair_attempts": 0}
        self.store.save_intent(intent)  # FULL synchronous commit before any write request.
        self.store.event(account["id"], "order", f"{symbol} {long.leverage}x 提交双向批次，每边 {wire(plan.qty)}")
        self.send(intent, orders)
        return self.reconcile(account, intent)

    @staticmethod
    def order(symbol, position_side, side, qty, price, client_id):
        return {"symbol": symbol, "positionSide": position_side, "side": side, "type": "LIMIT", "timeInForce": "FOK",
                "quantity": wire(qty), "price": wire(price), "newClientOrderId": client_id, "newOrderRespType": "RESULT"}

    def send(self, intent, orders):
        try:
            result = self.broker.submit(orders)
            if not isinstance(result, list) or len(result) != len(orders):
                raise TradingError("批量订单响应格式异常，等待逐笔核对")
            for order, row in zip(orders, result):
                cid = order["newClientOrderId"]
                if isinstance(row, dict) and isinstance(row.get("code"), int) and row["code"] < 0:
                    # A batch-level timeout still needs a query, even inside HTTP 200.
                    if row["code"] not in (-1006, -1007):
                        intent["receipts"][cid] = {**order, "clientOrderId": cid, "status": "REJECTED", "executedQty": "0", "avgPrice": "0"}
                else:
                    self.validate_receipt(order, row)
                    intent["receipts"][cid] = row
        except TradingError as exc:
            # Even a transport failure may have reached the exchange. Never resend.
            intent["last_error"] = str(exc)
        self.store.save_intent(intent)

    @staticmethod
    def validate_receipt(order, row):
        if not isinstance(row, dict) or any(row.get(key) != order[key] for key in ("symbol", "positionSide", "side")) or row.get("clientOrderId") != order["newClientOrderId"]:
            raise TradingError("订单回执与本批委托不匹配")
        qty = positive(row.get("executedQty"), True)
        if qty > dec(order["quantity"]) or row.get("status") not in TERMINAL | {"NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"}:
            raise TradingError("订单成交数量或状态无效")
        if row["status"] == "FILLED" and qty != dec(order["quantity"]):
            raise TradingError("完全成交回执数量与委托数量不一致")
        if qty:
            positive(row.get("avgPrice"))

    def leverage(self, account, symbol, old, target):
        if self.store.intent(account["id"]):
            raise TradingError("已有批次正在执行")
        intent = {"id": uuid.uuid4().hex, "kind": "leverage", "account_id": account["id"], "symbol": symbol,
                  "previous": old, "target": target, "created_at": time.time(), "status": "pending"}
        self.store.save_intent(intent)
        try:
            self.broker.set_leverage(symbol, target)
        except TradingError as exc:
            intent["last_error"] = str(exc)
            self.store.save_intent(intent)
        return "正在核对杠杆调整结果"

    def attention(self, account, intent, reason):
        if intent.get("status") != "attention" or intent.get("last_error") != reason:
            self.store.event(account["id"], "error", reason)
        intent.update(status="attention", last_error=reason)
        self.store.save_intent(intent)
        account["enabled"] = False
        self.store.save_account(account)
        return reason

    def reconcile(self, account, intent=None):
        intent = intent or self.store.intent(account["id"])
        if not intent:
            return "没有未完成批次"
        if intent["kind"] == "leverage":
            snapshot = self.broker.snapshot(account["policy"]["symbols"])
            long, short = snapshot.pair(intent["symbol"])
            if long.leverage == short.leverage == intent["target"]:
                intent["status"] = "complete"
                self.store.save_intent(intent)
                self.store.event(account["id"], "leverage", f"{intent['symbol']} 杠杆已从 {intent['previous']}x 调整为 {intent['target']}x，并核实到账户")
                return "杠杆调整已确认"
            if time.time() - intent["created_at"] > 120:
                return self.attention(account, intent, "杠杆变更尚未确认，请核对账户后重新检查")
            return "等待账户确认目标杠杆"

        orders = intent["orders"] + intent["repairs"]
        unresolved = []
        for order in orders:
            cid = order["newClientOrderId"]
            known = intent["receipts"].get(cid)
            if known and known.get("status") in TERMINAL:
                try:
                    self.validate_receipt(order, known)
                except TradingError as exc:
                    return self.attention(account, intent, str(exc))
                continue
            try:
                row = self.broker.query(intent["symbol"], cid)
                self.validate_receipt(order, row)
                intent["receipts"][cid] = row
                if row["status"] not in TERMINAL:
                    unresolved.append(cid)
                    if time.time() - intent["created_at"] > 10:
                        self.broker.cancel(intent["symbol"], cid)
            except ExchangeError as exc:
                unresolved.append(cid)
                intent["last_error"] = str(exc)
            except TradingError as exc:
                return self.attention(account, intent, str(exc))
        self.store.save_intent(intent)
        if unresolved:
            if time.time() - intent["created_at"] > 120:
                return self.attention(account, intent, "订单结果仍不确定，已暂停新开仓；请核对未完成批次")
            return "核对订单回执中，不重复提交"

        # Confirm actual account positions before any repair, including manual changes/ADL.
        snapshot = self.broker.snapshot(account["policy"]["symbols"])
        long, short = snapshot.pair(intent["symbol"])
        filled = {side: dec(0) for side in ("LONG", "SHORT")}
        notionals = {side: dec(0) for side in filled}
        for order in intent["orders"]:
            row = intent["receipts"][order["newClientOrderId"]]
            qty = dec(row["executedQty"])
            filled[order["positionSide"]] += qty
            notionals[order["positionSide"]] += qty * dec(row["avgPrice"])
        repaired = {side: dec(0) for side in filled}
        for order in intent["repairs"]:
            repaired[order["positionSide"]] += dec(intent["receipts"][order["newClientOrderId"]]["executedQty"])
        net = {side: filled[side] - repaired[side] for side in filled}
        actual = {"LONG": long.qty, "SHORT": short.qty}
        if any(actual[s] != dec(intent["baseline"][s]) + net[s] or net[s] < 0 for s in actual):
            return self.attention(account, intent, "持仓变化与本批回执不一致，暂停并等待核对（可能有外部成交或 ADL）")
        if net["LONG"] != net["SHORT"]:
            if intent["repair_attempts"] >= 3:
                return self.attention(account, intent, "单腿补偿尚未完成，已暂停新开仓；请核对并重试补偿")
            side = "LONG" if net["LONG"] > net["SHORT"] else "SHORT"
            book = self.market.book(intent["symbol"])
            book.require_fresh()
            rule = self.market.rules[intent["symbol"]]
            qty = floor_step(min(abs(net["LONG"] - net["SHORT"]), book.bid_qty if side == "LONG" else book.ask_qty), rule.step)
            if qty <= 0:
                return self.attention(account, intent, "盘口不足以补偿本批单腿，请核对持仓")
            repair = self.order(intent["symbol"], side, "SELL" if side == "LONG" else "BUY", qty,
                                book.bid if side == "LONG" else book.ask, "R" + uuid.uuid4().hex[:28])
            intent["repairs"].append(repair)
            intent["repair_attempts"] += 1
            intent["status"] = "repair"
            self.store.save_intent(intent)
            self.store.event(account["id"], "repair", f"{intent['symbol']} 处理单腿差额：仅平掉本批多出的 {side} {wire(qty)}")
            self.send(intent, [repair])
            return self.reconcile(account, intent)

        qty = net["LONG"]
        if qty:
            # Completed notional refers to the matched portion, not repaired exposure.
            total = sum(notionals[s] * qty / filled[s] for s in filled)
            self.store.complete_pair(intent, {"qty": wire(qty), "notional": wire(total)})
        else:
            intent["status"] = "aborted"
            self.store.save_intent(intent)
            self.store.event(account["id"], "order", f"{intent['symbol']} 本批未形成新增双向仓位，订单已核对")
        return "双向批次已核对完成" if qty else "本批未成交或已完成单腿补偿"
