"""Deterministic paper broker with persistent balances and order receipts."""
from fractions import Fraction
import math
import time

from .exchange import ExchangeError
from .depth import DepthSnapshot
from .models import AccountSnapshot, Book, MIN_OPEN_LEVERAGE, Position, Rules, SYMBOLS, TAKER_FEE_ESTIMATE, TIERS, TradingError, dec, floor_step, maintenance_for, positive, require_non_decreasing_leverage, require_supported_leverage, wire


PAPER_BRACKETS = [{"notionalFloor": "0", "notionalCap": "1000000", "maintMarginRatio": "0.025", "cum": "0", "initialLeverage": 20}]


class PaperOrderAbsent(ExchangeError):
    """A successful ledger read confirms no simulated fill was committed."""


class DemoMarket:
    """Explicitly simulated quotes; never used by a live account."""
    demo = True

    def __init__(self):
        self.assets = dict.fromkeys(SYMBOLS, "USD1")
        self.rules = {s: Rules(s, dec("0.001"), dec("0.01"), dec("0.001"), dec("10000"), dec("5")) for s in SYMBOLS}

    def load_rules(self):
        pass

    def book(self, symbol):
        bid = {"XAUUSD1": dec("4412.01"), "SPCXUSD1": dec("724.18"), "CLUSD1": dec("79.32")}[symbol]
        return Book(bid, bid + dec("0.01"), dec(50), dec(50), bid + dec("0.005"), time.time())

    def capacities(self, symbol, leverages):
        values = {5: "156800", 10: "85000", 20: "32000"}
        return {v: dec(values[next(t for t in TIERS if t >= v)]) for v in leverages if 1 <= v <= 20}

    def depth(self, symbol):
        book = self.book(symbol)
        # Explicit demo liquidity, separate from the paper broker's fill model.
        return DepthSnapshot.from_response({
            "E": int(book.timestamp * 1000),
            "bids": [[wire(book.bid - book.bid * dec(i) / 10000), "100"] for i in range(10)],
            "asks": [[wire(book.ask + book.ask * dec(i) / 10000), "100"] for i in range(10)],
        }, requested_at=book.timestamp)

    def depth_weight(self, symbols):
        return 0


