"""Cycle admission uses revocable hot state and never fetches before POST."""
import copy
from contextlib import nullcontext
from dataclasses import replace
import json
import sqlite3
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from trading.account_cache import CycleAccountCache
from trading.cycle_execution import CycleExecutor
from trading.exchange import HotAccountUnavailable, LiveBroker
from trading.models import TradingError, dec
from tests import test_cycle_execution as execution_cases


class RevocableLease:
    def __init__(self, snapshot):
        self.snapshot = copy.deepcopy(snapshot)
        self.started_monotonic = time.monotonic()
        self.error = None

    def require_fresh(self):
        if self.error:
            raise self.error


class HotBroker(LiveBroker):
    """Exercise live executor dispatch with a real local fill ledger."""
    def __init__(self, paper):
        self.paper = paper
        self.lease = None
        self.before_post = None
        self.cycle_hot_snapshot = Mock(side_effect=self.hot_snapshot)
        self.cycle_snapshot = Mock(side_effect=paper.cycle_snapshot)
        self.query = Mock(side_effect=paper.query)
        self.cancel = Mock(side_effect=paper.cancel)
        self.cycle_trades = Mock(side_effect=paper.cycle_trades)
        self.submit = Mock(side_effect=self.post)
        self.publish()

    def publish(self):
        if self.lease is not None:
            self.lease.error = HotAccountUnavailable("后台账户版本已变化")
        self.lease = RevocableLease(replace(self.paper.cycle_snapshot(["XAUUSD1"]), open_orders=None))

    def hot_snapshot(self, symbols):
        if self.lease is None:
            raise HotAccountUnavailable("账户热快照尚未就绪")
        return self.lease

    def post(self, orders):
        if self.before_post is not None:
            self.before_post(orders)
        # The adapter invalidates account leases when a write begins.
        self.lease.error = HotAccountUnavailable("本地提交已撤销旧账户版本")
        return self.paper.submit(orders)

    def cycle_volume_budget(self):
        return nullcontext()

    def reconciliation_budget(self):
        return nullcontext()


