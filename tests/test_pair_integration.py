"""Two-account ownership and controls; no live API or funds are used."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import threading
import time
import unittest
from unittest.mock import patch

from tests.helpers import Fixture, account
from tests.test_cycle_account_snapshot import (ACCOUNT, BRACKET, ORDERS, RISK, SYMBOL,
    LocalQuoteMarket, cycle_account_responses, risk_row)
from tests.test_exchange_hardening import FixtureAPI
from trading.engine import Engine, snapshot_json
from trading.exchange import LiveBroker
from trading.models import TradingError, dec
from trading.pairing import validate_pair
from trading.paper import PAPER_BRACKETS, PaperBroker
from trading.store import Store


def pair_config(**changes):
    return {"id": "gold", "name": "黄金配对", "long_account_id": "test",
            "short_account_id": "second", "cycle": {"enabled": True}, **changes}


class PairIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        for aid in ("test", "second", "third"):
            self.f.store.save_account({**account(aid), "enabled": False})
        self.engine = Engine(self.f.store, market=self.f.market)
        self.engine.brokers["test"] = self.f.broker

    def create(self, **changes):
        return self.engine.pairs.create(pair_config(**changes))

    def test_create_reserves_members_and_blocks_every_old_account_write(self):
        pair = self.create()
        self.assertFalse(pair["enabled"])
        self.assertEqual(self.f.store.pair_for_account("test")["id"], "gold")
        self.assertEqual(self.f.store.pair_for_account("second")["id"], "gold")
        self.assertFalse(self.f.store.account("test")["enabled"])
        for action in (lambda: self.engine.enable("test", True), lambda: self.engine.enable("test", False),
                       lambda: self.engine.configure("test", {"order_notional": "2000"}),
                       lambda: self.engine.delete_account("test"), lambda: self.engine.retry("test")):
            with self.assertRaisesRegex(TradingError, "配对"):
                action()
        with patch("trading.engine.Executor.open_pair", side_effect=AssertionError("legacy writer")):
            self.assertEqual(self.engine.tick_account("test"), 30)
        state = self.engine.state()
        self.assertEqual(state["pairs"][0]["id"], "gold")
        member = next(row for row in state["accounts"] if row["id"] == "test")
        self.assertEqual(member["pair_direction"], "LONG")

    def test_existing_positions_and_pending_legacy_intents_block_creation(self):
        self.f.broker.state["positions"]["XAUUSD1:SHORT"].update(qty="1", entry="4412")
        self.f.store.put("paper:test", self.f.broker.state)
        with self.assertRaisesRegex(TradingError, "原有仓位|外部持仓"):
            self.create()
        self.assertIsNone(self.f.store.pair("gold"))
        self.f.broker.state["positions"]["XAUUSD1:SHORT"]["qty"] = "0"
        self.f.store.put("paper:test", self.f.broker.state)
        self.f.store.save_intent({"id": "pending", "account_id": "test", "status": "pending", "kind": "leverage"})
        with self.assertRaisesRegex(TradingError, "未完成"):
            self.create()

    def test_atomic_membership_excludes_overlapping_groups_from_other_store(self):
        other = Store(self.f.store.path)
        barrier = threading.Barrier(2)
        configs = [validate_pair(pair_config()), validate_pair(pair_config(id="other", long_account_id="third"))]

        def write(store, config):
            barrier.wait(timeout=2)
            try:
                store.save_pair(config, create=True)
                return True
            except TradingError:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(write, store, config) for store, config in zip((self.f.store, other), configs)]
            self.assertEqual(sorted(future.result() for future in futures), [False, True])
        self.assertEqual(len(self.f.store.pairs()), 1)

    def test_config_rejects_stale_revision_and_retains_frozen_members(self):
        pair = self.create()
        updated = self.engine.pairs.configure("gold", {"name": "新的名称"})
        self.assertGreater(updated["revision"], pair["revision"])
        with self.assertRaisesRegex(TradingError, "已变化"):
            self.f.store.save_pair(pair)
        with self.assertRaisesRegex(TradingError, "方向固定"):
            self.engine.pairs.configure("gold", {"long_account_id": "third"})
        with self.assertRaisesRegex(TradingError, "同时启用"):
            self.engine.pairs.configure("gold", {"ordinary": {"enabled": True}})

    def test_pending_and_unknown_transfer_block_changes_and_release(self):
        self.create()
        self.f.store.put("pair_runtime:gold", {"pending": {"id": "uncertain"}})
        with self.assertRaisesRegex(TradingError, "核对订单"):
            self.engine.pairs.configure("gold", {"name": "blocked"})
        with self.assertRaises(TradingError):
            self.engine.pairs.delete("gold")
        self.f.store.put("pair_runtime:gold", {"pending": None})
        self.f.store.put("pair_margin:gold", {"status": "unknown", "pending": {"id": "transfer"}})
        with self.assertRaisesRegex(TradingError, "划转"):
            self.engine.pairs.delete("gold")
        self.f.store.put("pair_margin:gold", {"status": "complete", "pending": None})
        self.engine.pairs.delete("gold")
        self.assertIsNone(self.f.store.pair_for_account("test"))
        with self.assertRaisesRegex(TradingError, "历史记录"):
            self.create()

    def test_pause_still_schedules_recovery_and_serializes_both_accounts(self):
        self.create()
        self.f.store.put("pair_runtime:gold", {"pending": {"id": "old"}})
        entered, release = threading.Event(), threading.Event()
        concurrent, maximum, calls = 0, 0, []
        guard = threading.Lock()

        class Trader:
            def tick(inner, pair):
                nonlocal concurrent, maximum
                with guard:
                    concurrent += 1
                    maximum = max(maximum, concurrent)
                    calls.append(pair["enabled"])
                entered.set()
                release.wait(timeout=3)
                with guard:
                    concurrent -= 1
                return {"pending": {"id": "old"}}

        self.engine.pairs.trader = Trader()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.engine.pairs.tick, "gold")
            self.assertTrue(entered.wait(timeout=2))
            second = pool.submit(self.engine.pairs.tick, "gold")
            time.sleep(.02)
            release.set()
            self.assertEqual(first.result(), 1)
            self.assertEqual(second.result(), 1)
        self.assertEqual(maximum, 1)
        self.assertEqual(calls, [False, False])

    def test_group_capacity_and_monitoring_includes_xau_with_paused_members(self):
        pair = self.create()
        pair["enabled"] = True
        self.f.store.save_pair(pair)
        self.assertNotIn("XAUUSD1", self.engine.fast_capacity_targets(self.f.store.accounts()))
        snapshots = {side: snapshot_json(self.engine.broker(self.f.store.account(aid)).snapshot([SYMBOL]), [SYMBOL])
            for side, aid in (("long", "test"), ("short", "second"))}
        self.f.store.put("pair_runtime:gold", {"snapshots": snapshots})
        self.assertEqual(self.engine.fast_capacity_targets(self.f.store.accounts())["XAUUSD1"], {5})
        self.assertEqual(pair["cycle"]["leverage"], 2)
        snapshots["short"]["timestamp"] -= 10
        self.f.store.put("pair_runtime:gold", {"snapshots": snapshots})
        self.assertNotIn("XAUUSD1", self.engine.fast_capacity_targets(self.f.store.accounts()))
        self.assertIn("XAUUSD1", self.engine.required_monitoring_symbols())
        self.assertIsNotNone(self.engine.pairs.active_for_account("test"))

    def test_group_polling_keeps_recovery_fast_and_idle_work_bounded(self):
        pair = self.create()
        manager = self.engine.pairs
        self.assertEqual(manager.poll_interval(pair, {}), 30)
        self.assertEqual(manager.poll_interval(pair, {"pending": {"id": "old"}}), 1)
        self.assertEqual(manager.poll_interval(pair, {"progress": {"quantities": {"LONG": "1"}}}), 1)
        self.f.store.put("pair_margin:gold", {"pending": {"id": "transfer"}})
        self.assertEqual(manager.poll_interval(pair, {}), 1)
        self.f.store.put("pair_margin:gold", {})
        pair = {**pair, "enabled": True}
        self.assertEqual(manager.poll_interval(pair, {}), 1)
        pair["cycle"]["enabled"] = False
        pair["ordinary"]["enabled"] = True
        self.assertEqual(manager.poll_interval(pair, {}), 2)
        pair["ordinary"]["enabled"] = False
        self.assertEqual(manager.poll_interval(pair, {}), 5)

    def test_manager_respects_current_worker_retry_after(self):
        from unittest.mock import Mock
        self.create()
        self.engine.pairs.trader = Mock()
        self.engine.pairs.trader.tick.return_value = {"pending": {"id": "old"}, "retry_after": 60}
        self.assertEqual(self.engine.pairs.tick("gold"), 60)
        self.engine.pairs.trader.tick.return_value = {"pending": {"id": "old"}}
        self.assertEqual(self.engine.pairs.tick("gold"), 1)

    def test_states_preserves_margin_reason_and_newer_durable_pending(self):
        pair = self.create(margin={"enabled": True})
        self.f.store.save_pair({**pair, "enabled": True})
        self.f.store.put("pair_runtime:gold", {"margin": {"enabled": True, "checked_at": 100,
            "status": "waiting", "reason": "余额差未达到阈值", "pending": None, "last_transfer": None}})
        self.f.store.put("pair_margin:gold", {"checked_at": 100, "next_check_at": 105})
        state = self.engine.pairs.states()[0]["state"]["margin"]
        self.assertEqual(state["status"], "waiting")
        self.assertEqual(state["reason"], "余额差未达到阈值")
        self.f.store.put("pair_margin:gold", {"checked_at": 100, "pending": {
            "status": "unknown", "request_id": "new", "identity": "internal", "before_receipts": {}}})
        state = self.engine.state()["pairs"][0]["state"]["margin"]
        self.assertEqual(state["status"], "unknown")
        self.assertTrue(state["blocks_trading"])
        self.assertEqual(state["pending"]["request_id"], "new")
        self.assertNotIn("identity", state["pending"])
        self.f.store.put("pair_margin:gold", [])
        self.assertEqual(self.engine.pairs.states()[0]["state"]["margin"]["status"], "blocked")

    def test_raw_legacy_intent_cannot_bypass_pair_ownership(self):
        self.create()
        with self.assertRaisesRegex(TradingError, "配对组"):
            self.f.store.save_account({**self.f.store.account("test"), "enabled": True})
        with self.assertRaisesRegex(TradingError, "配对组"):
            self.f.store.save_intent({"id": "bad", "account_id": "test", "status": "pending", "kind": "pair"})
        self.assertIsNone(self.f.store.intent("test"))

    def test_reconcile_flat_clears_only_verified_closed_owned_positions(self):
        self.create()
        self.f.store.put("pair_runtime:gold", {"owned": {"LONG": "1", "SHORT": "1"},
            "progress": {"quantities": {"LONG": "0", "SHORT": "0"}, "completed_cycles": 9},
            "pending": None, "attention": "外部已平仓", "daily_volume": {"2026-09-29": {"long": "100", "short": "100"}}})
        history = {"kind": "ordinary", "quantity": "1", "completed": True}
        self.f.store.put("pair_batch:old", history)
        with patch.object(self.f.broker, "submit", side_effect=AssertionError("read-only recovery")):
            self.engine.pairs.reconcile_flat("gold")
        runtime = self.f.store.get("pair_runtime:gold")
        self.assertEqual(runtime["owned"], {"LONG": "0", "SHORT": "0"})
        self.assertEqual(runtime["phase"], "paused")
        self.assertEqual(runtime["progress"]["completed_cycles"], 9)
        self.assertNotIn("attention", runtime)
        self.assertIn("2026-09-29", runtime["daily_volume"])
        self.assertEqual(self.f.store.get("pair_batch:old"), history)
        # Empty accounts may already have withdrawn their cash; collateral is
        # an opening prerequisite, not a condition for proving flat ownership.
        for aid in ("test", "second"):
            broker = self.engine.broker(self.f.store.account(aid))
            broker.state["wallet"] = "0"
            self.f.store.put("paper:" + aid, broker.state)
        self.engine.pairs.reconcile_flat("gold")
        self.engine.pairs.delete("gold")
        self.assertIsNone(self.f.store.pair_for_account("test"))

    def test_reconcile_flat_rejects_nonzero_positions_active_group_and_pending_writes(self):
        pair = self.create()
        self.f.store.put("pair_runtime:gold", {"owned": {"LONG": "1", "SHORT": "1"}})
        self.f.broker.state["positions"]["XAUUSD1:LONG"].update(qty="1", entry="4412")
        self.f.store.put("paper:test", self.f.broker.state)
        with self.assertRaisesRegex(TradingError, "外部持仓"):
            self.engine.pairs.reconcile_flat("gold")
        self.assertEqual(self.f.store.get("pair_runtime:gold")["owned"]["LONG"], "1")
        self.f.broker.state["positions"]["XAUUSD1:LONG"]["qty"] = "0"
        self.f.store.put("paper:test", self.f.broker.state)
        pair = self.f.store.save_pair({**pair, "enabled": True})
        with self.assertRaisesRegex(TradingError, "暂停"):
            self.engine.pairs.reconcile_flat("gold")
        self.f.store.save_pair({**pair, "enabled": False})
        self.f.store.put("pair_runtime:gold", {"pending": {"id": "order"}})
        with self.assertRaisesRegex(TradingError, "核对订单"):
            self.engine.pairs.reconcile_flat("gold")
        self.f.store.put("pair_runtime:gold", {})
        self.f.store.put("pair_margin:gold", {"pending": {"id": "transfer"}})
        with self.assertRaisesRegex(TradingError, "划转"):
            self.engine.pairs.reconcile_flat("gold")


class PairSnapshotLifecycleTests(unittest.TestCase):
    """Real broker generation checks with offline GET fixtures, never credentials."""
    create = PairIntegrationTests.create

    def setUp(self):
        PairIntegrationTests.setUp(self)
        self.live = {}
        for aid in ("test", "second"):
            self.f.store.save_account({**self.f.store.account(aid), "mode": "live"})
            responses = cycle_account_responses()
            responses["/fapi/v3/positionSide/dual"] = {"dualSidePosition": True}
            responses[RISK] = [risk_row(row) for row in responses[ACCOUNT]["positions"]]
            responses[BRACKET] = {"symbol": SYMBOL, "brackets": PAPER_BRACKETS}
            responses[ORDERS] = []
            broker = LiveBroker({}, LocalQuoteMarket(), api=FixtureAPI(responses))
            digit = "1" if aid == "test" else "2"
            broker.api.credentials = {"user": "0x" + digit * 40, "signer": "0x" + digit * 39 + "f"}
            self.live[aid] = self.engine.brokers[aid] = broker
        patcher = patch.object(self.engine, "live_allowed", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("trading.margin_balance.MarginBalancer.verify_members", return_value={"verified": True})
        self.verify = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.assertTrue(all(call[0] == "GET" for broker in self.live.values() for call in broker.api.calls))

    def test_live_capacity_uses_both_current_leases_without_stale_view_fallback(self):
        self.create()
        self.engine.pairs.enable("gold", True)
        self.f.store.put("pair_runtime:gold", {"snapshots": {
            side: {"timestamp": time.time(), "positions": [{"symbol": SYMBOL, "leverage": 5}]}
            for side in ("long", "short")}})
        with patch.object(self.live["test"].cycle_cache, "current_leverage", return_value=10), \
             patch.object(self.live["second"].cycle_cache, "current_leverage", return_value=10) as second:
            self.assertEqual(self.engine.fast_capacity_targets(self.f.store.accounts()), {SYMBOL: {10}})
            second.return_value = 20
            self.assertNotIn(SYMBOL, self.engine.fast_capacity_targets(self.f.store.accounts()))
            second.side_effect = TradingError("热快照已失效")
            self.assertNotIn(SYMBOL, self.engine.fast_capacity_targets(self.f.store.accounts()))

    def revoke_after_reads(self):
        original = self.engine.pairs._read_members

        def read(*args, **kwargs):
            result = original(*args, **kwargs)
            self.live["test"]._cycle_account_event("ORDER_TRADE_UPDATE")
            return result

        return patch.object(self.engine.pairs, "_read_members", side_effect=read)

    def test_create_rejects_account_event_during_either_open_orders_read(self):
        for aid in self.live:
            with self.subTest(account=aid):
                broker = self.live[aid]
                original = broker.api.call

                def call(method, path, *args, **kwargs):
                    value = original(method, path, *args, **kwargs)
                    if path == ORDERS:
                        broker._cycle_account_event("ACCOUNT_UPDATE")
                    return value

                with patch.object(broker.api, "call", side_effect=call), self.assertRaisesRegex(TradingError, "失效"):
                    self.create()
                self.assertIsNone(self.f.store.pair("gold"))
                self.assertIsNone(self.f.store.pair_for_account(aid))

    def test_create_rechecks_revocation_after_all_preparation(self):
        with self.revoke_after_reads(), self.assertRaisesRegex(TradingError, "失效"):
            self.create()
        self.assertIsNone(self.f.store.pair("gold"))

    def test_create_rechecks_first_snapshot_after_waiting_for_other_account(self):
        first_read = threading.Event()
        first, second = self.live["test"], self.live["second"]
        first_call, second_call = first.api.call, second.api.call

        def read_first(method, path, *args, **kwargs):
            value = first_call(method, path, *args, **kwargs)
            if path == ORDERS:
                first_read.set()
            return value

        def read_second(method, path, *args, **kwargs):
            value = second_call(method, path, *args, **kwargs)
            if path == ORDERS:
                self.assertTrue(first_read.wait(2))
                first._cycle_account_event("ORDER_TRADE_UPDATE")
            return value

        with patch.object(first.api, "call", side_effect=read_first), \
             patch.object(second.api, "call", side_effect=read_second), self.assertRaisesRegex(TradingError, "失效"):
            self.create()
        self.assertIsNone(self.f.store.pair("gold"))

    def test_enable_rechecks_after_master_verification_without_clearing_attention(self):
        self.create()
        runtime = {"attention": "仍需核对", "owned": {"LONG": "0", "SHORT": "0"}}
        self.f.store.put("pair_runtime:gold", runtime)
        self.verify.side_effect = lambda pair: self.live["second"]._cycle_account_event("ACCOUNT_CONFIG_UPDATE")
        with self.assertRaisesRegex(TradingError, "失效"):
            self.engine.pairs.enable("gold", True)
        self.assertFalse(self.f.store.pair("gold")["enabled"])
        self.assertEqual(self.f.store.get("pair_runtime:gold"), runtime)

    def test_delete_rechecks_revocation_before_releasing_members(self):
        self.create()
        with self.revoke_after_reads(), self.assertRaisesRegex(TradingError, "失效"):
            self.engine.pairs.delete("gold")
        self.assertIsNotNone(self.f.store.pair("gold"))
        self.assertEqual(self.f.store.pair_for_account("test")["id"], "gold")
        self.assertIsNone(self.f.store.get("pair_deleted:gold"))

    def test_reconcile_rechecks_revocation_before_clearing_owned(self):
        self.create()
        runtime = {"attention": "外部平仓待核对", "owned": {"LONG": "1", "SHORT": "1"},
                   "progress": {"quantities": {"LONG": "0", "SHORT": "0"}}}
        self.f.store.put("pair_runtime:gold", runtime)
        with self.revoke_after_reads(), self.assertRaisesRegex(TradingError, "失效"):
            self.engine.pairs.reconcile_flat("gold")
        self.assertEqual(self.f.store.get("pair_runtime:gold"), runtime)

    def test_account_event_cannot_cross_final_guard_and_local_commit(self):
        entered, finished = threading.Event(), threading.Event()
        committed = []
        broker = self.live["test"]
        original_lock = broker._snapshot_lock

        class ObservedLock:
            def __enter__(inner):
                if threading.current_thread().name == "pair-lifecycle-event":
                    entered.set()
                return original_lock.__enter__()

            def __exit__(inner, *args):
                return original_lock.__exit__(*args)

        def event():
            broker._cycle_account_event("ACCOUNT_UPDATE")
            committed.append(self.f.store.pair("gold") is not None)
            finished.set()

        worker = threading.Thread(target=event, name="pair-lifecycle-event", daemon=True)
        original_save = self.f.store.save_pair

        def save(*args, **kwargs):
            worker.start()
            self.assertTrue(entered.wait(2))
            self.assertFalse(finished.wait(.02))
            return original_save(*args, **kwargs)

        with patch.object(broker, "_snapshot_lock", ObservedLock()), patch.object(self.f.store, "save_pair", side_effect=save):
            try:
                self.create()
            finally:
                worker.join(timeout=2)
        self.assertTrue(finished.is_set())
        self.assertEqual(committed, [True])

    def test_current_reads_allow_enable_reconcile_and_release(self):
        self.create()
        self.engine.pairs.enable("gold", True)
        self.assertTrue(self.f.store.pair("gold")["enabled"])
        self.engine.pairs.enable("gold", False)
        self.engine.pairs.reconcile_flat("gold")
        self.engine.pairs.delete("gold")
        self.assertIsNone(self.f.store.pair("gold"))
