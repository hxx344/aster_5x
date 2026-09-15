"""Slow ordinary GETs yield execution; stale reads cannot fund later orders."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests.helpers import Fixture
from trading.cycle import DEFAULT_CYCLE
from trading.engine import Engine
from trading.exchange import BudgetWait, LiveBroker, SnapshotSuperseded
from trading.models import dec


class OrdinaryBackgroundReadTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.account = deepcopy(self.f.account)
        self.account.update(mode="live", cycle={**DEFAULT_CYCLE, "enabled": True})
        self.account["policy"]["symbols"] = ["SPCXUSD1"]
        self.f.store.save_account(self.account)
        self.account = self.f.store.account("test")
        self.engine = Engine(self.f.store, market=self.f.market)
        self.engine.live_allowed = Mock(return_value=True)
        self.api = SimpleNamespace(budget=Mock(), call=Mock(side_effect=AssertionError("unexpected HTTP")))
        self.broker = LiveBroker({}, self.f.market, self.api)
        self.engine.brokers["test"] = self.broker
        self.engine.tick_cycle_account = Mock(return_value=5)
        self.engine.cycle_public_hint = Mock(return_value={"phase": "open"})
        self.engine.capacities = Mock(return_value={5: dec(0), 10: dec(0), 20: dec(0)})
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.addCleanup(self.pool.shutdown)
        self.engine._ordinary_pool = self.pool

    def snapshot(self, symbols):
        value = self.f.broker.snapshot(symbols)
        value.account_read_generation = self.broker._snapshot_generation
        return value

    def test_slow_ordinary_read_releases_account_for_fast_cycle_and_resumes_once(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def slow(symbols):
            value = self.snapshot(symbols)
            entered.set()
            self.assertTrue(release.wait(3))
            return value
        self.broker.snapshot = Mock(side_effect=slow)
        self.assertEqual(self.engine.tick_account("test"), 5)
        self.assertTrue(entered.wait(1))
        read = self.engine.work("test").ordinary_read
        self.assertFalse(read.future.done())
        signal = {"symbol": "XAUUSD1", "source": "bbo", "received_at": time.time(),
                  "received_monotonic": time.monotonic()}
        self.assertEqual(self.engine.tick_account("test", cycle_signal=signal), 5)
        self.assertIsNotNone(self.engine.tick_cycle_account.call_args.kwargs["trigger"])
        self.assertFalse(read.future.done())
        release.set()
        read.future.result(timeout=2)
        self.engine.tick_account("test")
        self.assertEqual(self.broker.snapshot.call_count, 1)
        self.assertIsNone(self.engine.work("test").ordinary_read)
        self.assertIsNone(self.engine.work("test").cycle.opportunity)
        self.assertEqual(self.engine.tick_cycle_account.call_count, 2)
        self.api.call.assert_not_called()

    def test_changed_account_cannot_queue_another_read_behind_uncancellable_get(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def slow(symbols):
            entered.set()
            self.assertTrue(release.wait(3))
            return self.snapshot(symbols)
        self.broker.snapshot = Mock(side_effect=slow)
        self.engine.tick_account("test")
        self.assertTrue(entered.wait(1))
        first = self.engine.work("test").ordinary_read
        changed = {**self.account, "name": "updated account"}
        self.f.store.save_account(changed)
        self.engine.tick_account("test")
        self.assertIs(self.engine.work("test").ordinary_read, first)
        self.assertEqual(self.broker.snapshot.call_count, 1)
        release.set()
        first.future.result(timeout=2)

    def test_account_write_discards_completed_read_and_restarts_without_account_backoff(self):
        self.broker.snapshot = Mock(side_effect=self.snapshot)
        symbols = ["SPCXUSD1", "XAUUSD1"]
        self.assertIsNone(self.engine.prepare_ordinary_snapshot(self.account, self.broker, symbols))
        original = self.engine.work("test").ordinary_read.future.result(timeout=2)
        self.broker.invalidate_cycle_hot_data("local write")
        self.assertIsNone(self.engine.prepare_ordinary_snapshot(self.account, self.broker, symbols))
        renewed = self.engine.work("test").ordinary_read.future.result(timeout=2)
        self.assertIsNot(original, renewed)
        self.assertIs(self.engine.prepare_ordinary_snapshot(self.account, self.broker, symbols), renewed)
        self.assertEqual(self.engine.work("test").backoff, 0)

    def test_superseded_read_retries_but_budget_error_retains_account_backoff(self):
        self.broker.snapshot = Mock(side_effect=SnapshotSuperseded("changed during read"))
        self.engine.tick_account("test")
        read = self.engine.work("test").ordinary_read
        with self.assertRaises(SnapshotSuperseded):
            read.future.result(timeout=2)
        self.broker.snapshot.side_effect = BudgetWait("wait", retry_after=50)
        self.engine.tick_account("test")
        read = self.engine.work("test").ordinary_read
        with self.assertRaises(BudgetWait):
            read.future.result(timeout=2)
        self.assertEqual(self.engine.tick_account("test"), 50)
        self.assertGreater(self.engine.work("test").backoff, time.monotonic() + 49)

    def test_ready_snapshot_restores_ordinary_priority_signals(self):
        self.broker.snapshot = Mock(side_effect=self.snapshot)
        signals = {"SPCXUSD1": time.time()}
        self.engine.prepare_ordinary_snapshot(self.account, self.broker, ["SPCXUSD1", "XAUUSD1"],
                                               priority=True, signals=signals)
        self.engine.work("test").ordinary_read.future.result(timeout=2)
        with patch.object(self.engine, "select_leverage", return_value=None) as choose:
            self.engine.tick_account("test")
        self.assertTrue(choose.called)
        self.assertTrue(all(call.args[-1] is True for call in choose.call_args_list))