class CycleHotExecutionTests(unittest.TestCase):
    plan = execution_cases.CycleExecutionTests.plan
    progress_now = execution_cases.CycleExecutionTests.progress_now
    quantities = execution_cases.CycleExecutionTests.quantities

    def setUp(self):
        execution_cases.CycleExecutionTests.setUp(self)
        self.hot = HotBroker(self.f.broker)
        self.market = SimpleNamespace(rules=self.f.market.rules,
                                      cycle_book=Mock(side_effect=self.f.market.book),
                                      book=Mock(side_effect=self.f.market.book))
        self.executor = CycleExecutor(self.f.store, self.hot, self.market)

    def start_hot(self, phase="open", before_submit=None):
        snapshot = self.executor.prepare_snapshot(self.f.account)
        return self.executor.start(self.f.account, snapshot, self.plan(phase), self.progress_now(), before_submit)

    def assert_no_intent_or_post(self):
        self.assertIsNone(self.f.store.intent("test"))
        self.hot.submit.assert_not_called()
        self.hot.cycle_snapshot.assert_not_called()
        self.hot.query.assert_not_called()
        self.market.book.assert_not_called()

    def test_real_open_and_close_use_only_hot_state_until_the_first_post(self):
        for phase, expected in (("open", (2, 2)), ("close", (0, 0))):
            with self.subTest(phase=phase):
                if phase == "close":
                    progress = self.progress_now()
                    progress.update(opened_at=time.time() - 61, phase="waiting_close")
                    self.f.store.put("cycle:test", progress)
                    self.hot.publish()
                self.hot.cycle_snapshot.reset_mock()
                self.hot.cycle_hot_snapshot.reset_mock()
                self.hot.submit.reset_mock()
                self.market.cycle_book.reset_mock()
                self.market.book.reset_mock()
                def before_post(orders):
                    self.hot.cycle_snapshot.assert_not_called()
                    self.hot.query.assert_not_called()
                    self.market.book.assert_not_called()
                    self.market.cycle_book.assert_called_once_with("XAUUSD1")
                    self.assertIsNone(self.hot.lease.snapshot.open_orders)
                    intent = self.f.store.intent("test")
                    self.assertEqual(intent["status"], "pending")
                    self.assertEqual(intent["orders"], orders)
                    with self.f.store.connect() as db:
                        self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0], 2)
                    encoded = json.dumps(intent, allow_nan=False)
                    self.assertNotIn("_cycle_submit_guard", encoded)
                    self.assertNotIn("started_monotonic", encoded)
                self.hot.before_post = before_post
                self.start_hot(phase)
                self.hot.submit.assert_called_once()
                self.hot.cycle_hot_snapshot.assert_called_once_with(["XAUUSD1"])
                # Fill confirmation keeps its existing independent account read.
                self.hot.cycle_snapshot.assert_called_once_with(["XAUUSD1"])
                self.assertEqual(self.quantities(), expected)
                self.assertIsNone(self.f.store.intent("test"))
        self.assertEqual(self.progress_now()["completed_cycles"], 1)

    def test_cold_or_revoked_hot_lookup_never_falls_back_to_rest(self):
        original = self.hot.lease
        for state in ("cold", "stale", "disconnected", "account changed"):
            with self.subTest(state=state):
                self.hot.lease = None if state == "cold" else original
                original.error = HotAccountUnavailable(state)
                with self.assertRaises(HotAccountUnavailable):
                    self.start_hot()
                self.assert_no_intent_or_post()

    def test_an_old_background_read_cannot_gain_a_new_trigger_timestamp(self):
        self.hot.lease.started_monotonic = 100
        with patch("trading.cycle_execution.monotonic", return_value=108.001), \
             self.assertRaisesRegex(TradingError, "本轮账户快照已过期"):
            self.start_hot()
        self.assert_no_intent_or_post()

    def test_direct_or_copied_live_snapshot_also_requires_a_new_hot_lease(self):
        for prepared in (False, True):
            with self.subTest(prepared=prepared):
                selected = self.executor.prepare_snapshot(self.f.account) if prepared else self.hot.lease.snapshot
                selected = copy.deepcopy(selected)
                self.hot.cycle_hot_snapshot.reset_mock()
                stop = Mock(side_effect=TradingError("stopped after hot admission"))
                with self.assertRaisesRegex(TradingError, "stopped after hot admission"):
                    self.executor.start(self.f.account, selected, self.plan("open"), self.progress_now(), stop)
                stop.assert_called_once_with(self.hot.lease.snapshot)
                self.hot.cycle_hot_snapshot.assert_called_once_with(["XAUUSD1"])
                self.assert_no_intent_or_post()

    def test_direct_live_start_cannot_fetch_when_the_hot_cache_is_cold(self):
        selected = copy.deepcopy(self.hot.lease.snapshot)
        self.hot.lease = None
        with self.assertRaises(HotAccountUnavailable):
            self.executor.start(self.f.account, selected, self.plan("open"), self.progress_now())
        self.assert_no_intent_or_post()

    def test_direct_live_start_still_compares_current_quantity_and_leverage(self):
        for field, value in (("qty", dec(1)), ("leverage", 3)):
            with self.subTest(field=field):
                self.hot.publish()
                selected = copy.deepcopy(self.hot.lease.snapshot)
                for position in self.hot.lease.snapshot.pair("XAUUSD1"):
                    setattr(position, field, value)
                with self.assertRaisesRegex(TradingError, "仓位或杠杆已变化"):
                    self.executor.start(self.f.account, selected, self.plan("open"), self.progress_now())
                self.assert_no_intent_or_post()

    def test_disconnect_or_new_version_during_final_callback_blocks_before_persistence(self):
        for reason in ("private stream disconnected", "account update", "new background version"):
            with self.subTest(reason=reason):
                self.hot.publish()
                lease = self.hot.lease
                def invalidate(snapshot):
                    lease.error = HotAccountUnavailable(reason)
                with patch.object(self.f.store, "create_cycle_intent") as persist, \
                     self.assertRaisesRegex(HotAccountUnavailable, reason):
                    self.start_hot(before_submit=invalidate)
                persist.assert_not_called()
                self.assert_no_intent_or_post()

    def test_invalid_after_full_commit_is_known_not_sent_and_recovery_never_resubmits(self):
        created = []
        original = self.f.store.create_cycle_intent
        def persist_then_revoke(intent, message):
            original(intent, message)
            created.append(intent["id"])
            self.hot.lease.error = HotAccountUnavailable("private stream disconnected after commit")
        with patch.object(self.f.store, "create_cycle_intent", side_effect=persist_then_revoke), \
             patch.object(self.executor, "reconcile", return_value="simulate restart before reconciliation"):
            self.start_hot()
        pending = self.f.store.intent("test")
        self.assertEqual(pending["id"], created[0])
        self.assertTrue(all(row.get("local_not_sent") and row["status"] == "REJECTED"
                            and row["executedQty"] == "0" for row in pending["receipts"].values()))
        self.assertEqual(len(pending["receipts"]), 2)
        self.assertEqual(pending["execution_quality"]["timing"]["request_status"], "not_sent")
        self.assertEqual(pending["execution_quality"]["actual"]["status"], "not_sent")
        self.hot.submit.assert_not_called()
        self.hot.cycle_snapshot.assert_not_called()
        restored = CycleExecutor(self.f.store, self.hot, self.market)
        restored.reconcile(self.f.account)
        self.hot.submit.assert_not_called()
        self.hot.query.assert_not_called()
        self.hot.cycle_snapshot.assert_called_once_with(["XAUUSD1"])
        self.assertIsNone(self.f.store.intent("test"))
        self.assertEqual(self.progress_now()["phase"], "waiting_open")

    def test_observation_setup_failure_never_bypasses_the_post_commit_guard(self):
        original = self.f.store.create_cycle_intent
        def persist_then_revoke(intent, message):
            original(intent, message)
            self.hot.lease.error = HotAccountUnavailable("revoked after commit")
        with patch("trading.cycle_execution.ObservedBroker", side_effect=RuntimeError("display unavailable")), \
             patch.object(self.f.store, "create_cycle_intent", side_effect=persist_then_revoke):
            self.start_hot()
        self.hot.submit.assert_not_called()
        self.hot.query.assert_not_called()
        self.assertIsNone(self.f.store.intent("test"))
        self.assertEqual(self.executor.last_completed_intent["execution_quality"]["actual"]["status"], "not_sent")

    def test_snapshot_aging_during_commit_is_also_known_not_sent(self):
        original = self.f.store.create_cycle_intent
        def persist_then_expire(intent, message):
            original(intent, message)
            self.hot.lease.snapshot.timestamp -= 9
        with patch.object(self.f.store, "create_cycle_intent", side_effect=persist_then_expire):
            self.start_hot()
        self.hot.submit.assert_not_called()
        self.assertIsNone(self.f.store.intent("test"))
        self.assertTrue(all(row.get("local_not_sent") for row in self.executor.last_completed_intent["receipts"].values()))

    def test_real_cache_publication_after_commit_revokes_only_the_old_submission_lease(self):
        cache = CycleAccountCache()
        cache.configure(["XAUUSD1"])
        cache.set_connected(True)
        snapshot = self.hot.lease.snapshot
        self.assertTrue(cache.publish(cache.begin_refresh(), snapshot, time.monotonic()))
        self.hot.cycle_hot_snapshot = Mock(side_effect=cache.lease)
        original = self.f.store.create_cycle_intent
        def persist_then_publish(intent, message):
            original(intent, message)
            self.assertTrue(cache.publish(cache.begin_refresh(), snapshot, time.monotonic()))
        with patch.object(self.f.store, "create_cycle_intent", side_effect=persist_then_publish):
            self.start_hot()
        cache.lease(["XAUUSD1"]).require_fresh()
        self.hot.submit.assert_not_called()
        self.hot.query.assert_not_called()
        self.assertIsNone(self.f.store.intent("test"))
        self.assertTrue(all(row.get("local_not_sent") for row in self.executor.last_completed_intent["receipts"].values()))

    def test_persistence_failure_sends_nothing_and_clears_the_ephemeral_guard(self):
        with patch.object(self.f.store, "create_cycle_intent", side_effect=sqlite3.OperationalError("commit failed")), \
             self.assertRaisesRegex(sqlite3.OperationalError, "commit failed"):
            self.start_hot()
        self.assert_no_intent_or_post()
        self.assertIsNone(self.executor._cycle_submit_guard)

    def test_pending_pause_phase_and_external_ownership_still_block_hot_admission(self):
        snapshot = self.executor.prepare_snapshot(self.f.account)
        with patch.object(self.f.store, "intent", return_value={"status": "pending"}), \
             self.assertRaisesRegex(TradingError, "已有批次"):
            self.executor.start(self.f.account, snapshot, self.plan("open"), self.progress_now())
        with self.assertRaisesRegex(TradingError, "阶段无效"):
            self.executor.start(self.f.account, snapshot, replace(self.plan("open"), phase="wrong"), self.progress_now())
        self.hot.lease.snapshot.pair("XAUUSD1")[0].qty = dec(-1)
        with self.assertRaisesRegex(TradingError, "持仓数量"):
            self.start_hot()
        self.hot.publish()
        def pause(snapshot):
            self.f.store.pause_account(self.f.store.account("test"), "pause after final quote")
        with self.assertRaisesRegex(TradingError, "已暂停"):
            self.start_hot(before_submit=pause)
        self.assert_no_intent_or_post()

    def test_absent_public_hot_quote_cannot_invoke_the_rest_book_fallback(self):
        self.market.cycle_book.side_effect = TradingError("循环报价尚未就绪")
        with self.assertRaisesRegex(TradingError, "循环报价尚未就绪"):
            self.start_hot()
        self.assert_no_intent_or_post()


if __name__ == "__main__":
    unittest.main()
