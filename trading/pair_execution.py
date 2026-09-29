"""Durable two-subaccount execution. Recovery never repeats an uncertain write."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, nullcontext
from copy import deepcopy
from datetime import datetime, timezone
from fractions import Fraction
import math
import time
import uuid

from .exchange import (AmbiguousOrder, ExchangeError, LiveBroker, RequestNotSent,
                       LeverageRejected, LEVERAGE_REJECTION_CODES, ORDER_REJECTION_CODES, api_wait_notice)
from .exchange_messages import exchange_reason, MISSING_REJECT_REASON
from .execution import Executor, TERMINAL
from .models import TradingError, dec, positive, wire, cycle_margin_limit, opening_margin_limit
from .paper import PaperOrderAbsent
from .pair_planning import (SYMBOL, SIDES, PairPositionError, positions, require_quantities,
                            plan_ordinary, plan_paired_cycle, ordinary_upgrade)
from .store import dumps


def empty_progress(owned=None, completed=0):
    return {"phase": "waiting_open", "baseline": dict(owned or {"LONG": "0", "SHORT": "0"}),
            "quantities": {"LONG": "0", "SHORT": "0"}, "completed_cycles": completed}


def runtime_default():
    return {"phase": "paused", "reason": "配对组尚未启动；核对两侧账户及策略设置后，点击启动才开始执行", "owned": {"LONG": "0", "SHORT": "0"},
            "progress": empty_progress(), "pending": None, "snapshots": {}, "daily_volume": {}}


class PairTrader:
    def __init__(self, engine):
        self.engine, self.store, self.market = engine, engine.store, engine.market
        self._ordinary_observations = {}
        self._margin_observations = {}

    def _margin_wait(self, pair, state, identities, brokers, margin_state):
        """Delay display-only reads while no balance check can consume them."""
        if (not pair["enabled"] or pair["ordinary"]["enabled"] or pair["cycle"]["enabled"]
                or not pair["margin"]["enabled"] or state.get("attention")):
            return False
        if time.time() >= max(margin_state.get("next_check_at", 0), margin_state.get("cooldown_until", 0)):
            return False
        observed = self._margin_observations.get(pair["id"])
        if not observed or observed[0] != (pair["revision"], identities):
            return False
        generations, started = observed[1:]
        if not 0 <= time.monotonic() - started < 30:
            return False
        # These generations only determine when to refresh the display. No old
        # snapshot from this observation can authorize a transfer or order.
        return all(generations[key] == (id(broker), getattr(broker, "_snapshot_generation", None))
                   for key, broker in brokers.items())

    def _ordinary_wait(self, pair, state, identities, margin_state):
        """Reuse recent reads only to reject work, never to authorize a write."""
        if not pair["enabled"] or not pair["ordinary"]["enabled"] or state.get("attention"):
            return None
        if pair["margin"]["enabled"] and time.time() >= max(
                margin_state.get("next_check_at", 0), margin_state.get("cooldown_until", 0)):
            return None
        observed = self._ordinary_observations.get(pair["id"])
        if not observed or observed[0] != (pair["revision"], identities):
            return None
        snapshots, guards, started = observed[1:]
        interval = min(5, pair["margin"]["check_interval_seconds"]) if pair["margin"]["enabled"] else 5
        if not 0 <= time.monotonic() - started < interval:
            return None
        # A slow read has already consumed part of the eight-second freshness
        # window. Start the observation interval at the source read, not its end.
        if any(not 0 <= time.time() - snapshot.timestamp < interval for snapshot in snapshots.values()):
            return None
        try:
            for guard in guards.values():
                guard()
            require_quantities(snapshots, self._expected(state))
        except TradingError:
            return None
        try:
            capacities = self.engine.capacities(SYMBOL)
            book = self.market.book(SYMBOL)
            book.require_fresh()
            if ordinary_upgrade(pair, snapshots, book, capacities) is not None:
                return None
            plan_ordinary(pair, snapshots, book, self.market.rules[SYMBOL], capacities)
        except PairPositionError:
            return None
        except TradingError as exc:
            return str(exc)
        return None

    def _save(self, pair, state):
        state["updated_at"] = time.time()
        self.store.put("pair_runtime:" + pair["id"], self._durable(state))

    @staticmethod
    def _durable(state):
        # This relative delay belongs to this scheduler turn, not a restarted one.
        return {key: value for key, value in state.items() if key != "retry_after"}

    @staticmethod
    def _retry_delay(state, exc):
        delay = getattr(exc, "retry_after", 0)
        if type(delay) in (int, float) and math.isfinite(delay) and delay > 0:
            state["retry_after"] = max(state.get("retry_after", 0), delay)

    def _members(self, pair):
        accounts, brokers, identities = {}, {}, {}
        for key, side in SIDES:
            account = self.store.account(pair[key + "_account_id"])
            if not account or not self.engine.live_allowed(account):
                raise PairPositionError("配对组子账户不存在或实盘执行未启用")
            broker = self.engine.broker(account)
            accounts[key], brokers[key] = account, broker
            identity = {"account_id": account["id"], "env_prefix": account["env_prefix"], "mode": account["mode"], "side": side}
            if isinstance(broker, LiveBroker):
                credentials = broker.api.credentials
                identity.update(user=credentials["user"].lower(), signer=credentials["signer"].lower())
            identities[key] = identity
        if accounts["long"]["mode"] != accounts["short"]["mode"]:
            raise PairPositionError("配对组两个子账户必须使用同一运行模式")
        if identities["long"].get("user") and identities["long"]["user"] == identities["short"].get("user"):
            raise PairPositionError("两个方向不能使用同一个真实账户")
        return accounts, brokers, identities

    def _read(self, brokers, *, hot=False, reconciliation=False):
        if not hot:
            budgets = {}
            for broker in brokers.values():
                if isinstance(broker, LiveBroker) and getattr(broker.api, "budget", None) is not None:
                    budget = broker.api.budget
                    budgets[budget] = budgets.get(budget, 0) + broker.snapshot_weight(
                        [SYMBOL], fresh_modes=reconciliation, reuse_account_mode=not reconciliation)
            # Check the complete shared-budget read before either worker starts;
            # actual requests still perform their own atomic admission checks.
            for budget, weight in budgets.items():
                with budget.reconciliation() if reconciliation else budget.cycle_accounting():
                    budget.require_available(weight)
        def read(key):
            broker = brokers[key]
            # Budget priority is thread-local: establish it inside each worker.
            budget = getattr(broker, "reconciliation_budget", nullcontext)() if reconciliation else nullcontext()
            with budget:
                if hot and isinstance(broker, LiveBroker):
                    lease = broker.cycle_hot_snapshot([SYMBOL])
                    lease.require_fresh()
                    return lease.snapshot, lease.require_fresh
                # Ordinary polling retains the broker's mode TTL; account events
                # still revoke these reads and mode changes clear its cache.
                options = {"reuse_account_mode": True} if isinstance(broker, LiveBroker) else {}
                snapshot = broker.snapshot([SYMBOL], fresh_modes=reconciliation, **options)
                guard = (lambda: broker.require_snapshot_current(snapshot)) if isinstance(broker, LiveBroker) else snapshot.require_fresh
                return snapshot, guard
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="pair-read") as pool:
            futures = {key: pool.submit(read, key) for key, _ in SIDES}
            rows = {key: future.result() for key, future in futures.items()}
        for _, guard in rows.values():
            guard()
        return {key: row[0] for key, row in rows.items()}, {key: row[1] for key, row in rows.items()}

    @contextmanager
    def _confirmed(self, pair, brokers, guards):
        """Keep known account events outside the local recovery commit."""
        with ExitStack() as stack:
            for key in sorted(brokers, key=lambda key: pair[key + "_account_id"]):
                if isinstance(brokers[key], LiveBroker):
                    stack.enter_context(brokers[key]._snapshot_lock)
            for guard in guards.values():
                guard()
            yield

    def _publish_snapshots(self, state, snapshots):
        from .engine import snapshot_json
        state["snapshots"] = {key: snapshot_json(value, [SYMBOL]) for key, value in snapshots.items()}

    @staticmethod
    def _expected(state):
        progress = state["progress"]
        return {side: wire(dec(state["owned"][side]) + dec(progress["quantities"].get(side, "0"))) for _, side in SIDES}

    def _config_guard(self, pair, identities, *, opening):
        current = self.store.pair(pair["id"])
        if not current or current.get("revision") != pair.get("revision"):
            raise RequestNotSent("配对组配置已变化，取消本次提交")
        if opening and not current["enabled"]:
            raise RequestNotSent("配对组已暂停，禁止新开仓")
        _, _, actual = self._members(current)
        if actual != identities:
            raise PairPositionError("配对组账户身份发生变化，禁止写入")
        if opening:
            from .pair_recovery import require_archived_orders_clear
            require_archived_orders_clear(self.engine, current)

    def tick(self, pair):
        state = {**runtime_default(), **(self.store.get("pair_runtime:" + pair["id"]) or {})}
        state.pop("retry_after", None)
        state["api_notice"] = None
        if not isinstance(state.get("progress"), dict):
            state["progress"] = empty_progress(state["owned"])
        try:
            accounts, brokers, identities = self._members(pair)
            if state.get("identities") and state["identities"] != identities:
                raise PairPositionError("配对组凭据指向的真实账户发生变化，等待人工核对")
            state["identities"] = identities
            pending = state.get("pending")
            if pending:
                if pending["identities"] != identities:
                    raise PairPositionError("未完成批次的账户身份与当前凭据不一致")
                self._recover(pair, state, brokers)
                return state
            holding = any(dec(qty) for qty in state["progress"]["quantities"].values())
            if state.get("volume_unknown_until_utc"):
                state["volume_unknown"] = datetime.now(timezone.utc).date().isoformat() <= state["volume_unknown_until_utc"]
            margin_state = self.store.get("pair_margin:" + pair["id"], {})
            if margin_state is None:
                margin_state = {}
            margin_invalid = not isinstance(margin_state, dict) or any(
                field in margin_state and (type(margin_state[field]) not in (int, float)
                    or not math.isfinite(margin_state[field]))
                for field in ("checked_at", "next_check_at", "cooldown_until"))
            margin_pending = margin_state.get("pending") if isinstance(margin_state, dict) else None
            from .margin_balance import MarginBalancer
            if (not holding and margin_pending is None and not margin_invalid
                    and margin_state.get("api_notice")
                    and time.time() < margin_state.get("next_check_at", 0)):
                # A failed balance preparation blocks new work. Re-reading both
                # accounts during its quota backoff cannot authorize anything.
                margin = MarginBalancer.status_view(pair, margin_state, state.get("margin"))
                if margin.get("blocks_trading"):
                    state.update(margin=margin, phase="margin_wait", reason=margin["reason"],
                                 retry_after=max(0, margin_state["next_check_at"] - time.time()))
                    return state
            if not holding and (margin_pending is not None or margin_invalid):
                # Pending transfers consume only their own reconciliation reads.
                # Even a completed reconciliation ends this turn: subsequent
                # trading must obtain a new account read, not an older wait hint.
                self._ordinary_observations.pop(pair["id"], None)
                self._margin_observations.pop(pair["id"], None)
                margin = MarginBalancer(self.engine).tick(pair, {})
                state.update(margin=margin, phase="margin_wait", reason=margin.get("reason", "划转核对中"))
                if margin.get("api_notice"):
                    state["retry_after"] = max(0, margin.get("retry_after", 0))
                return state
            if not pair["enabled"] and not holding and not margin_pending:
                state.update(phase="paused", reason=pair.get("pause_reason") or "配对组已暂停，不开始新开仓或新划转；已有普通策略底仓保留，需点击启动后重新检查执行条件")
                return state
            if not holding and self._margin_wait(pair, state, identities, brokers, margin_state):
                # Project the journal without invoking a funds workflow. A
                # deadline crossing here cannot create a transfer with no read.
                margin = MarginBalancer.status_view(pair, margin_state, state.get("margin"))
                state.update(margin=margin, phase="margin_wait" if margin.get("blocks_trading") else "monitoring",
                             reason=margin.get("reason", "等待下一次保证金检查"))
                return state
            if not holding and not margin_pending:
                reason = self._ordinary_wait(pair, state, identities, margin_state)
                if reason:
                    margin = state.get("margin") or {}
                    state.update(phase="margin_wait" if margin.get("blocks_trading") else "waiting",
                                 reason=margin.get("reason", reason) if margin.get("blocks_trading") else reason)
                    return state
            closing_due = holding and (not pair["enabled"] or time.time() >=
                state["progress"]["opened_at"] + state["progress"]["config"]["hold_seconds"])
            # A due reduction must not depend on a hot refresh that uses ordinary
            # quota. Read fresh positions with the repair reserve when closing.
            hot = not closing_due and bool(pair["enabled"] and pair["cycle"]["enabled"] or holding)
            snapshots, guards = self._read(brokers, hot=hot, reconciliation=closing_due)
            self._publish_snapshots(state, snapshots)
            held = require_quantities(snapshots, self._expected(state))
            if not pair["ordinary"]["enabled"] and not pair["cycle"]["enabled"] and not holding:
                if len(self._margin_observations) >= 16:
                    self._margin_observations.clear()
                self._margin_observations[pair["id"]] = (
                    (pair["revision"], deepcopy(identities)),
                    {key: (id(broker), getattr(snapshots[key], "account_read_generation", None))
                     for key, broker in brokers.items()}, time.monotonic())
            if (not hot and not closing_due and pair["ordinary"]["enabled"]
                    and all(isinstance(broker, LiveBroker) for broker in brokers.values())):
                if len(self._ordinary_observations) >= 16:
                    self._ordinary_observations.clear()
                self._ordinary_observations[pair["id"]] = (
                    (pair["revision"], deepcopy(identities)), snapshots, guards, time.monotonic())
            # The independent balancer uses the same ordered account locks. A
            # transfer invalidates both read leases before any following order.
            if pair["margin"]["enabled"] and not holding:
                # Publish the completed read before potentially slow transfer
                # preparation; this never changes the original snapshot time.
                self._save(pair, state)
            margin = MarginBalancer(self.engine).tick(pair, snapshots, pending_orders=closing_due)
            state["margin"] = margin
            if margin.get("blocks_trading") and not holding:
                state.update(phase="margin_wait", reason=margin.get("reason", "划转核对中"))
                if margin.get("api_notice"):
                    state["retry_after"] = max(0, margin.get("retry_after", 0))
                return state
            if not pair["enabled"] and not holding:
                state.update(phase="paused", reason=pair.get("pause_reason") or "配对组已暂停，不开始新开仓或新划转；已有普通策略底仓保留，需点击启动后重新检查执行条件")
                return state
            if state.get("attention") and not holding:
                state.update(phase="attention", reason=state["attention"])
                return state
            if not holding and time.time() < state.get("retry_at", 0):
                state.update(phase="waiting", reason="上批未完整成交，已减回原始基线；当前处于开仓冷却期，结束后系统重新检查开仓条件")
                return state
            book = self.engine.cycle_book(SYMBOL) if pair["cycle"]["enabled"] or holding else self.market.book(SYMBOL)
            rule = self.market.rules[SYMBOL]
            if holding or pair["cycle"]["enabled"]:
                capacity = None
                if not holding:
                    capacity = self.engine.require_cycle_open_capacity(pair["cycle"], held["LONG"].leverage)
                depth = self.engine.cycle_depth(SYMBOL)
                remaining = self._daily_remaining(pair, state) if not holding else None
                plan = plan_paired_cycle(pair, snapshots, book, depth, rule, state["progress"],
                                         capacity=capacity, daily_remaining=remaining, paused=not pair["enabled"])
                self._start(pair, state, brokers, snapshots, guards, plan, kind="cycle")
            elif pair["ordinary"]["enabled"]:
                capacities = self.engine.capacities(SYMBOL)
                target = ordinary_upgrade(pair, snapshots, book, capacities)
                if target is not None:
                    self._leverage(pair, state, brokers, snapshots, guards, target)
                else:
                    plan = plan_ordinary(pair, snapshots, book, rule, capacities)
                    self._start(pair, state, brokers, snapshots, guards, plan, kind="ordinary")
            else:
                state.update(phase="monitoring", reason="未启用开仓模式，仅管理保证金；"
                             + ("自动平衡已开启，满足余额差额及安全可划条件时会划转"
                                if pair["margin"]["enabled"] else "自动平衡未开启，不发起新划转"))
        except PairPositionError as exc:
            state.update(phase="attention", reason=str(exc), attention=str(exc))
        except TradingError as exc:
            self._retry_delay(state, exc)
            state["api_notice"] = api_wait_notice(exc)
            state.update(phase="reconciling" if state.get("pending") else
                         "holding" if any(dec(q) for q in state["progress"]["quantities"].values()) else "waiting",
                         reason=str(exc))
        finally:
            self._save(pair, state)
        return state

    @staticmethod
    def _daily_remaining(pair, state):
        limit = dec(pair["cycle"]["daily_volume_limit"])
        if not limit:
            return None
        if state.get("volume_unknown"):
            raise TradingError("配对组成交时间或金额尚未核实，当日剩余成交额度无法确认；暂停新循环开仓，已有本轮仓位仍可减回")
        date = datetime.now(timezone.utc).date().isoformat()
        used = state.get("daily_volume", {}).get(date, {})
        return {key: wire(max(dec(0), limit - dec(used.get(key, "0")))) for key, _ in SIDES}

    def _start(self, pair, state, brokers, snapshots, guards, plan, *, kind):
        opening = plan.phase == "open"
        identities = state["identities"]
        self._config_guard(pair, identities, opening=opening)
        for guard in guards.values():
            guard()
        before = self._expected(state)
        require_quantities(snapshots, before)
        token = uuid.uuid4().hex
        legs = []
        for key, side in SIDES:
            buy = (side == "LONG") == opening
            order = Executor.order(SYMBOL, side, "BUY" if buy else "SELL", plan.qty, side[0] + token[:28])
            legs.append({"key": key, "order": order, "receipt": None, "dispatch": "prepared"})
        target = {side: wire(dec(before[side]) + (plan.qty if opening else -plan.qty)) for _, side in SIDES}
        if any(dec(q) < dec(state["owned"][side]) for side, q in target.items()):
            raise PairPositionError("平仓数量会侵占配对组原始底仓")
        pending = {"id": token, "kind": kind, "phase": plan.phase, "symbol": SYMBOL,
                   "identities": deepcopy(identities), "created_at": time.time(), "quantity": wire(plan.qty),
                   "before": before, "target": target, "leverage": plan.leverage, "legs": legs,
                   "repairs": [], "repair_attempts": 0, "config": {**pair["cycle"], "leverage": plan.leverage}}
        state.update(pending=pending, phase="submitting", reason="并行提交两个子账户的市价单")
        self._save(pair, state)
        try:
            # Persistent intent precedes the final age/config/capacity checks.
            self._config_guard(pair, identities, opening=opening)
            for guard in guards.values():
                guard()
            book = self.engine.cycle_book(SYMBOL) if kind == "cycle" else self.market.book(SYMBOL)
            if kind == "cycle":
                capacity = self.engine.require_cycle_open_capacity(pair["cycle"], plan.leverage,
                    minimum_notional=plan.capacity_notional or 0) if opening else None
                latest = plan_paired_cycle(pair, snapshots, book, self.engine.cycle_depth(SYMBOL), self.market.rules[SYMBOL],
                                           state["progress"], capacity=capacity, daily_remaining=self._daily_remaining(pair, state) if opening else None,
                                           paused=not pair["enabled"])
            else:
                latest = plan_ordinary(pair, snapshots, book, self.market.rules[SYMBOL], self.engine.capacities(SYMBOL))
            if latest.phase != plan.phase or latest.qty < plan.qty or latest.leverage != plan.leverage:
                raise RequestNotSent("发单前两账户风险、盘口或额度已变化")
            for guard in guards.values():
                guard()
        except TradingError as exc:
            self._retry_delay(state, exc)
            for leg in legs:
                leg["receipt"] = self._absent(leg["order"], str(exc))
            pending["last_error"] = str(exc)
            self._save(pair, state)
            self._recover(pair, state, brokers)
            return
        self._dispatch(pair, state, brokers, legs, guards=guards, reconciliation=not opening)
        self._recover(pair, state, brokers)

    @staticmethod
    def _absent(order, reason, *, local=True):
        return {**order, "clientOrderId": order["newClientOrderId"], "status": "REJECTED", "executedQty": "0",
                "avgPrice": "0", "reject_reason": reason, "local_not_sent": local}

    def _dispatch(self, pair, state, brokers, legs, *, guards=None, reconciliation=False):
        # A crash between this commit and HTTP leaves an uncertain request, not
        # permission to repeat it. Each leg retains its own durable client ID.
        for leg in legs:
            leg["dispatch"] = "sending"
            # Persist before HTTP: only older records may use the narrow legacy
            # rejection recovery below, never a new ambiguous submission.
            leg["submit_evidence_version"] = 1
        self._save(pair, state)
        def send(leg):
            order, broker = leg["order"], brokers[leg["key"]]
            try:
                if guards:
                    try:
                        guards[leg["key"]]()
                    except TradingError as exc:
                        raise RequestNotSent(str(exc)) from None
                budget = getattr(broker, "reconciliation_budget", nullcontext)() if reconciliation else nullcontext()
                with budget:
                    result = broker.submit([order])
                if not isinstance(result, list) or len(result) != 1:
                    raise TradingError("单腿订单回执无效，等待查询")
                row = result[0]
                if isinstance(row, dict) and row.get("code") in ORDER_REJECTION_CODES:
                    code = row["code"]
                    reason = exchange_reason(row.get("msg")) or MISSING_REJECT_REASON
                    row = {**self._absent(order, f"Aster 拒绝订单（代码 {code}）：{reason}", local=False), "reject_code": code}
                Executor.validate_receipt(order, row)
                return row, None
            except RequestNotSent as exc:
                return {**self._absent(order, str(exc)), "retry_after": max(1, getattr(exc, "retry_after", 0))}, None
            except ExchangeError as exc:
                if not isinstance(exc, AmbiguousOrder) and exc.code in ORDER_REJECTION_CODES:
                    return {**self._absent(order, str(exc), local=False), "reject_code": exc.code}, None
                return None, str(exc)
            except Exception as exc:
                return None, str(exc) if isinstance(exc, TradingError) else "订单结果未知，继续按客户端订单号核对"
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="pair-order") as pool:
            futures = [(leg, pool.submit(send, leg)) for leg in legs]
            for leg, future in futures:
                receipt, error = future.result()
                leg["receipt"], leg["error"] = receipt, error
                leg["submit_error"] = error or (receipt or {}).get("reject_reason")
                self._save(pair, state)

    def _legacy_notional_rejection(self, pending, leg, exc):
        """Recover the old -2029 classification bug, not arbitrary missing orders."""
        if (pending.get("kind") not in {"ordinary", "cycle"} or pending.get("phase") != "open"
                or not any(leg is original for original in pending["legs"])
                or leg.get("receipt") is not None or leg.get("dispatch") != "sending"
                or "submit_evidence_version" in leg
                or not isinstance(exc, ExchangeError) or isinstance(exc, (AmbiguousOrder, RequestNotSent))
                or exc.code != -2013 or exc.retry_after or exc.http_status not in (None, 400, 404)):
            return None
        # Old releases retained only the locally formatted original POST error.
        # Match the whole known response; never search the latest query error or
        # accept an embedded code, timeout, gateway error, or unknown message.
        reason = ("Aster 拒绝请求（代码 -2029）：You've reached the maximum notional value limit for this symbol. "
                  "You can still reduce or close your position to manage your risk.")
        if leg.get("submit_error") != reason:
            return None
        order = leg["order"]
        if (order.get("symbol") != SYMBOL or order.get("type") != "MARKET"
                or (order.get("positionSide"), order.get("side")) not in {("LONG", "BUY"), ("SHORT", "SELL")}):
            return None
        return {**self._absent(order, reason, local=False), "reject_code": -2029,
                "recovered_from_submit_error": True}

    def _query(self, pair, state, brokers):
        pending = state["pending"]
        for leg in pending["legs"] + pending["repairs"]:
            receipt, order = leg.get("receipt"), leg["order"]
            if receipt is not None:
                Executor.validate_receipt(order, receipt)
                if receipt["status"] in TERMINAL:
                    continue
            if leg.get("dispatch") == "prepared":
                leg["receipt"] = self._absent(order, "重启确认该订单尚未进入发送阶段")
                continue
            try:
                row = brokers[leg["key"]].query(SYMBOL, order["newClientOrderId"])
                Executor.validate_receipt(order, row)
                if receipt and dec(row["executedQty"]) < dec(receipt["executedQty"]):
                    raise PairPositionError("订单查询累计成交数量倒退")
                leg["receipt"], leg["error"] = row, None
            except PaperOrderAbsent:
                leg["receipt"] = self._absent(order, "模拟订单未写入持久账本")
            except TradingError as exc:
                self._retry_delay(state, exc)
                leg["error"] = str(exc)
                restored = self._legacy_notional_rejection(pending, leg, exc)
                if restored is not None:
                    leg["receipt"] = restored
            self._save(pair, state)
        return all(leg.get("receipt") and leg["receipt"]["status"] in TERMINAL
                   for leg in pending["legs"] + pending["repairs"])

    def _recover(self, pair, state, brokers):
        pending = state["pending"]
        if pending["kind"] == "leverage":
            return self._recover_leverage(pair, state, brokers)
        if not self._query(pair, state, brokers):
            state.update(phase="reconciling", reason="至少一条市价单结果未确定，系统按原订单编号继续查询；禁止重发、新开仓及划转")
            return
        snapshots, guards = self._read(brokers, reconciliation=True)
        self._publish_snapshots(state, snapshots)
        held = positions(snapshots)
        actual = {side: held[side].qty for _, side in SIDES}
        expected = {side: dec(pending["before"][side]) for _, side in SIDES}
        for leg in pending["legs"] + pending["repairs"]:
            order, row = leg["order"], leg["receipt"]
            adding = (order["positionSide"] == "LONG") == (order["side"] == "BUY")
            expected[order["positionSide"]] += dec(row["executedQty"]) * (1 if adding else -1)
        if actual != expected or any(pos.leverage != pending["leverage"] for pos in held.values()):
            raise PairPositionError("成交回执与两子账户实际仓位或杠杆不一致，保留批次并停止新增；请人工核对交易所成交与持仓")
        original_full = all(leg["receipt"]["status"] == "FILLED" for leg in pending["legs"])
        if pending["phase"] == "open" and original_full and not pending["repairs"]:
            limit = cycle_margin_limit(pair["ordinary"]) if pending["kind"] == "cycle" else opening_margin_limit(pair["ordinary"], pending["leverage"])
            if any(s.equity <= 0 or s.margin_exceeds(limit) for s in snapshots.values()):
                original_full = False
                pending["last_error"] = "成交后子账户保证金超限，撤回本批新增量"
        desired = pending["target"] if pending["phase"] == "close" or original_full and not pending["repairs"] else pending["before"]
        if any(actual[side] < dec(desired[side]) for _, side in SIDES):
            raise PairPositionError("实际仓位低于恢复目标，禁止通过反向加仓修复；请人工核对两侧实际仓位与本批成交")
        if all(actual[side] == dec(desired[side]) for _, side in SIDES):
            with self._confirmed(pair, brokers, guards):
                self._finish(pair, state, pending, completed=original_full and not pending["repairs"]
                             or pending["phase"] == "close")
            return
        if pending["repair_attempts"] >= 3:
            raise PairPositionError("减仓恢复已尝试三次，仍有本批残余仓位；系统停止继续自动减仓，请人工核对两侧持仓与成交")
        if time.time() < pending.get("repair_retry_at", 0):
            state.update(phase="repairing", reason="本地请求预算不足，系统等待预算恢复后重试尚未发送的减仓；已发送订单只查询原编号")
            return
        repairs = []
        rule = self.market.rules[SYMBOL]
        for key, side in SIDES:
            qty = actual[side] - dec(desired[side])
            if not qty:
                continue
            if qty % rule.step or qty < rule.min_qty or qty > rule.max_qty:
                raise PairPositionError("本批残余数量不符合市价减仓步长或限额，等待人工核对")
            order = Executor.order(SYMBOL, side, "SELL" if side == "LONG" else "BUY", qty, "R" + uuid.uuid4().hex[:28])
            repairs.append({"key": key, "order": order, "receipt": None, "dispatch": "prepared"})
        pending["repair_attempts"] += 1
        pending["repairs"].extend(repairs)
        state.update(phase="repairing", reason="系统正在按本批记录的恢复目标继续减仓；不追加另一腿")
        self._save(pair, state)
        self._config_guard(pair, pending["identities"], opening=False)
        self._dispatch(pair, state, brokers, repairs, guards=guards, reconciliation=True)
        if all(leg.get("receipt", {}).get("local_not_sent") for leg in repairs if leg.get("receipt")) \
                and all(leg.get("receipt") for leg in repairs):
            pending["repair_attempts"] -= 1
            pending["repair_retry_at"] = time.time() + max(leg["receipt"].get("retry_after", 1) for leg in repairs)
            self._save(pair, state)

    def _finish(self, pair, state, pending, *, completed):
        published = state
        state = deepcopy(state)
        state.pop("attention", None)
        if completed:
            state.update(failure_count=0, retry_at=0)
        else:
            count = min(5, state.get("failure_count", 0) + 1)
            state.update(failure_count=count, retry_at=time.time() + min(300, 30 * 2 ** (count - 1)))
        opened = pending["phase"] == "open"
        if pending["kind"] == "ordinary" and completed:
            state["owned"] = deepcopy(pending["target"])
            state["progress"] = empty_progress(state["owned"], state["progress"].get("completed_cycles", 0))
        elif pending["kind"] == "cycle" and opened and completed:
            state["progress"] = {"phase": "holding", "baseline": deepcopy(state["owned"]),
                                 "quantities": {side: pending["quantity"] for _, side in SIDES},
                                 "leverage": pending["leverage"], "config": pending["config"],
                                 "opened_at": time.time(), "completed_cycles": state["progress"].get("completed_cycles", 0)}
        elif pending["kind"] == "cycle" and not opened and completed:
            state["progress"] = empty_progress(state["owned"], state["progress"].get("completed_cycles", 0) + 1)
        self._account_volume(state, pending)
        state["pending"] = None
        state["last_batch"] = {"id": pending["id"], "kind": pending["kind"], "phase": pending["phase"],
                               "quantity": pending["quantity"], "completed": completed, "at": time.time()}
        any_fill = any(dec(leg["receipt"]["executedQty"]) for leg in pending["legs"] + pending["repairs"])
        state.update(phase="holding" if any(dec(q) for q in state["progress"]["quantities"].values()) else "waiting",
                     reason="两个子账户本批成交与持仓核对完成" if completed else
                            "本批未完整成交，已减回原始基线" if any_fill else "本批未成交，底仓保持不变")
        rejection_notes = [f"{'A 多侧' if leg['key'] == 'long' else 'B 空侧'}：{exchange_reason(leg['receipt']['reject_reason'])}"
                           for leg in pending["legs"] + pending["repairs"] if leg["receipt"].get("reject_reason")]
        rejection_text = "；拒单反馈：" + "；".join(rejection_notes) if rejection_notes else ""
        state["updated_at"] = time.time()
        # History and lifecycle advancement share one commit. A restart cannot
        # count the batch twice or forget its remaining cycle position.
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for key, value in (("pair_runtime:" + pair["id"], self._durable(state)),
                               ("pair_batch:" + pending["id"], {**pending, "completed": completed, "finished_at": time.time()})):
                db.execute("INSERT INTO kv(key,data) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data", (key, dumps(value)))
            for key, _ in SIDES:
                db.execute("INSERT INTO events(account_id,kind,message,created_at) VALUES(?,?,?,?)",
                           (pair[key + "_account_id"], "order", f"配对组 {pair['name']}：{state['reason']}；XAUUSD1 本批每边委托 {pending['quantity']}{rejection_text}", time.time()))
        # The outer tick always persists its state, even after an exception.
        # Publish only after the history and lifecycle transaction commits.
        published.clear()
        published.update(state)

    @staticmethod
    def _account_volume(state, pending):
        daily = state.setdefault("daily_volume", {})
        def unknown():
            # A receipt cannot prove each fill's date across midnight. Bound
            # the uncertainty to affected days, never freeze all future days.
            state["volume_unknown"] = True
            state["volume_unknown_until_utc"] = datetime.now(timezone.utc).date().isoformat()
        for leg in pending["legs"] + pending["repairs"]:
            row = leg["receipt"]
            qty = dec(row["executedQty"])
            if not qty:
                continue
            stamp = row.get("updateTime", row.get("time"))
            if type(stamp) not in (int, float) or not pending["created_at"] - 1 <= stamp / 1000 <= time.time() + 1:
                unknown()
                continue
            # Unknown cross-midnight executions cannot be assigned to one day
            # from the last update timestamp alone. Preserve the uncertainty.
            start_day = datetime.fromtimestamp(pending["created_at"], timezone.utc).date().isoformat()
            date = datetime.fromtimestamp(stamp / 1000, timezone.utc).date().isoformat()
            if start_day != date:
                unknown()
                continue
            quote = row.get("cumQuote", row.get("cumQuoteQty"))
            try:
                amount = positive(quote) if quote is not None else qty * positive(row["avgPrice"])
            except TradingError:
                unknown()
                continue
            used = daily.setdefault(date, {"long": "0", "short": "0"})
            used[leg["key"]] = wire(dec(used[leg["key"]]) + amount)
        # Keep a week in the hot row; every original receipt remains in pair_batch.
        state["daily_volume"] = {day: daily[day] for day in sorted(daily)[-8:]}

    def _leverage(self, pair, state, brokers, snapshots, guards, target):
        self._config_guard(pair, state["identities"], opening=True)
        for guard in guards.values():
            guard()
        pending = {"id": uuid.uuid4().hex, "kind": "leverage", "identities": deepcopy(state["identities"]),
                   "created_at": time.time(), "before": self._expected(state), "target_leverage": target,
                   "previous_leverage": positions(snapshots)["LONG"].leverage, "results": {}}
        state.update(pending=pending, phase="leverage",
                     reason=f"系统正在将两个子账户从 {pending['previous_leverage']}x 共同升至 {target}x；两侧实际杠杆确认前不下单")
        self._save(pair, state)
        def send(key):
            broker = brokers[key]
            try:
                self._config_guard(pair, pending["identities"], opening=True)
                if isinstance(broker, LiveBroker):
                    def check(snapshot):
                        snapshot.require_modes([SYMBOL])
                        rows = snapshot.pair(SYMBOL)
                        side = "LONG" if key == "long" else "SHORT"
                        if any(p.qty != dec(pending["before"][side]) if p.side == side else p.qty != 0 for p in rows):
                            raise PairPositionError("升杠杆前仓位已变化")
                    return {"response": broker.set_leverage(SYMBOL, target, checked_snapshot=snapshots[key], before_submit=check)}
                return {"response": broker.set_leverage(SYMBOL, target)}
            except (RequestNotSent, LeverageRejected) as exc:
                return {"rejected": True, "reason": str(exc)}
            except Exception:
                return {"unknown": True, "reason": "杠杆请求结果未知，系统继续读取两侧实际杠杆；不重发原请求"}
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="pair-leverage") as pool:
            futures = {key: pool.submit(send, key) for key, _ in SIDES}
            for key, future in futures.items():
                pending["results"][key] = future.result()
                self._save(pair, state)
        self._recover_leverage(pair, state, brokers)

    def _recover_leverage(self, pair, state, brokers):
        pending = state["pending"]
        snapshots, guards = self._read(brokers, reconciliation=True)
        self._publish_snapshots(state, snapshots)
        held = require_quantities(snapshots, pending["before"], equal_leverage=False)
        with self._confirmed(pair, brokers, guards):
            if all(p.leverage == pending["target_leverage"] for p in held.values()):
                state.update(pending=None, phase="waiting", reason=f"两子账户实际杠杆均已确认 {pending['target_leverage']}x；系统下一轮重新检查额度、余额、价差及风险条件后才开仓")
            elif (all(p.leverage == pending["previous_leverage"] for p in held.values())
                  and all(pending["results"].get(key, {}).get("rejected") for key, _ in SIDES)):
                state.update(pending=None, phase="waiting", reason=f"两个升杠杆请求均被明确拒绝，实际仍为 {pending['previous_leverage']}x；系统稍后重新评估升档条件")
            else:
                state.update(phase="reconciling",
                    reason=f"两子账户升杠杆尚未共同确认至 {pending['target_leverage']}x：做多账户 {held['LONG'].leverage}x、做空账户 {held['SHORT'].leverage}x；系统继续读取实际设置，不重发原请求，禁止新订单或划转")
            self._save(pair, state)