class PaperBroker:
    mode = "paper"

    def __init__(self, account_id, market, store, seed=False):
        self.account_id, self.market, self.store = account_id, market, store
        state = store.get("paper:" + account_id)
        if state is None:
            state = {"wallet": "25000", "positions": {}, "leverages": dict.fromkeys(SYMBOLS, MIN_OPEN_LEVERAGE), "orders": {}}
            for symbol in SYMBOLS:
                for side in ("LONG", "SHORT"):
                    qty = {"XAUUSD1": "5", "SPCXUSD1": "10", "CLUSD1": "30"}[symbol] if seed else "0"
                    state["positions"][symbol + ":" + side] = {"qty": qty, "entry": wire(market.book(symbol).mark) if seed else "0"}
            store.put("paper:" + account_id, state)
        self.state = state

    def snapshot(self, symbols, fresh_modes=False):
        started = time.time()
        positions, maintenance, initial, pnl = [], dec(0), dec(0), dec(0)
        for symbol in SYMBOLS:
            book = self.market.book(symbol)
            for side in ("LONG", "SHORT"):
                row = self.state["positions"][symbol + ":" + side]
                qty, entry = dec(row["qty"]), dec(row["entry"])
                profit = qty * (book.mark - entry) * (1 if side == "LONG" else -1)
                leverage = self.state["leverages"][symbol]
                mm = maintenance_for(qty * book.mark, PAPER_BRACKETS)
                maintenance += mm
                initial += qty * book.mark / leverage
                pnl += profit
                positions.append(Position(symbol, side, qty, entry, book.mark, leverage, profit, maintenance=mm))
        wallet = dec(self.state["wallet"])
        return AccountSnapshot(wallet + pnl, maintenance, wallet + pnl - initial, wallet, pnl, positions, [], True, False, True,
            started, dict.fromkeys(symbols, TAKER_FEE_ESTIMATE), {s: PAPER_BRACKETS for s in symbols})

    def set_leverage(self, symbol, leverage):
        require_supported_leverage(leverage)
        require_non_decreasing_leverage(self.state["leverages"][symbol], leverage)
        state = {**self.state, "leverages": {**self.state["leverages"], symbol: leverage}}
        self.store.put("paper:" + self.account_id, state)
        self.state = state
        return {"symbol": symbol, "leverage": leverage}

    def cycle_snapshot(self, symbols, fresh_modes=False):
        self.reload()
        return self.snapshot(symbols, fresh_modes=fresh_modes)

    def set_cycle_leverage(self, symbol, leverage, *, checked_snapshot=None, before_submit=None):
        if type(leverage) is not int or not 1 <= leverage <= 125:
            raise TradingError("独立循环杠杆必须为 1 至 125 的整数")
        snapshot = self.cycle_snapshot([symbol], fresh_modes=True)
        long, short = snapshot.require_ready(symbol)
        if long.qty or short.qty:
            raise TradingError("独立循环仅允许在所选品种确认空仓时设置杠杆")
        if before_submit is not None:
            before_submit(snapshot)
            long, short = snapshot.require_ready(symbol)
            if long.qty or short.qty:
                raise TradingError("独立循环杠杆提交前必须仍为空仓")
        state = {**self.state, "leverages": {**self.state["leverages"], symbol: leverage}}
        self.store.put("paper:" + self.account_id, state)
        self.state = state
        return {"symbol": symbol, "leverage": leverage}

    def submit(self, orders):
        responses = []
        for order in orders:
            cid, symbol, side = order["newClientOrderId"], order["symbol"], order["positionSide"]
            if cid in self.state["orders"]:
                responses.append(self.state["orders"][cid])
                continue
            book = self.market.book(symbol)
            qty = positive(order["quantity"])
            buy = order["side"] == "BUY"
            price = book.ask if buy else book.bid
            depth = max(dec(0), book.ask_qty if buy else book.bid_qty)
            if order.get("type") == "MARKET":
                # Only the current BBO depth is known. Model a partial market fill
                # and terminate the remainder instead of inventing deeper liquidity.
                executed = floor_step(min(qty, depth), self.market.rules[symbol].step)
            else:
                limit = dec(order["price"])
                fills = (price <= limit if buy else price >= limit) and qty <= depth
                executed = qty if fills else dec(0)
            position = self.state["positions"][symbol + ":" + side].copy()
            old_qty, old_entry = dec(position["qty"]), dec(position["entry"])
            wallet = self.state["wallet"]
            opening = (side == "LONG" and buy) or (side == "SHORT" and not buy)
            if not opening and qty > old_qty:
                executed = dec(0)
            if executed:
                fee = executed * price * TAKER_FEE_ESTIMATE
                pnl = dec(0) if opening else executed * (price - old_entry) * (1 if side == "LONG" else -1)
                wallet = wire(dec(wallet) + pnl - fee)
                next_qty = old_qty + executed if opening else old_qty - executed
                entry = (old_entry * old_qty + price * executed) / next_qty if opening else old_entry
                position.update(qty=wire(next_qty), entry=wire(entry if next_qty else 0))
            receipt = {"symbol": symbol, "clientOrderId": cid, "positionSide": side, "side": order["side"],
                       "status": "FILLED" if executed == qty else "EXPIRED", "executedQty": wire(executed),
                       "origQty": wire(qty), "avgPrice": wire(price if executed else 0)}
            # Fill time and identity are committed with both balance and receipt;
            # restart recovery must not move a fill into a different UTC day.
            executed_at = time.time()
            receipt.update(orderId="paper:" + cid, time=int(executed_at * 1000), updateTime=int(executed_at * 1000),
                           paperTrades=[{"trade_id": "paper:" + cid + ":0", "order_id": "paper:" + cid,
                                         "client_id": cid, "symbol": symbol, "position_side": side, "side": order["side"],
                                         "quantity": wire(executed), "price": wire(price),
                                         "notional": wire(Fraction(executed) * Fraction(price)),
                                         "executed_at": executed_at, "time_source": "paper"}] if executed else [])
            state = {**self.state, "wallet": wallet,
                     "positions": {**self.state["positions"], symbol + ":" + side: position},
                     "orders": {**self.state["orders"], cid: receipt}}
            # A simulated fill exists only once balances and its receipt commit.
            # Keep earlier successful legs while a failed leg remains absent.
            self.store.put("paper:" + self.account_id, state)
            self.state = state
            responses.append(receipt)
        return responses

    def query(self, symbol, client_id):
        if client_id not in self.state["orders"]:
            # A commit may have succeeded before its acknowledgement failed.
            # Only the durable ledger can prove a simulated order was absent.
            self.reload()
        if client_id not in self.state["orders"]:
            raise PaperOrderAbsent("模拟订单未写入账本", code=-2013)
        return self.state["orders"][client_id]

    def cycle_trades(self, order, receipt, created_at, *, checkpoint=None):
        if any(receipt.get(key) != order[key] for key in ("symbol", "positionSide", "side")) \
                or receipt.get("clientOrderId") != order["newClientOrderId"]:
            raise TradingError("循环模拟成交回执身份不一致")
        qty = positive(receipt.get("executedQty"), True)
        if not qty:
            return []
        if "paperTrades" in receipt:
            rows = receipt["paperTrades"]
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise TradingError("模拟逐笔成交账本无效")
            if sum((Fraction(positive(row.get("quantity"))) for row in rows), Fraction(0)) != Fraction(qty):
                raise TradingError("模拟逐笔成交数量与回执不一致")
            return [dict(row) for row in rows]
        # Historical paper receipts have no execution timestamp. Preserve this
        # limitation explicitly, with a stable estimate from the original intent.
        if type(created_at) not in (int, float) or not math.isfinite(created_at) or created_at <= 0:
            raise TradingError("旧模拟成交缺少可用的时间依据")
        cid = order["newClientOrderId"]
        receipt.setdefault("orderId", "paper:" + cid)
        price = positive(receipt.get("avgPrice"))
        return [{"trade_id": "legacy:" + cid, "order_id": str(receipt["orderId"]), "client_id": cid,
                 "symbol": order["symbol"], "position_side": order["positionSide"], "side": order["side"],
                 "quantity": wire(qty), "price": wire(price), "notional": wire(Fraction(qty) * Fraction(price)),
                 "executed_at": created_at, "time_source": "legacy_estimated"}]

    def reload(self):
        state = self.store.get("paper:" + self.account_id)
        if state is None:
            raise TradingError("模拟账户账本不存在，无法确认执行结果")
        self.state = state

    def cancel(self, symbol, client_id):
        return self.query(symbol, client_id)

    def save(self):
        self.store.put("paper:" + self.account_id, self.state)

    def close(self):
        pass
