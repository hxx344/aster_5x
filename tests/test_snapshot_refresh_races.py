"""Maintenance cannot starve recovery reads; genuine account changes still revoke them."""
from copy import deepcopy
import time
from unittest import TestCase
from unittest.mock import Mock, patch

from tests.helpers import Fixture
from tests.test_cycle_account_snapshot import ACCOUNT, BRACKET, RISK, SYMBOL, LocalQuoteMarket, cycle_account_responses, risk_row
from tests import test_cycle_parallel_engine as parallel_cases
from tests.test_exchange_hardening import FixtureAPI
from trading.account_cache import HotAccountUnavailable
from trading.cycle import DEFAULT_CYCLE
from trading.cycle_execution import CycleExecutor
from trading.engine import CYCLE_HOT_POLL_INTERVAL, Engine
from trading.exchange import ExchangeError, LiveBroker, SnapshotSuperseded
from trading.paper import PAPER_BRACKETS


class MaintenanceSnapshotTests(TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        account = deepcopy(self.f.account)
        account.update(mode="live", enabled=True, cycle={**DEFAULT_CYCLE, "enabled": True})
        self.f.store.save_account(account)
        responses = cycle_account_responses()
        responses["/fapi/v3/positionSide/dual"] = {"dualSidePosition": True}
        responses[RISK] = [risk_row(row) for row in responses[ACCOUNT]["positions"]]
        responses[BRACKET] = {"symbol": SYMBOL, "brackets": PAPER_BRACKETS}
        self.api = FixtureAPI(responses)
        self.broker = LiveBroker({}, LocalQuoteMarket(), api=self.api)
        self.broker.start_cycle_hot_data = Mock()
        self.engine = Engine(self.f.store, market=self.f.market)
        self.addCleanup(self.engine.dashboard_reports.close)
        self.engine.live_allowed = Mock(return_value=True)
        self.engine.brokers["test"] = self.broker
        self.broker.cycle_cache.configure([SYMBOL])
        self.broker.cycle_cache.set_connected(True)
        self.assertTrue(self.broker.refresh_cycle_hot_snapshot())

    def test_repeated_pending_polls_revoke_hot_leases_without_interrupting_either_recovery_read(self):
        original = self.api.call

        def read_while_maintenance_runs(method, path, *args, **kwargs):
            value = original(method, path, *args, **kwargs)
            if path == ACCOUNT:
                for _ in range(3):
                    self.assertEqual(self.engine.poll_cycle_hot_data("test"), CYCLE_HOT_POLL_INTERVAL)
            return value

        for marker in ("intent", "post_fill"):
            for method in (self.broker.cycle_snapshot, self.broker.snapshot):
                with self.subTest(marker=marker, method=method.__name__):
                    self.assertTrue(self.broker.refresh_cycle_hot_snapshot())
                    lease = self.broker.cycle_hot_snapshot([SYMBOL])
                    generation = self.broker._snapshot_generation
                    self.f.store.put("post_fill_check:test", {"intent_id": "existing"} if marker == "post_fill" else None)
                    with patch.object(self.f.store, "intent", return_value={"id": "existing"} if marker == "intent" else None), \
                         patch.object(self.api, "call", side_effect=read_while_maintenance_runs):
                        snapshot = method([SYMBOL])
                    snapshot.require_fresh()
                    self.assertEqual(self.broker._snapshot_generation, generation)
                    with self.assertRaises(HotAccountUnavailable):
                        lease.require_fresh()
                    self.f.store.put("post_fill_check:test", None)
        self.assertTrue(all(call[0] == "GET" for call in self.api.calls))

    def test_new_batch_after_refresh_discards_publication_without_revoking_recovery_authority(self):
        snapshot = self.broker.snapshot([SYMBOL], fresh_modes=True)
        refresh = self.broker.refresh_cycle_hot_snapshot
        leases = []

        def publish_then_mark():
            published = refresh()
            leases.append(self.broker.cycle_hot_snapshot([SYMBOL]))
            self.f.store.put("post_fill_check:test", {"intent_id": "new-batch"})
            return published

        with patch.object(self.broker, "refresh_cycle_hot_snapshot", side_effect=publish_then_mark):
            self.assertEqual(self.engine.poll_cycle_hot_data("test"), CYCLE_HOT_POLL_INTERVAL)
        self.broker.require_snapshot_current(snapshot)
        with self.assertRaises(HotAccountUnavailable):
            leases[0].require_fresh()

    def test_failed_background_refresh_does_not_revoke_an_independent_read(self):
        snapshot = self.broker.snapshot([SYMBOL], fresh_modes=True)
        lease = self.broker.cycle_hot_snapshot([SYMBOL])
        with patch.object(self.broker, "refresh_cycle_hot_snapshot", side_effect=ExchangeError("rate limit", retry_after=180)):
            self.assertEqual(self.engine.poll_cycle_hot_data("test"), 180)
        self.broker.require_snapshot_current(snapshot)
        self.assertGreater(self.engine.work("test").hot_backoff, time.monotonic() + 179)
        with self.assertRaises(HotAccountUnavailable):
            lease.require_fresh()

    def test_real_account_events_still_interrupt_both_snapshot_readers(self):
        original = self.api.call
        for event in ("ACCOUNT_UPDATE", "ORDER_TRADE_UPDATE", "ACCOUNT_CONFIG_UPDATE"):
            def changed(method, path, *args, **kwargs):
                value = original(method, path, *args, **kwargs)
                if path == ACCOUNT:
                    self.broker._cycle_account_event(event)
                return value
            for method in (self.broker.cycle_snapshot, self.broker.snapshot):
                with self.subTest(event=event, method=method.__name__), \
                     patch.object(self.api, "call", side_effect=changed), self.assertRaises(SnapshotSuperseded):
                    method([SYMBOL])


class SnapshotRetryTests(TestCase):
    setUp = parallel_cases.CycleParallelEngineTests.setUp
    start = parallel_cases.CycleParallelEngineTests.start
    capacities = parallel_cases.CycleParallelEngineTests.capacities

    def pending_cycle(self):
        owner = self.start()
        with patch.object(CycleExecutor, "reconcile", return_value="simulated interruption"):
            self.engine.tick_cycle_account(owner, self.f.broker, None)
        return self.f.store.intent("test")

    def test_cycle_reconciliation_conflict_waits_briefly_and_resumes_without_resubmission(self):
        pending = self.pending_cycle()
        orders = deepcopy(self.f.broker.state["orders"])
        before = time.monotonic()
        with patch.object(self.f.broker, "cycle_snapshot", side_effect=SnapshotSuperseded("账户在查询期间发生变化，等待新快照")):
            self.assertEqual(self.engine.tick_account("test"), 1)
        state = self.engine.views["test"]
        self.assertEqual((state["status"], state["cycle_state"]["phase"]), ("waiting", "reconciling"))
        self.assertTrue(self.f.store.account("test")["enabled"])
        self.assertEqual(self.f.store.intent("test")["id"], pending["id"])
        self.assertLess(self.engine.work("test").backoff - before, 2)
        self.assertFalse(any(row["kind"] == "error" for row in self.f.store.events()))
        with patch.object(self.f.broker, "submit", side_effect=AssertionError("must only reconcile")):
            self.engine.tick_account("test")
        self.assertIsNone(self.f.store.intent("test"))
        self.assertEqual(self.f.store.get("cycle:test")["phase"], "holding")
        self.assertEqual(self.f.broker.state["orders"], orders)

    def test_post_fill_conflict_keeps_risk_marker_and_existing_longer_backoff(self):
        self.start()
        marker = {"symbol": "CLUSD1", "leverage": 5}
        self.f.store.put("post_fill_check:test", marker)
        until = self.engine.work("test").backoff = time.monotonic() + 180
        with patch.object(self.f.broker, "snapshot", side_effect=SnapshotSuperseded("read changed")):
            self.assertEqual(self.engine.tick_account("test"), 1)
        self.assertEqual(self.engine.views["test"]["status"], "waiting")
        self.assertEqual(self.engine.work("test").backoff, until)
        self.assertEqual(self.f.store.get("post_fill_check:test"), marker)
        self.assertEqual(self.f.broker.state["orders"], {})

    def test_existing_attention_is_not_cleared_by_a_transient_read_conflict(self):
        pending = self.pending_cycle()
        pending.update(status="attention", last_error="核对外部持仓")
        self.f.store.save_intent(pending)
        self.f.store.pause_account(self.f.store.account("test"), "核对外部持仓")
        with patch.object(self.f.broker, "cycle_snapshot", side_effect=SnapshotSuperseded("read changed")):
            self.assertEqual(self.engine.tick_account("test"), 1)
        self.assertFalse(self.f.store.account("test")["enabled"])
        self.assertEqual(self.engine.views["test"]["status"], "attention")
        self.assertEqual(self.engine.views["test"]["reason"], "核对外部持仓")
        self.assertEqual(self.f.store.intent("test")["status"], "attention")

    def test_manual_pause_is_preserved_while_an_existing_batch_waits_for_a_new_read(self):
        pending = self.pending_cycle()
        self.engine.enable("test", False)
        orders = deepcopy(self.f.broker.state["orders"])
        with patch.object(self.f.broker, "cycle_snapshot", side_effect=SnapshotSuperseded("read changed")):
            self.assertEqual(self.engine.tick_account("test"), 1)
        self.assertFalse(self.f.store.account("test")["enabled"])
        self.assertEqual(self.engine.views["test"]["status"], "paused")
        self.assertEqual(self.f.store.intent("test")["id"], pending["id"])
        self.assertEqual(self.f.broker.state["orders"], orders)
