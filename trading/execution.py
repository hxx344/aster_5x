"""Crash-recoverable hedge execution. All mutations have durable intent IDs."""
import time
import uuid

from .exchange import ExchangeError
from .models import AccountModeError, TradingError, dec, floor_step, hedge_balanced, positive, require_non_decreasing_leverage, wire

TERMINAL = {"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"}


class Executor:
    def __init__(self, store, broker, market):
        self.store, self.broker, self.market = store, broker, market

    def open_pair(self, account, snapshot, symbol, plan, book):
        snapshot.require_modes(account["policy"]["symbols"])
        long, short = snapshot.pair(symbol)
        if not hedge_balanced(long.qty, short.qty):
            raise TradingError("已有多空数量差超过 0.1%，等待人工核对")
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

    def leverage(self, account, symbol, old, target, snapshot=None):
        require_non_decreasing_leverage(old, target)
        # Re-read before creating intent; the selection snapshot may now be stale.
        snapshot = self.broker.snapshot(account["policy"]["symbols"], fresh_modes=True)
        snapshot.require_modes(account["policy"]["symbols"])
        long, short = snapshot.require_ready(symbol)
        old = long.leverage
        require_non_decreasing_leverage(old, target)
        if not hedge_balanced(long.qty, short.qty):
            raise TradingError("已有多空数量差超过 0.1%，等待人工核对")
        if self.store.intent(account["id"]):
            raise TradingError("已有批次正在执行")
        if target == old:
            return f"当前已为 {old}x，保持杠杆不变"
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
            if intent["target"] < intent["previous"]:
                return self.attention(account, intent, "发现旧降杠杆批次，全局禁止继续执行；请核对实际杠杆")
            snapshot = self.broker.snapshot(account["policy"]["symbols"])
            try:
                snapshot.require_modes(account["policy"]["symbols"])
            except AccountModeError as exc:
                return self.attention(account, intent, str(exc))
            long, short = snapshot.pair(intent["symbol"])
            if long.leverage == short.leverage and long.leverage >= intent["target"]:
                self.store.complete_leverage(intent, long.leverage)
                self.store.event(account["id"], "leverage", f"{intent['symbol']} 已核实实际杠杆 {long.leverage}x（目标 {intent['target']}x）；按实际档位准备开仓，禁止降档")
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
        try:
            snapshot.require_modes(account["policy"]["symbols"])
        except AccountModeError as exc:
            return self.attention(account, intent, str(exc))
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
        if not hedge_balanced(long.qty, short.qty):
            if intent["repair_attempts"] >= 3:
                return self.attention(account, intent, "单腿补偿尚未完成，已暂停新开仓；请核对并重试补偿")
            side = "LONG" if long.qty > short.qty else "SHORT"
            book = self.market.book(intent["symbol"])
            book.require_fresh()
            rule = self.market.rules[intent["symbol"]]
            # Bring total holdings back within tolerance without closing baseline holdings.
            room = floor_step(min(abs(net["LONG"] - net["SHORT"]), net[side], rule.max_qty,
                                  book.bid_qty if side == "LONG" else book.ask_qty), rule.step)
            qty = floor_step(min(abs(long.qty - short.qty), room), rule.step)
            other = "SHORT" if side == "LONG" else "LONG"
            # Old holdings may predate today's step. Crossing equality by one step
            # is allowed only when it restores tolerance and stays within this batch.
            if not hedge_balanced(actual[side] - qty, actual[other]) and qty + rule.step <= room \
                    and hedge_balanced(actual[side] - qty - rule.step, actual[other]):
                qty += rule.step
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

        added = any(net.values())
        if added:
            # Keep each side's retained fill; exclude quantities closed by repairs.
            total = sum(notionals[s] * net[s] / filled[s] for s in filled if filled[s])
            self.store.complete_pair(intent, {"long_qty": wire(net["LONG"]), "short_qty": wire(net["SHORT"]), "notional": wire(total)})
        else:
            intent["status"] = "aborted"
            self.store.save_intent(intent)
            self.store.event(account["id"], "order", f"{intent['symbol']} 本批未形成新增双向仓位，订单已核对")
        return "双向批次已核对完成，多空数量差不超过 0.1%" if added else "本批未成交或已完成单腿补偿"
