from dataclasses import replace
from pathlib import Path
import os
import sqlite3
import threading
import time
import unittest
from unittest.mock import Mock, patch

from trading.engine import Engine
from trading.exchange import MarketData
from trading.execution import Executor
from trading.lock import ProcessLock
from trading.models import TradingError, dec, plan_pair
from trading.paper import PaperBroker
from trading.store import Store
from .helpers import Fixture, account


class EngineHardeningTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.engine = Engine(self.f.store, market=self.f.market)
        self.engine.brokers["test"] = self.f.broker
        self.engine.poll_market("XAUUSD1")

    def test_repeated_lock_acquire_keeps_original_ownership(self):
        owner = ProcessLock(Path(self.f.directory.name) / "owner.lock")
        other = ProcessLock(owner.path)
        owner.acquire()
        self.addCleanup(owner.release)
        self.addCleanup(other.release)
        original = owner.file
        with self.assertRaises(TradingError):
            owner.acquire()
        self.assertIs(owner.file, original)
        with self.assertRaises(TradingError):
            other.acquire()
        owner.release()
        other.acquire()

    @unittest.skipIf(os.name == "nt", "Linux deployment uses symlinked release/data paths")
    def test_database_symlink_uses_same_process_lock(self):
        alias = Path(self.f.directory.name) / "alias.sqlite3"
        alias.symlink_to(self.f.store.path)
        other = Engine(Store(alias), market=self.f.market)
        self.engine.process_lock.acquire()
        self.addCleanup(self.engine.process_lock.release)
        self.addCleanup(other.process_lock.release)
        with self.assertRaises(TradingError):
            other.process_lock.acquire()

    def test_engine_start_is_not_reentrant(self):
        worker = Mock()
        worker.is_alive.return_value = True
        with patch("trading.engine.threading.Thread", return_value=worker) as factory:
            self.engine.start()
            self.addCleanup(self.engine.process_lock.release)
            with self.assertRaises(TradingError):
                self.engine.start()
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(worker.start.call_count, 1)

    def test_failed_thread_start_releases_process_lock(self):
        worker = Mock()
        worker.start.side_effect = RuntimeError("thread creation failed")
        with patch("trading.engine.threading.Thread", return_value=worker):
            with self.assertRaisesRegex(RuntimeError, "thread creation failed"):
                self.engine.start()
        self.addCleanup(self.engine.process_lock.release)
        other = ProcessLock(self.engine.process_lock.path)
        self.addCleanup(other.release)
        other.acquire()

    def test_scheduler_failure_clears_health_and_closes_every_client_without_error_details(self):
        first, second = Mock(), Mock()
        first.close.side_effect = RuntimeError("private client diagnostic")
        market = Mock(spec=MarketData)
        market.api = Mock()
        market.api.close.side_effect = RuntimeError("private market diagnostic")
        self.engine.brokers = {"first": first, "second": second}
        self.engine.market = market
        self.engine.ready = True
        with patch.object(self.f.store, "accounts", side_effect=RuntimeError("private database diagnostic")), \
             self.assertLogs("aster.trading", level="ERROR") as log:
            self.engine.run()
        self.assertFalse(self.engine.ready)
        self.assertTrue(self.engine.shutdown.is_set())
        self.assertIn("异常停止", self.engine.error)
        for client in (first, second, market.api):
            client.close.assert_called_once_with()
        self.assertNotIn("private", self.engine.error + " ".join(log.output))

    def test_shutdown_retains_process_lock_and_clients_until_active_worker_stops(self):
        entered = threading.Event()
        release = threading.Event()
        broker = Mock()
        self.engine.brokers = {"test": broker}
        other = ProcessLock(self.engine.process_lock.path)
        self.addCleanup(other.release)
        def blocked_tick(account_id):
            entered.set()
            release.wait(3)
            return 60
        with patch.object(self.engine, "tick_account", side_effect=blocked_tick), \
             patch.object(self.engine, "poll_market", return_value=60), \
             patch.object(self.engine, "notify", return_value=60):
            self.engine.start()
            try:
                self.assertTrue(entered.wait(1))
                self.engine.shutdown.set()
                deadline = time.monotonic() + 1
                while self.engine.ready and time.monotonic() < deadline:
                    time.sleep(.005)
                self.assertFalse(self.engine.ready)
                broker.close.assert_not_called()
                with self.assertRaises(TradingError):
                    other.acquire()
            finally:
                release.set()
                self.engine.stop()
        broker.close.assert_called_once_with()
        self.assertFalse(self.engine.thread.is_alive())
        other.acquire()

    def test_nonpositive_post_fill_equity_persists_pause_and_finishes_campaign(self):
        snapshot = self.f.broker.snapshot(["XAUUSD1"])
        book = self.f.market.book("XAUUSD1")
        plan = plan_pair(snapshot, book, self.f.market.rules["XAUUSD1"], {5: dec(500000)}, self.f.account["policy"])
        Executor(self.f.store, self.f.broker, self.f.market).open_pair(self.f.account, snapshot, "XAUUSD1", plan, book)
        after = replace(self.f.broker.snapshot(["XAUUSD1"]), equity=dec(0))
        with patch.object(self.f.broker, "snapshot", return_value=after), \
             patch.object(self.f.broker, "submit", side_effect=AssertionError("must pause")):
            self.engine.tick_account("test")
        saved = self.f.store.account("test")
        self.assertFalse(saved["enabled"])
        self.assertIn("总权益不足", saved["pause_reason"])
        self.assertIsNone(self.f.store.get("post_fill_check:test"))
        self.assertIsNone(self.f.store.get("campaign:test"))
        self.assertIn("无法计算", self.f.store.events()[0]["message"])
        restored = Engine(Store(self.f.store.path), market=self.f.market)
        restored.brokers["test"] = self.f.broker
        self.assertEqual(restored.state()["accounts"][0]["status"], "attention")
        with patch.object(self.f.broker, "submit", side_effect=AssertionError("must not auto-resume")):
            restored.tick_account("test")
        self.assertEqual(restored.state()["accounts"][0]["reason"], saved["pause_reason"])
        restored.enable("test", True)
        self.assertNotIn("pause_reason", self.f.store.account("test"))

    def test_fixed_mode_pause_reason_survives_recovery_until_manual_start(self):
        snapshot = self.f.broker.snapshot(["XAUUSD1"])
        with patch.object(self.f.broker, "snapshot", return_value=replace(snapshot, hedge_mode=False)):
            self.engine.tick_account("test")
        reason = self.engine.state()["accounts"][0]["reason"]
        with patch.object(self.f.broker, "submit", side_effect=AssertionError("must not auto-resume")):
            self.engine.tick_account("test")
        self.assertEqual(self.engine.state()["accounts"][0]["reason"], reason)
        self.assertEqual(self.engine.state()["accounts"][0]["status"], "attention")
        self.engine.enable("test", True)
        self.assertNotIn("pause_reason", self.f.store.account("test"))

    def pending_pair(self):
        intent = {"id": "unfinished", "kind": "pair", "account_id": "test", "symbol": "XAUUSD1", "status": "pending"}
        self.f.store.save_intent(intent)
        return intent

    def test_aborted_pair_keeps_durable_post_fill_risk_check(self):
        intent = self.pending_pair()
        self.f.store.abort_pair(intent)
        restored = Store(self.f.store.path)
        self.assertIsNone(restored.intent("test"))
        self.assertTrue(restored.get("post_fill_check:test"))
        snapshot = replace(self.f.broker.snapshot(["XAUUSD1"]), equity=dec(0))
        with patch.object(self.f.broker, "snapshot", return_value=snapshot):
            self.engine.tick_account("test")
        self.assertFalse(restored.account("test")["enabled"])

    def test_aborted_pair_and_risk_marker_commit_atomically(self):
        intent = self.pending_pair()
        with self.f.store.connect() as db:
            db.execute("""CREATE TRIGGER fail_check BEFORE INSERT ON kv
                WHEN NEW.key='post_fill_check:test' BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.f.store.abort_pair(intent)
        self.assertEqual(self.f.store.intent("test")["status"], "pending")
        self.assertEqual(intent["status"], "pending")
        self.assertIsNone(self.f.store.get("post_fill_check:test"))

    def test_risk_pause_still_allows_pending_reconciliation_next_tick(self):
        intent = self.pending_pair()
        self.f.store.put("post_fill_check:test", True)
        snapshot = replace(self.f.broker.snapshot(["XAUUSD1"]), equity=dec(0))
        with patch.object(self.f.broker, "snapshot", return_value=snapshot), \
             patch("trading.engine.Executor.reconcile", return_value="核对保留批次") as reconcile:
            self.engine.tick_account("test")
            self.assertFalse(self.f.store.account("test")["enabled"])
            self.assertEqual(reconcile.call_count, 0)
            self.engine.tick_account("test")
        self.assertEqual(reconcile.call_count, 1)
        self.assertEqual(reconcile.call_args.args[1]["id"], intent["id"])
        self.assertFalse(self.f.store.account("test")["enabled"])

    def test_stale_snapshot_cannot_clear_post_fill_risk_check(self):
        self.f.store.put("post_fill_check:test", True)
        stale = replace(self.f.broker.snapshot(["XAUUSD1"]), timestamp=time.time() - 9)
        with patch.object(self.f.broker, "snapshot", return_value=stale), \
             patch.object(self.f.broker, "submit", side_effect=AssertionError("risk check is unresolved")):
            self.engine.tick_account("test")
        self.assertTrue(self.f.store.get("post_fill_check:test"))
        self.assertIn("快照已过期", self.engine.state()["accounts"][0]["reason"])

    def test_state_does_not_hold_global_view_lock_during_database_reads(self):
        def read_accounts():
            acquired = threading.Event()
            def update_view():
                with self.engine.lock:
                    acquired.set()
            worker = threading.Thread(target=update_view)
            worker.start()
            try:
                self.assertTrue(acquired.wait(1), "state blocks all account views while waiting for database I/O")
            finally:
                worker.join(timeout=1)
            return [self.f.account]

        with patch.object(self.f.store, "accounts", side_effect=read_accounts):
            self.engine.state()

    def test_shutdown_skips_queued_account_network_and_reconciliation(self):
        self.engine.shutdown.set()
        with patch.object(self.f.broker, "snapshot", side_effect=AssertionError("must not send a new request")), \
             patch("trading.engine.Executor.reconcile", side_effect=AssertionError("must not reconcile")):
            self.engine.tick_account("test")

    def test_slow_account_does_not_block_other_account_and_its_own_ticks_serialize(self):
        self.engine.enable("test", False)
        second = {**account("second"), "enabled": False}
        self.f.store.save_account(second)
        other = PaperBroker("second", self.f.market, self.f.store)
        self.engine.brokers["second"] = other
        entered = threading.Event()
        release = threading.Event()
        other_done = threading.Event()
        snapshot = self.f.broker.snapshot(["XAUUSD1"])
        reads = []
        def slow_snapshot(*args, **kwargs):
            reads.append(threading.get_ident())
            entered.set()
            release.wait(3)
            return snapshot
        def second_tick():
            self.engine.tick_account("second")
            other_done.set()
        with patch.object(self.f.broker, "snapshot", side_effect=slow_snapshot):
            first = threading.Thread(target=self.engine.tick_account, args=("test",))
            same = threading.Thread(target=self.engine.tick_account, args=("test",))
            independent = threading.Thread(target=second_tick)
            first.start()
            try:
                self.assertTrue(entered.wait(1))
                same.start()
                independent.start()
                self.assertTrue(other_done.wait(1), "one account blocked an independent account")
                self.assertEqual(len(reads), 1, "same-account ticks overlapped")
            finally:
                release.set()
                for worker in (first, same, independent):
                    if worker.ident is not None:
                        worker.join(timeout=2)
        self.assertEqual(len(reads), 2)

    def test_adding_account_invalidates_scheduler_cache_immediately(self):
        first_seen = threading.Event()
        added_seen = threading.Event()
        def tick(account_id):
            (added_seen if account_id == "second" else first_seen).set()
            return 60
        with patch("trading.engine.ACCOUNT_LIST_INTERVAL", 60), \
             patch.object(self.engine, "tick_account", side_effect=tick), \
             patch.object(self.engine, "poll_market", return_value=60), \
             patch.object(self.engine, "notify", return_value=60):
            self.engine.start()
            try:
                self.assertTrue(first_seen.wait(1))
                self.engine.add_account(account("second"))
                self.assertTrue(added_seen.wait(1), "new account remained hidden until the cache interval")
            finally:
                self.engine.stop()

    def test_slow_eight_accounts_do_not_starve_market_or_notification_workers(self):
        for index in range(7):
            self.f.store.save_account(account("account" + str(index)))
        release = threading.Event()
        all_started = threading.Event()
        refreshed = threading.Event()
        notified = threading.Event()
        counters = {"accounts": 0, "markets": {}}
        guard = threading.Lock()

        def blocked_account(account_id):
            with guard:
                counters["accounts"] += 1
                if counters["accounts"] == 8:
                    all_started.set()
            release.wait(4)
            return 60

        def market(symbol):
            with guard:
                calls = counters["markets"].get(symbol, 0) + 1
                counters["markets"][symbol] = calls
                if calls >= 2:
                    refreshed.set()
            return .01

        def notify():
            notified.set()
            return .01

        with patch.object(self.engine, "tick_account", side_effect=blocked_account), \
             patch.object(self.engine, "poll_market", side_effect=market), \
             patch.object(self.engine, "notify", side_effect=notify), \
             patch.object(self.f.store, "accounts", wraps=self.f.store.accounts) as list_accounts:
            self.engine.start()
            try:
                self.assertTrue(all_started.wait(2), "all eight account jobs should start independently")
                self.assertTrue(refreshed.wait(1), "slow accounts starved the second market poll")
                self.assertTrue(notified.wait(1), "slow accounts starved the notification worker")
                time.sleep(.35)
                self.assertLessEqual(list_accounts.call_count, 2, "scheduler rereads all accounts every 100ms")
            finally:
                self.engine.shutdown.set()
                release.set()
                self.engine.stop()


if __name__ == "__main__":
    unittest.main()
