"""Exclusive two-account ownership, configuration and scheduler coordination."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, nullcontext
from copy import deepcopy
import json
import re
import threading
import time

from .cycle import DEFAULT_CYCLE, validate_cycle
from .exchange import LiveBroker
from .margin_balance import DEFAULT_MARGIN, validate_margin
from .models import AccountModeError, TradingError, dec, positive, wire

SYMBOL = "XAUUSD1"
DEFAULT_ORDINARY = {"enabled": False, "threshold": "10000", "order_notional": "1000",
                    "min_open_leverage": 5, "margin_limit": "0.5", "spread_limit": "0.0005"}
PAIR_FIELDS = {"id", "name", "long_account_id", "short_account_id", "symbol", "enabled",
               "ordinary", "cycle", "margin", "revision", "pause_reason"}


def _decimal_fields(config, keys):
    for key in keys:
        if not isinstance(config[key], str) or len(config[key]) > 128:
            raise TradingError("配对金额及比例必须为十进制字符串")
        config[key] = wire(positive(config[key], True))


def validate_pair(value):
    if not isinstance(value, dict) or set(value) - PAIR_FIELDS:
        raise TradingError("配对组配置字段无效")
    pair = deepcopy(value)
    for key in ("id", "long_account_id", "short_account_id"):
        if not isinstance(pair.get(key), str) or not re.fullmatch(r"[a-z0-9_-]{1,32}", pair[key]):
            raise TradingError("配对组及账户标识仅支持小写字母、数字、下划线和短横线")
    if pair["long_account_id"] == pair["short_account_id"]:
        raise TradingError("配对组需要两个不同子账户")
    if not isinstance(pair.get("name"), str) or not 1 <= len(pair["name"].strip()) <= 50:
        raise TradingError("配对组名称需为 1 至 50 个字符")
    pair.setdefault("symbol", SYMBOL)
    pair.setdefault("enabled", False)
    if pair["symbol"] != SYMBOL or type(pair["enabled"]) is not bool:
        raise TradingError("配对组仅支持 XAUUSD1，运行开关必须为布尔值")
    ordinary = pair.get("ordinary", {})
    margin = pair.get("margin", {})
    if not isinstance(ordinary, dict) or set(ordinary) - set(DEFAULT_ORDINARY):
        raise TradingError("配对普通策略配置字段无效")
    if not isinstance(margin, dict) or set(margin) - set(DEFAULT_MARGIN):
        raise TradingError("保证金均衡配置字段无效")
    ordinary = {**DEFAULT_ORDINARY, **ordinary}
    margin = {**DEFAULT_MARGIN, **margin}
    if type(ordinary["enabled"]) is not bool or type(margin["enabled"]) is not bool:
        raise TradingError("策略开关必须为布尔值")
    _decimal_fields(ordinary, ("threshold", "order_notional", "margin_limit", "spread_limit"))
    if not 0 <= dec(ordinary["threshold"]) <= 1000000000 or not 500 <= dec(ordinary["order_notional"]) <= 1000000:
        raise TradingError("普通额度阈值应在 0 至 1000000000，单边批次金额应在 500 至 1000000 USD1")
    if type(ordinary["min_open_leverage"]) is not int or ordinary["min_open_leverage"] not in (5, 10, 20):
        raise TradingError("普通最低开仓杠杆仅支持 5x、10x、20x")
    if not 0 < dec(ordinary["margin_limit"]) <= 1 or not 0 < dec(ordinary["spread_limit"]) <= dec("0.0005"):
        raise TradingError("保证金上限须大于 0 且不超过 100%，普通价差上限须大于 0 且不超过万 5")
    cycle = validate_cycle(pair.get("cycle"))
    if cycle["symbol"] != SYMBOL:
        raise TradingError("配对循环仅支持 XAUUSD1")
    if ordinary["enabled"] and cycle["enabled"]:
        raise TradingError("同一配对组不能同时启用普通加仓和独立循环")
    margin = validate_margin(margin)
    pair.update(ordinary=ordinary, cycle=cycle, margin=margin)
    return pair


def has_cycle_quantity(runtime):
    progress = (runtime or {}).get("progress") or {}
    return any(dec(qty) for qty in progress.get("quantities", {}).values())


def pair_active(pair, runtime=None):
    return bool(pair.get("enabled") or (runtime or {}).get("pending") or has_cycle_quantity(runtime))


class PairManager:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store
        self.locks = {}
        self.trader = None
        from .pair_recovery import PairOrderRecovery
        self.recovery = PairOrderRecovery(self)

    def group_lock(self, pair_id):
        with self.engine.lock:
            return self.locks.setdefault(pair_id, threading.RLock())

    @contextmanager
    def locked(self, pair):
        with self.group_lock(pair["id"]), ExitStack() as stack:
            for aid in sorted((pair["long_account_id"], pair["short_account_id"])):
                stack.enter_context(self.engine.account_lock(aid))
            yield

    def require_unbound(self, account_id):
        if self.store.pair_for_account(account_id):
            raise TradingError("该账户由配对组管理，请在配对组配置或暂停策略")

    def _members(self, pair):
        accounts = [self.store.account(pair[key]) for key in ("long_account_id", "short_account_id")]
        if any(account is None for account in accounts):
            raise TradingError("配对组成员账户不存在")
        if accounts[0]["mode"] != accounts[1]["mode"]:
            raise TradingError("配对组不能混用模拟和实盘账户")
        for account in accounts:
            if account["enabled"]:
                raise TradingError("请先暂停两个子账户原有单账户策略")
            existing = self.store.pair_for_account(account["id"])
            if existing and existing["id"] != pair["id"]:
                raise TradingError("账户已属于其他配对组")
            if self.store.intent(account["id"]) or self.store.get("post_fill_check:" + account["id"]):
                raise TradingError("原账户仍有未完成批次，请先恢复核对")
            legacy = self.store.get("cycle:" + account["id"]) or {}
            if any(dec(qty) for qty in legacy.get("quantities", {}).values()):
                raise TradingError("原账户循环仍有新增持仓，请先完成原循环")
        return accounts

    def _read_members(self, pair, *, flat=False, adopt=False, reconciliation=False):
        accounts = self._members(pair)
        brokers = {account["id"]: self.engine.broker(account) for account in accounts}
        budgets = {}
        for broker in brokers.values():
            if isinstance(broker, LiveBroker) and getattr(broker.api, "budget", None) is not None:
                budget = broker.api.budget
                budgets[budget] = budgets.get(budget, 0) + broker.snapshot_weight([SYMBOL], fresh_modes=True) + 40
        # Admit both fresh account reads and full-account open orders before
        # starting either worker. Each request still reserves its own weight.
        for budget, weight in budgets.items():
            with budget.reconciliation() if reconciliation else budget.cycle_accounting():
                budget.require_available(weight)

        def read(account):
            broker = brokers[account["id"]]
            budget = broker.reconciliation_budget() if reconciliation and isinstance(broker, LiveBroker) else nullcontext()
            with budget:
                return read_broker(broker)

        def read_broker(broker):
            if hasattr(broker, "reload"):
                broker.reload()
            snapshot = broker.snapshot([SYMBOL], fresh_modes=True)
            current = (lambda: broker.require_snapshot_current(snapshot)) if isinstance(broker, LiveBroker) else snapshot.require_fresh
            guard = (broker._snapshot_lock if isinstance(broker, LiveBroker) else None, current)
            snapshot.require_fresh()
            snapshot.require_modes([SYMBOL])
            snapshot.pair(SYMBOL)
            if not snapshot.can_trade:
                raise TradingError("子账户没有交易权限")
            if not flat and snapshot.equity <= 0:
                raise TradingError("启用配对组前需要子账户具有正的 USD1 权益")
            orders = snapshot.open_orders
            if isinstance(broker, LiveBroker):
                orders = broker.api.call("GET", "/fapi/v3/openOrders", signed=True, weight=40)
            if not isinstance(orders, list) or orders:
                raise TradingError("配对组启用或绑定前需确认两个子账户都没有未完成挂单")
            return snapshot, guard

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="pair-check") as pool:
            rows = list(pool.map(read, accounts))
        snapshots = [row[0] for row in rows]
        guards = {account["id"]: row[1] for account, row in zip(accounts, rows)}
        with self._current_members(guards):
            pass
        runtime = self.store.get("pair_runtime:" + pair["id"]) or {}
        owned = runtime.get("owned") or {"LONG": "0", "SHORT": "0"}
        for side, snapshot in zip(("LONG", "SHORT"), snapshots):
            snapshot.require_fresh()
            for position in snapshot.positions:
                if position.qty and (position.symbol != SYMBOL or position.side != side):
                    raise TradingError("每个配对子账户只能持有 XAUUSD1 的指定方向，请先处理原有仓位")
                if position.symbol == SYMBOL and position.side == side:
                    expected = dec("0") if flat else dec(owned.get(side, "0"))
                    if position.qty != expected and (flat or not adopt):
                        raise TradingError("子账户实际仓位与配对组记录不一致，禁止自动采纳外部持仓")
        if snapshots[0].pair(SYMBOL)[0].leverage != snapshots[1].pair(SYMBOL)[0].leverage:
            raise TradingError("两个子账户的 XAUUSD1 实际杠杆必须相同")
        return dict(zip(("long", "short"), snapshots)), guards

    @contextmanager
    def _current_members(self, guards):
        # Do not hold these locks across network reads. Once all preparation is
        # complete, serialize known account events with the local durable write.
        # Sorting matches member lock ordering and checks BOTH reads only after
        # both revocation locks are acquired.
        with ExitStack() as stack:
            for account_id in sorted(guards):
                lock, _ = guards[account_id]
                if lock is not None:
                    stack.enter_context(lock)
            for _, current in guards.values():
                current()
            yield

    def _changed(self, pair):
        with self.engine.lock:
            self.engine.accounts_generation += 1
        for key in ("long_account_id", "short_account_id"):
            self.engine.revoke_cycle_hot_data(pair[key], "配对组状态或配置已更新")
        self.engine.scheduler_event.set()

    def create(self, data):
        if not isinstance(data, dict) or set(data) - (PAIR_FIELDS - {"revision", "pause_reason"}):
            raise TradingError("配对组创建字段无效")
        if data.get("enabled") not in (None, False):
            raise TradingError("新配对组必须先保存，再单独启动")
        pair = validate_pair({**data, "enabled": False})
        with self.locked(pair), self.engine.registration_lock:
            if (self.store.get("pair_runtime:" + pair["id"]) or self.store.get("pair_margin:" + pair["id"])
                    or self.store.get("pair_deleted:" + pair["id"])):
                raise TradingError("配对组标识存在历史记录，请使用新标识")
            _, guards = self._read_members(pair, flat=True)
            with self._current_members(guards):
                pair = self.store.save_pair(pair, create=True)
            self.store.event(pair["id"], "pair", "双子账户配对组已建立，默认暂停")
            self._changed(pair)
        return pair

    def _idle(self, pair, *, require_flat=False):
        if pair["enabled"]:
            raise TradingError("请先暂停配对组")
        runtime = self.store.get("pair_runtime:" + pair["id"], {})
        margin = self.store.get("pair_margin:" + pair["id"], {})
        if not isinstance(runtime, dict) or not isinstance(margin, dict):
            raise TradingError("配对组或保证金日志无效，请先核对，不能重新采纳持仓")
        if runtime.get("pending") is not None or has_cycle_quantity(runtime):
            raise TradingError("配对组仍在核对订单或减回本轮循环持仓，暂不能启动、修改设置、核对空仓或删除；请查看配对组当前执行状态")
        if margin.get("pending") is not None or margin.get("status") in ("submitting", "acknowledged", "accepted", "unknown"):
            raise TradingError("保证金划转结果尚未核实，暂不能启动、修改设置、核对空仓或删除；请查看保证金平衡中的核对状态")
        if require_flat and any(dec(qty) for qty in (runtime.get("owned") or {}).values()):
            raise TradingError("配对组仍有普通策略底仓，完全平仓并核对后才能解除绑定")
        return runtime

    def configure(self, pair_id, changes):
        previous = self.store.pair(pair_id)
        if not previous:
            raise TradingError("配对组不存在")
        if not isinstance(changes, dict) or not changes or set(changes) - {"name", "ordinary", "cycle", "margin"}:
            raise TradingError("仅可修改组名称及策略配置，账户方向固定")
        with self.locked(previous):
            pair = self.store.pair(pair_id)
            self._idle(pair)
            updated = deepcopy(pair)
            for key, value in changes.items():
                if key == "name":
                    updated[key] = value
                elif not isinstance(value, dict) or not value:
                    raise TradingError("请提供有效的配对策略配置")
                else:
                    updated[key] = {**updated[key], **value}
            updated = validate_pair(updated)
            self._members(updated)
            saved = self.store.save_pair(updated)
            self._changed(saved)
            return saved

    def enable(self, pair_id, enabled):
        original = self.store.pair(pair_id)
        if not original:
            raise TradingError("配对组不存在")
        with self.locked(original):
            pair = self.store.pair(pair_id)
            if enabled:
                from .engine import snapshot_json
                from .pair_execution import PairTrader, empty_progress
                if pair["enabled"]:
                    raise TradingError("配对组已启动，无需重复启动")
                pending = (self.store.get("pair_runtime:" + pair_id, {}) or {}).get("pending")
                if isinstance(pending, dict) and pending.get("kind") == "ordinary" and pending.get("phase") == "open":
                    result = self.recovery.check(pair_id, source="paused_start")
                    if not result["completed"]:
                        raise TradingError("启动前自动核对尚未完成，仓位保持不变；" + result["message"])
                    pair = self.store.pair(pair_id)
                margin = self.store.get("pair_margin:" + pair_id, {})
                original_runtime = self._idle(pair)
                if not any(pair[key]["enabled"] for key in ("ordinary", "cycle", "margin")):
                    raise TradingError("请先选择普通策略、独立循环或保证金均衡")
                accounts = self._members(pair)
                if any(not self.engine.live_allowed(a) for a in accounts):
                    raise TradingError("服务器尚未启用实盘执行（ASTER_ALLOW_LIVE=1）")
                trader = PairTrader(self.engine)
                _, _, identities = trader._members(pair)
                if original_runtime.get("identities") and original_runtime["identities"] != identities:
                    raise TradingError("配对组凭据指向的真实账户发生变化，不能采纳持仓，请先核对账户身份")
                snapshots, guards = self._read_members(pair, adopt=True)
                if original_runtime.get("recovery_watch") is not None:
                    from .pair_recovery import require_archived_orders_clear
                    require_archived_orders_clear(self.engine, pair, state=original_runtime)
                    for key, side, index in (("long", "LONG", 0), ("short", "SHORT", 1)):
                        if snapshots[key].pair(SYMBOL)[index].qty != dec(original_runtime["owned"][side]):
                            raise TradingError("人工归档后实际仓位发生变化，不能自动采纳；请先核对原订单是否迟到成交")
                from .margin_balance import MarginBalancer
                MarginBalancer(self.engine).verify_members(pair)
                if trader._members(pair)[2] != identities:
                    raise TradingError("启动核验期间账户身份已变化，请重新核对")
                owned = {side: wire(snapshots[key].pair(SYMBOL)[index].qty)
                         for key, side, index in (("long", "LONG", 0), ("short", "SHORT", 1))}
                runtime = deepcopy(original_runtime)
                previous_owned = runtime.get("owned") or {"LONG": "0", "SHORT": "0"}
                changed = any(dec(previous_owned.get(side, "0")) != dec(qty) for side, qty in owned.items())
                reason = (f"启动时已核验并采纳实际底仓：多 {owned['LONG']}、空 {owned['SHORT']} XAU；后续循环仅处理本轮新增仓位"
                          if changed else "两侧实际底仓与账户身份已核验，系统开始检查所选策略和保证金条件")
                runtime.update(owned=owned, pending=None,
                    progress=empty_progress(owned, (runtime.get("progress") or {}).get("completed_cycles", 0)),
                    identities=identities, phase="waiting", reason=reason,
                    snapshots={side: snapshot_json(snapshot, [SYMBOL]) for side, snapshot in snapshots.items()},
                    updated_at=time.time())
                runtime.pop("attention", None)
                runtime.pop("retry_after", None)
                pair.pop("pause_reason", None)
                pair["enabled"] = True
                message = (f"配对组已启动；实际持仓已采纳为底仓：多 {previous_owned.get('LONG', '0')} → {owned['LONG']}，"
                           f"空 {previous_owned.get('SHORT', '0')} → {owned['SHORT']} XAU；采纳本身未下单或划转"
                           if changed else "配对组已启动，系统开始检查所选策略和保证金条件")
                def check_current():
                    for _, current in guards.values():
                        current()
                    if any(not self.engine.live_allowed(a) for a in accounts):
                        raise TradingError("服务器尚未启用实盘执行（ASTER_ALLOW_LIVE=1）")
                with self._current_members(guards):
                    saved = self.store.activate_pair(pair, runtime, expected_runtime=original_runtime,
                        expected_margin=margin, accounts=accounts, check_current=check_current, message=message)
            else:
                pair["enabled"] = False
                saved = self.store.save_pair(pair)
                self.store.event(pair_id, "pair", "配对组已暂停，停止新开仓和新划转；已提交订单及划转继续核对，循环新增仓位继续减回，普通底仓保留")
            self._changed(saved)
            return saved

    def preview_recovery(self, pair_id):
        pair = self.store.pair(pair_id)
        if pair is None:
            raise TradingError("配对组不存在")
        with self.locked(pair), self.store.connection_scope():
            return self.recovery.preview(pair_id)

    def check_recovery(self, pair_id):
        pair = self.store.pair(pair_id)
        if pair is None:
            raise TradingError("配对组不存在")
        with self.locked(pair), self.store.connection_scope():
            return self.recovery.check(pair_id)

    def confirm_recovery(self, pair_id, token, acknowledge_unknown=False):
        pair = self.store.pair(pair_id)
        if pair is None:
            raise TradingError("配对组不存在")
        with self.locked(pair), self.store.connection_scope():
            return self.recovery.confirm(pair_id, token, acknowledge_unknown)

    def skip_recovery(self, pair_id, batch_id, acknowledge_skip=False):
        pair = self.store.pair(pair_id)
        if pair is None:
            raise TradingError("配对组不存在")
        with self.locked(pair), self.store.connection_scope():
            return self.recovery.skip_positions(pair_id, batch_id, acknowledge_skip)

    def delete(self, pair_id):
        pair = self.store.pair(pair_id)
        if not pair:
            raise TradingError("配对组不存在")
        with self.locked(pair), self.engine.registration_lock:
            pair = self.store.pair(pair_id)
            runtime = self._idle(pair, require_flat=True)
            if runtime.get("recovery_watch") is not None:
                raise TradingError("人工归档订单仍需跟踪，不能解除子账户绑定；请保留此配对组")
            _, guards = self._read_members(pair, flat=True)
            with self._current_members(guards):
                self.store.delete_pair(pair_id)
            self.store.event(pair_id, "pair", "配对组已空仓解除绑定，历史执行记录保留")
            self._changed(pair)

    def reconcile_flat(self, pair_id):
        """A read-only exchange check releases obsolete, fully closed ownership."""
        pair = self.store.pair(pair_id)
        if not pair:
            raise TradingError("配对组不存在")
        with self.locked(pair):
            pair = self.store.pair(pair_id)
            original = self._idle(pair)
            snapshots, guards = self._read_members(pair, flat=True)
            from .engine import snapshot_json
            from .store import dumps
            zeros = {"LONG": "0", "SHORT": "0"}
            runtime = {**original, "owned": zeros.copy(), "pending": None,
                       "progress": {"phase": "waiting_open", "baseline": zeros.copy(), "quantities": zeros.copy(),
                                    "completed_cycles": (original.get("progress") or {}).get("completed_cycles", 0)},
                       "phase": "paused", "reason": "已只读核实两子账户完全空仓，组底仓记录已清零",
                       "snapshots": {side: snapshot_json(snapshot, [SYMBOL]) for side, snapshot in snapshots.items()},
                       "updated_at": time.time()}
            runtime.pop("attention", None)
            with self._current_members(guards), self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                saved_pair = db.execute("SELECT data FROM pairs WHERE id=?", (pair_id,)).fetchone()
                saved_runtime = db.execute("SELECT data FROM kv WHERE key=?", ("pair_runtime:" + pair_id,)).fetchone()
                saved_margin = db.execute("SELECT data FROM kv WHERE key=?", ("pair_margin:" + pair_id,)).fetchone()
                margin = json.loads(saved_margin[0]) if saved_margin else {}
                if (not saved_pair or json.loads(saved_pair[0]) != pair
                        or (json.loads(saved_runtime[0]) if saved_runtime else {}) != original
                        or margin.get("pending") or margin.get("status") in ("submitting", "accepted", "unknown")):
                    raise TradingError("空仓核对期间组状态已变化，请重新核对")
                for _, current in guards.values():
                    current()
                db.execute("INSERT INTO kv(key,data) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                           ("pair_runtime:" + pair_id, dumps(runtime)))
                db.execute("INSERT INTO events(account_id,kind,message,created_at) VALUES(?,?,?,?)",
                           (pair_id, "pair", runtime["reason"] + "；历史成交保留，未下单或划转", time.time()))
            self._changed(pair)

    def states(self, reader=None):
        from .margin_balance import MarginBalancer
        reader = reader or self.store
        rows = []
        for pair in reader.pairs():
            runtime = reader.get("pair_runtime:" + pair["id"]) or {}
            state = {"phase": "waiting" if pair["enabled"] else "paused",
                     "reason": pair.get("pause_reason") or ("等待系统首次检查两侧账户与策略条件" if pair["enabled"] else "配对组已暂停；核对设置后需点击启动"),
                     "updated_at": None, "snapshots": {}, "progress": None, "pending": None, **runtime}
            state["margin"] = MarginBalancer.status_view(pair,
                reader.get("pair_margin:" + pair["id"], {}), state.get("margin"))
            rows.append({**pair, "state": state})
        return rows

    def active_for_account(self, account_id):
        pair = self.store.pair_for_account(account_id)
        return pair if pair and pair_active(pair, self.store.get("pair_runtime:" + pair["id"])) else None

    def tick(self, pair_id):
        pair = self.store.pair(pair_id)
        if not pair or self.engine.shutdown.is_set():
            return 5
        with self.locked(pair), self.store.connection_scope():
            pair = self.store.pair(pair_id)
            if not pair:
                return 5
            try:
                accounts = self._members(pair)
                if any(not self.engine.live_allowed(account) for account in accounts):
                    raise TradingError("服务器未开放实盘执行，配对组仅保留现有记录")
                if self.trader is None:
                    from .pair_execution import PairTrader
                    self.trader = PairTrader(self.engine)
                state = self.trader.tick(pair)
                delay = self.poll_interval(pair, state)
                return max(delay, state.get("retry_after", 0))
            except (TradingError, KeyError, ValueError, TypeError) as exc:
                runtime = self.store.get("pair_runtime:" + pair_id) or {}
                runtime.update(phase="attention" if isinstance(exc, AccountModeError) else "waiting",
                               reason=str(exc) if isinstance(exc, TradingError) else "配对数据暂不可用，系统将重新读取；若持续出现，请检查服务器日志", updated_at=time.time())
                self.store.put("pair_runtime:" + pair_id, runtime)
                if isinstance(exc, AccountModeError) and pair["enabled"]:
                    self.store.save_pair({**pair, "enabled": False, "pause_reason": str(exc)})
                    self._changed(pair)
                return max(1, getattr(exc, "retry_after", 0))

    def poll_interval(self, pair, state):
        margin = self.store.get("pair_margin:" + pair["id"]) or {}
        if state.get("pending") or has_cycle_quantity(state) or margin.get("pending"):
            return 1
        if not pair["enabled"]:
            return 30
        if pair["cycle"]["enabled"]:
            return 1
        if pair["ordinary"]["enabled"]:
            return 2
        return min(5, pair["margin"]["check_interval_seconds"])
