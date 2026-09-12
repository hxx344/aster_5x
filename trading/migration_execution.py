"""Durable, opening-first XAU migration; recovery submits reductions only."""
from copy import deepcopy
from fractions import Fraction
import time
import uuid

from .exchange import ExchangeError, RequestNotSent
from .execution import Executor, TERMINAL
from .models import MIN_BATCH_NOTIONAL, SYMBOLS, TradingError, dec, floor_step, hedge_balanced, positive, require_supported_leverage, wire
from .paper import PaperBroker, PaperOrderAbsent


SIDES = ("LONG", "SHORT")
PURPOSES = {"open_target", "balance_target", "close_source", "balance_source", "trim_target"}
MAX_REDUCTION_ATTEMPTS = 3


class MigrationExecutor(Executor):
    def start(self, account, snapshot, plan, run_id=None, before_submit=None):
        self.last_snapshot = self.last_completed_intent = None
        if self.store.intent(account["id"]):
            raise TradingError("已有批次正在执行")
        if not account.get("enabled") or not account.get("migration", {}).get("enabled"):
            raise TradingError("迁移开关或账户策略未启动")
        source, target = plan.source_symbol, plan.target_symbol
        if source != "XAUUSD1" or target not in ("SPCXUSD1", "CLUSD1"):
            raise TradingError("迁移仅允许 XAU 转入 SPCX 或 CL")
        symbols = list(dict.fromkeys([*account["policy"]["symbols"], *SYMBOLS]))
        selected = snapshot
        snapshot = self.broker.snapshot(symbols, fresh_modes=True)
        snapshot.require_modes(symbols)
        baseline, leverages = {}, {}
        for symbol in (source, target):
            pair = snapshot.require_ready(symbol)
            old_pair = selected.require_ready(symbol)
            if any(p.qty != old.qty or p.leverage != old.leverage for p, old in zip(pair, old_pair)):
                raise TradingError("迁移规划后的仓位或杠杆已变化，等待重新规划")
            if not hedge_balanced(pair[0].qty, pair[1].qty):
                raise TradingError("迁移前多空数量差超过 0.1%")
            baseline[symbol] = {s: wire(p.qty) for s, p in zip(SIDES, pair)}
            leverages[symbol] = pair[0].leverage
        if leverages[source] != plan.source_leverage or leverages[target] != plan.target_leverage:
            raise TradingError("迁移实际杠杆与规划不一致")
        require_supported_leverage(plan.target_leverage)
        if plan.target_leverage < plan.source_leverage:
            raise TradingError("迁入标的杠杆不得低于 XAU 原杠杆")
        books = {s: self.market.book(s) for s in (source, target)}
        for book in books.values():
            book.require_fresh()
        qty = positive(plan.target_qty)
        self._quantity(target, qty, opening=True, book=books[target])
        if qty * books[target].mark < MIN_BATCH_NOTIONAL and not getattr(plan, "is_tail", False):
            raise TradingError("普通迁移批次每边至少 500 USD1，只有尾批规划可例外")
        source_budget = {}
        for side in SIDES:
            value = positive(plan.source_quantities[side])
            self._quantity(source, value, opening=False)
            if value > dec(baseline[source][side]):
                raise TradingError("迁出数量超过实际 XAU 仓位")
            source_budget[side] = wire(value)
        tolerance = positive(plan.notional_tolerance, True)
        if tolerance >= 1:
            raise TradingError("迁移金额误差须小于 100%")
        if before_submit is not None:
            before_submit(snapshot)
        snapshot.require_fresh()
        for book in books.values():
            book.require_fresh()
        token = uuid.uuid4().hex
        orders = [self.order(target, s, "BUY" if s == "LONG" else "SELL", qty,
                             "M" + s[0] + token[:26]) for s in SIDES]
        progress = self.store.get("migration:" + account["id"], {}) or {}
        intent = {"id": token, "kind": "migration", "version": 1, "account_id": account["id"],
                  "symbol": target, "source_symbol": source, "target_symbol": target,
                  "run_id": run_id or progress.get("run_id") or token,
                  "status": "pending", "phase": "open_target", "created_at": time.time(),
                  "phase_started_at": time.time(), "baseline": baseline, "leverages": leverages,
                  "leverage": plan.target_leverage, "source_leverage": plan.source_leverage,
                  "target_leverage": plan.target_leverage, "source_close_budget": source_budget,
                  "target_open_budget": wire(qty), "notional_tolerance": wire(tolerance),
                  "policy_snapshot": deepcopy(account.get("migration", {})),
                  "progress_source": deepcopy(progress.get("migrated_notional", dict.fromkeys(SIDES, "0"))),
                  "progress_delta": deepcopy(progress.get("cumulative_notional_delta", dict.fromkeys(SIDES, "0"))),
                  "orders": deepcopy(orders), "order_roles": {o["newClientOrderId"]: "open_target" for o in orders},
                  "order_times": {o["newClientOrderId"]: time.time() for o in orders},
                  "receipts": {}, "repairs": [], "repair_attempts": 0, "attempts": {}, "reduction_batches": []}
        self.store.save_intent(intent)
        self.store.event(account["id"], "migration", f"XAU → {target} 迁移批次：先开目标多空，再核实并减持 XAU")
        self.send(intent, orders)
        return self.reconcile(account, intent)

    def _quantity(self, symbol, qty, *, opening=False, book=None):
        rule = self.market.rules[symbol]
        qty = positive(qty)
        if qty != floor_step(qty, rule.step) or not rule.min_qty <= qty <= rule.max_qty:
            raise TradingError("迁移委托数量不符合交易所规则")
        # Tail migrations may be below the application's ordinary 500 USD1 floor.
        if opening and qty * book.mark < rule.min_notional:
            raise TradingError("迁入金额低于交易所最小开仓金额")
        return qty

    def _resolve(self, account, intent):
        unresolved = []
        for order in intent["orders"]:
            cid = order["newClientOrderId"]
            row = intent["receipts"].get(cid)
            if row and row.get("status") in TERMINAL:
                self.validate_receipt(order, row)
                continue
            try:
                row = self.broker.query(order["symbol"], cid)
                self.validate_receipt(order, row)
                intent["receipts"][cid] = row
                if row["status"] not in TERMINAL:
                    unresolved.append(cid)
                    if time.time() - intent["order_times"][cid] > 10:
                        self.broker.cancel(order["symbol"], cid)
            except ExchangeError as exc:
                if isinstance(self.broker, PaperBroker) and isinstance(exc, PaperOrderAbsent):
                    intent["receipts"][cid] = {**order, "clientOrderId": cid, "status": "REJECTED",
                                               "executedQty": "0", "avgPrice": "0", "paper_not_committed": True}
                else:
                    intent["last_error"] = str(exc)
                    unresolved.append(cid)
        # Recover the refund even if the process stopped after send() committed
        # known-absent receipts but before _reduce committed the retry refund.
        for batch in intent.get("reduction_batches", []):
            rows = [intent["receipts"].get(cid, {}) for cid in batch["cids"]]
            if batch.get("counted") and all(row.get("local_not_sent") or row.get("paper_not_committed") for row in rows):
                role = batch["role"]
                intent["attempts"][role] = max(0, intent["attempts"].get(role, 0) - 1)
                batch["counted"] = False
        self.store.save_intent(intent)
        if unresolved:
            if any(time.time() - intent["order_times"][cid] > 120 for cid in unresolved):
                return self.attention(account, intent, "迁移订单结果仍不确定，暂停等待核对；不会重复提交")
            return "正在核对迁移订单回执，不重复提交"
        return None

    @staticmethod
    def _ledger(intent):
        source, target = intent["source_symbol"], intent["target_symbol"]
        opened, reduced, closed = ({s: Fraction(0) for s in SIDES} for _ in range(3))
        opened_notional, closed_notional = ({s: Fraction(0) for s in SIDES} for _ in range(2))
        for order in intent["orders"]:
            cid, side = order["newClientOrderId"], order["positionSide"]
            role = intent["order_roles"][cid]
            if side not in SIDES or role not in PURPOSES:
                raise TradingError("迁移委托用途无效")
            opening = role == "open_target"
            expected_symbol = source if role in ("close_source", "balance_source") else target
            expected_side = ("BUY" if side == "LONG" else "SELL") if opening else ("SELL" if side == "LONG" else "BUY")
            if order["symbol"] != expected_symbol or order["side"] != expected_side:
                raise TradingError("迁移委托超出既定增减仓方向")
            row = intent["receipts"][cid]
            qty = Fraction(dec(row["executedQty"]))
            amount = qty * Fraction(dec(row["avgPrice"])) if qty else Fraction(0)
            if opening:
                opened[side] += qty
                opened_notional[side] += amount
            elif order["symbol"] == target:
                reduced[side] += qty
            else:
                closed[side] += qty
                closed_notional[side] += amount
        net = {s: opened[s] - reduced[s] for s in SIDES}
        if any(net[s] < 0 or opened[s] > Fraction(dec(intent["target_open_budget"]))
               or closed[s] > Fraction(dec(intent["source_close_budget"][s])) for s in SIDES):
            raise TradingError("迁移成交超出冻结预算，暂停核对")
        average = {s: opened_notional[s] / opened[s] if opened[s] else Fraction(0) for s in SIDES}
        retained_notional = {s: net[s] * average[s] for s in SIDES}
        return {"target_qty": net, "source_qty": closed, "target_notional": retained_notional,
                "source_notional": closed_notional, "target_average": average}

    def _snapshot(self, account, intent, ledger):
        source, target = intent["source_symbol"], intent["target_symbol"]
        symbols = list(dict.fromkeys([*account["policy"]["symbols"], *SYMBOLS]))
        if isinstance(self.broker, PaperBroker):
            self.broker.reload()
        snapshot = self.broker.snapshot(symbols)
        snapshot.require_modes(symbols)
        for symbol in (source, target):
            pair = snapshot.require_ready(symbol)
            for side, position in zip(SIDES, pair):
                delta = ledger["target_qty"][side] if symbol == target else -ledger["source_qty"][side]
                if Fraction(position.qty) != Fraction(dec(intent["baseline"][symbol][side])) + delta:
                    raise TradingError("迁移持仓变化与回执不一致，可能有外部成交或 ADL")
                if position.leverage != intent["leverages"][symbol]:
                    raise TradingError("迁移期间实际杠杆发生变化，暂停核对")
        return snapshot

    def _advance(self, intent, phase):
        intent.update(phase=phase, phase_started_at=time.time())
        self.store.save_intent(intent)

    def _reduce(self, account, intent, snapshot, ledger, role, quantities):
        symbol = intent["source_symbol"] if role in ("close_source", "balance_source") else intent["target_symbol"]
        count = intent["attempts"].get(role, 0)
        if count >= MAX_REDUCTION_ATTEMPTS:
            return self.attention(account, intent, "迁移减仓修复达到重试上限，暂停等待核对")
        book = self.market.book(symbol)
        book.require_fresh()
        snapshot.require_fresh()
        rule, orders = self.market.rules[symbol], []
        for side in SIDES:
            requested = Fraction(quantities.get(side, 0))
            if requested <= 0:
                continue
            remaining = (Fraction(dec(intent["source_close_budget"][side])) - ledger["source_qty"][side]
                         if symbol == intent["source_symbol"] else ledger["target_qty"][side])
            actual, = (p for p in snapshot.pair(symbol) if p.side == side)
            amount = floor_step(min(requested, remaining, Fraction(actual.qty), Fraction(rule.max_qty),
                                    Fraction(book.bid_qty if side == "LONG" else book.ask_qty)), rule.step)
            if amount < rule.min_qty:
                return self.attention(account, intent, "迁移剩余修复量低于最小数量或盘口不足，暂停核对")
            self._quantity(symbol, amount)
            orders.append(self.order(symbol, side, "SELL" if side == "LONG" else "BUY", amount,
                                     "MR" + uuid.uuid4().hex[:26]))
        if not orders:
            return self.attention(account, intent, "迁移无法形成合法的剩余减仓委托")
        for order in orders:
            cid = order["newClientOrderId"]
            intent["orders"].append(order)
            intent["order_roles"][cid] = role
            intent["order_times"][cid] = time.time()
        intent["attempts"][role] = count + 1
        batch = {"role": role, "cids": [o["newClientOrderId"] for o in orders], "counted": True}
        intent.setdefault("reduction_batches", []).append(batch)
        intent["status"] = "repair" if role != "close_source" else "pending"
        self.store.save_intent(intent)
        self.store.event(account["id"], "migration", f"迁移减仓核对：{symbol}，阶段 {role}")
        not_sent = self.send(intent, orders)
        if isinstance(not_sent, RequestNotSent):
            intent["attempts"][role] = count
            batch["counted"] = False
            self.store.save_intent(intent)
            raise not_sent
        return None

    @staticmethod
    def _acceptable(intent, ledger):
        tolerance = Fraction(dec(intent["notional_tolerance"]))
        for side in SIDES:
            source, target = ledger["source_notional"][side], ledger["target_notional"][side]
            if abs(target - source) > source * tolerance:
                return False
            past_source = Fraction(dec(intent["progress_source"].get(side, "0")))
            past_delta = Fraction(dec(intent["progress_delta"].get(side, "0")))
            if abs(past_delta + target - source) > (past_source + source) * tolerance:
                return False
        return True

    def _trim_quantities(self, intent, ledger):
        """Find an attainable retained target quantity meeting both error budgets."""
        tolerance = Fraction(dec(intent["notional_tolerance"]))
        lower, upper, preferred = Fraction(0), min(ledger["target_qty"].values()), []
        for side in SIDES:
            source = ledger["source_notional"][side]
            avg = ledger["target_average"][side]
            past_source = Fraction(dec(intent["progress_source"].get(side, "0")))
            past_delta = Fraction(dec(intent["progress_delta"].get(side, "0")))
            low_amount = max(Fraction(0), source * (1 - tolerance), source - past_delta - (past_source + source) * tolerance)
            high_amount = min(source * (1 + tolerance), source - past_delta + (past_source + source) * tolerance)
            if not avg:
                if source:
                    raise TradingError("目标成交量不足以覆盖已平 XAU，暂停核对")
                upper = Fraction(0)
                continue
            lower, upper = max(lower, low_amount / avg), min(upper, high_amount / avg)
            preferred.append(max(Fraction(0), source - past_delta) / avg)
        step = self.market.rules[intent["target_symbol"]].step
        chosen = min([upper, *preferred]) if preferred else Fraction(0)
        chosen = Fraction(floor_step(max(Fraction(0), chosen), step))
        if chosen < lower:
            chosen = Fraction(floor_step(upper, step))
        if upper < lower or chosen < lower or chosen < 0:
            raise TradingError("已成交金额无法在误差范围内通过减仓匹配，暂停核对")
        return {s: ledger["target_qty"][s] - chosen for s in SIDES}

    def _source_plan(self, intent, ledger, snapshot):
        source = intent["source_symbol"]
        rule, book = self.market.rules[source], self.market.book(source)
        book.require_fresh()
        snapshot.require_fresh()
        prices = {"LONG": Fraction(book.bid), "SHORT": Fraction(book.ask)}
        tolerance = Fraction(dec(intent["notional_tolerance"]))
        # A complete tail uses its two actual quantities, including an old
        # tolerated quantity difference, rather than leaving a one-sided dust lot.
        full = {s: Fraction(dec(intent["source_close_budget"][s])) for s in SIDES}
        tail = all(full[s] == Fraction(dec(intent["baseline"][source][s])) for s in SIDES)
        if tail and all(abs(ledger["target_notional"][s] - full[s] * prices[s])
                        <= full[s] * prices[s] * tolerance for s in SIDES):
            return {s: wire(full[s]) for s in SIDES}
        maximum = min([*full.values(), *(ledger["target_notional"][s] / prices[s] for s in SIDES)])
        qty = floor_step(maximum, rule.step)
        if qty < rule.min_qty:
            return dict.fromkeys(SIDES, "0")
        return dict.fromkeys(SIDES, wire(qty))

    def _finish(self, account, intent, snapshot, ledger):
        source, target = intent["source_symbol"], intent["target_symbol"]
        for symbol in (source, target):
            long, short = snapshot.pair(symbol)
            if not hedge_balanced(long.qty, short.qty):
                raise TradingError("迁移收尾时多空数量差超过 0.1%，暂停核对")
        if not self._acceptable(intent, ledger):
            raise TradingError("迁移实际成交金额超出允许误差，暂停核对")
        intent["status"] = "complete" if any(ledger["source_qty"].values()) else "aborted"
        result = {name: {s: wire(ledger[name][s]) for s in SIDES}
                  for name in ("source_notional", "target_notional", "source_qty", "target_qty")}
        remaining = {s: wire(p.qty) for s, p in zip(SIDES, snapshot.pair(source))}
        self.store.complete_migration(intent, result, remaining)
        self.last_snapshot, self.last_completed_intent = snapshot, deepcopy(intent)
        return "本批 XAU 迁移已完成并核对" if intent["status"] == "complete" else "迁移未形成有效成交，已回退本批目标新增仓位"

    def _reconcile(self, account, intent=None):
        intent = intent or self.store.intent(account["id"])
        if not intent:
            return "没有未完成迁移批次"
        if intent.get("kind") != "migration" or intent.get("version") != 1:
            return self.attention(account, intent, "迁移批次类型或版本无效")
        try:
            # A normal full fill takes four iterations. Partial reductions have
            # independent bounded counters and resume in a later engine tick.
            for _ in range(12):
                pending = self._resolve(account, intent)
                if pending:
                    return pending
                ledger = self._ledger(intent)
                snapshot = self._snapshot(account, intent, ledger)
                phase = intent["phase"]
                if phase in ("open_target", "balance_target"):
                    kept = ledger["target_qty"]
                    matched = min(kept.values())
                    if kept["LONG"] != kept["SHORT"]:
                        self._advance(intent, "balance_target")
                        reason = self._reduce(account, intent, snapshot, ledger, "balance_target",
                                              {s: kept[s] - matched for s in SIDES})
                        if reason:
                            return reason
                        continue
                    if not matched:
                        return self._finish(account, intent, snapshot, ledger)
                    intent["planned_source_close"] = self._source_plan(intent, ledger, snapshot)
                    if not any(dec(q) for q in intent["planned_source_close"].values()):
                        self._advance(intent, "trim_target")
                        continue
                    self._advance(intent, "close_source")
                    reason = self._reduce(account, intent, snapshot, ledger, "close_source",
                                          {s: Fraction(dec(intent["planned_source_close"][s])) for s in SIDES})
                    if reason:
                        return reason
                    continue
                if phase in ("close_source", "balance_source"):
                    pair = snapshot.pair(intent["source_symbol"])
                    planned = {s: Fraction(dec(intent["planned_source_close"][s])) for s in SIDES}
                    full_tail = all(planned[s] == Fraction(dec(intent["baseline"][intent["source_symbol"]][s])) for s in SIDES)
                    closed = ledger["source_qty"]
                    amounts = {}
                    if full_tail and any(closed.values()):
                        amounts = {s: planned[s] - closed[s] for s in SIDES if planned[s] > closed[s]}
                    elif closed["LONG"] != closed["SHORT"]:
                        matched = max(closed.values())
                        amounts = {s: min(matched - closed[s], planned[s] - closed[s])
                                   for s in SIDES if matched > closed[s]}
                    if not amounts and not hedge_balanced(pair[0].qty, pair[1].qty):
                        # Close the larger remaining source side, never reopen
                        # XAU or exceed the quantity frozen before source writes.
                        side = "LONG" if pair[0].qty > pair[1].qty else "SHORT"
                        amount = Fraction(abs(pair[0].qty - pair[1].qty))
                        room = Fraction(dec(intent["planned_source_close"][side])) - ledger["source_qty"][side]
                        amount = min(amount, room)
                        if amount <= 0:
                            raise TradingError("XAU 剩余单腿无法在本批迁出预算内修复")
                        amounts = {side: amount}
                    if amounts:
                        self._advance(intent, "balance_source")
                        reason = self._reduce(account, intent, snapshot, ledger, "balance_source", amounts)
                        if reason:
                            return reason
                        continue
                    self._advance(intent, "trim_target")
                    continue
                if phase == "trim_target":
                    target_pair = snapshot.pair(intent["target_symbol"])
                    if self._acceptable(intent, ledger) and hedge_balanced(target_pair[0].qty, target_pair[1].qty):
                        return self._finish(account, intent, snapshot, ledger)
                    quantities = self._trim_quantities(intent, ledger)
                    reason = self._reduce(account, intent, snapshot, ledger, "trim_target", quantities)
                    if reason:
                        return reason
                    continue
                raise TradingError("迁移批次阶段无效")
            return "迁移正在分步核对，等待下一轮继续减仓收尾"
        except RequestNotSent:
            raise
        except TradingError as exc:
            return self.attention(account, intent, str(exc))
