"""Live position visibility can lag terminal order receipts; never trade on it."""
import copy
import time
import unittest
from unittest.mock import Mock, patch

from trading.cycle import CyclePlan, DEFAULT_CYCLE
from trading.cycle_execution import CycleExecutor
from trading.engine import Engine
from trading.exchange import ExchangeError, LiveBroker, RequestNotSent
from trading.models import TradingError, dec
from trading.store import Store
from .helpers import Fixture


class CyclePositionSyncTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.f.account["cycle"] = {**DEFAULT_CYCLE, "enabled": True}
        self.f.store.save_account(self.f.account)
        self.f.store.put("cycle:test", {
            "run_id": "position-sync", "phase": "waiting_open", "opened_at": None,
            "quantities": {"LONG": "0", "SHORT": "0"}, "completed_cycles": 0,
            "config": copy.deepcopy(self.f.account["cycle"]),
        })
        self.f.broker.set_cycle_leverage("XAUUSD1", 2)
        self.paper = CycleExecutor(self.f.store, self.f.broker, self.f.market)
        api = Mock()
        api.budget = None
        self.live = LiveBroker({}, self.f.market, api=api)
        self.live.cycle_snapshot = Mock(side_effect=self.f.broker.cycle_snapshot)
        self.live.query = Mock(side_effect=self.f.broker.query)
        self.live.submit = Mock(side_effect=self.f.broker.submit)
        self.executor = CycleExecutor(self.f.store, self.live, self.f.market)
        clock = patch("trading.cycle_execution.time.time", return_value=time.time())
        self.clock = clock.start()
        self.addCleanup(clock.stop)

    def advance(self, seconds=5):
        self.clock.return_value += seconds

    def pending(self, phase="open"):
        progress = self.f.store.get("cycle:test")
        if phase == "close":
            progress.update(opened_at=time.time() - 61, phase="waiting_close")
            self.f.store.put("cycle:test", progress)
        book = self.f.market.book("XAUUSD1")
        plan = CyclePlan(phase, "XAUUSD1", dec(2), 2, dec(2) * book.ask, dec(2) * book.bid, dec("0.02"))
        with patch.object(self.paper, "reconcile"):
            self.paper.start(self.f.account, self.f.broker.cycle_snapshot(["XAUUSD1"]), plan, progress)
        return self.f.store.intent("test")

    def observe(self, long_qty, short_qty):
        def read(symbols):
            snapshot = self.f.broker.cycle_snapshot(symbols)
            long, short = snapshot.pair("XAUUSD1")
            long.qty, short.qty = dec(long_qty), dec(short_qty)
            return snapshot
        self.live.cycle_snapshot.side_effect = read

    def reconcile(self):
        return self.executor.reconcile(self.f.account)

    def test_delayed_open_waits_without_resending_then_starts_holding(self):
        self.pending()
        self.observe("0", "2")
        self.assertIn("等待持仓同步", self.reconcile())
        self.assertTrue(self.f.store.account("test")["enabled"])
        self.assertIsNone(self.f.store.get("cycle:test")["opened_at"])
        self.advance()
        self.observe("2", "2")
        self.reconcile()
        self.assertIsNone(self.f.store.intent("test"))
        self.assertEqual(self.f.store.get("cycle:test")["opened_at"], time.time())
        self.assertNotIn("position_sync", self.executor.last_completed_intent)
        self.live.submit.assert_not_called()
        self.live.query.assert_not_called()

    def test_early_wakeups_do_not_poll_or_exhaust_confirmation_count(self):
        self.pending()
        self.observe("0", "0")
        with patch("trading.cycle_execution.time.sleep", side_effect=AssertionError("must yield")):
            self.reconcile()
            for _ in range(4):
                self.advance(1)
                self.assertIn("等待持仓同步", self.reconcile())
        self.assertEqual(self.live.cycle_snapshot.call_count, 1)
        self.assertEqual(self.f.store.intent("test")["position_sync"]["checks"], 1)
        self.advance(1)
        self.reconcile()
        self.assertEqual(self.live.cycle_snapshot.call_count, 2)
        self.live.submit.assert_not_called()

    def test_three_spaced_mismatches_pause_with_exact_quantities(self):
        self.pending()
        self.observe("3", "2")
        for check in (1, 2):
            self.assertIn("等待持仓同步", self.reconcile())
            self.assertTrue(self.f.store.account("test")["enabled"])
            self.assertEqual(self.f.store.intent("test")["position_sync"]["checks"], check)
            self.advance()
        reason = self.reconcile()
        self.assertIn("多头预期 2、实际 3", reason)
        self.assertIn("空头预期 2、实际 2", reason)
        self.assertFalse(self.f.store.account("test")["enabled"])
        self.assertEqual(self.f.store.intent("test")["status"], "attention")
        self.assertEqual(self.live.cycle_snapshot.call_count, 3)
        self.live.submit.assert_not_called()

    def test_restart_retains_interval_and_confirmation_count(self):
        self.pending()
        self.observe("0", "0")
        self.reconcile()
        self.executor = CycleExecutor(Store(self.f.store.path), self.live, self.f.market)
        self.reconcile()
        self.assertEqual(self.live.cycle_snapshot.call_count, 1)
        self.advance()
        self.reconcile()
        self.assertEqual(self.f.store.intent("test")["position_sync"]["checks"], 2)
        self.advance()
        self.reconcile()
        self.assertFalse(self.f.store.account("test")["enabled"])
        self.live.submit.assert_not_called()

    def test_delayed_close_does_not_resend_or_complete_before_visibility(self):
        self.pending()
        self.paper.reconcile(self.f.account)
        self.pending("close")
        self.observe("2", "2")
        self.reconcile()
        self.assertEqual(self.f.store.get("cycle:test")["completed_cycles"], 0)
        self.advance()
        self.observe("0", "0")
        self.reconcile()
        self.assertEqual(self.f.store.get("cycle:test")["completed_cycles"], 1)
        self.assertIsNone(self.f.store.intent("test"))
        self.live.submit.assert_not_called()

    def test_partial_open_waits_before_repair_and_repair_gets_own_window(self):
        submit = self.f.broker.submit
        with patch.object(self.f.broker, "submit", side_effect=lambda orders: [submit(orders[:1])[0], {"code": -2019}]):
            self.pending()
        self.observe("0", "0")
        self.reconcile()
        self.advance()
        self.reconcile()
        self.live.submit.assert_not_called()
        self.advance()
        # The opening now becomes visible; the repair's close is still delayed.
        self.observe("2", "0")
        self.reconcile()
        self.assertEqual(self.live.submit.call_count, 1)
        repair = self.live.submit.call_args.args[0]
        self.assertEqual([(o["positionSide"], o["side"], o["quantity"]) for o in repair], [("LONG", "SELL", "2")])
        pending = self.f.store.intent("test")
        self.assertEqual(pending["position_sync"]["checks"], 1)
        self.assertEqual(pending["position_sync"]["expected"], {"LONG": "0", "SHORT": "0"})
        self.advance()
        self.observe("0", "0")
        self.reconcile()
        self.assertIsNone(self.f.store.intent("test"))
        self.assertTrue(self.f.store.account("test")["enabled"])
        self.assertEqual(self.live.submit.call_count, 1)

    def test_failed_reads_do_not_count_as_mismatch_or_authorize_orders(self):
        self.pending()
        self.observe("0", "0")
        self.reconcile()
        self.advance()
        for error in (ExchangeError("network"), RequestNotSent("budget"), TradingError("stale snapshot")):
            with self.subTest(error=type(error).__name__):
                self.live.cycle_snapshot.side_effect = error
                with self.assertRaises(type(error)):
                    self.reconcile()
                self.assertEqual(self.f.store.intent("test")["position_sync"]["checks"], 1)
        self.observe("2", "2")
        self.reconcile()
        self.assertIsNone(self.f.store.intent("test"))
        self.live.submit.assert_not_called()

    def test_old_terminal_batch_still_gets_a_new_sync_window(self):
        intent = self.pending()
        intent["created_at"] -= 3600
        self.f.store.save_intent(intent)
        self.observe("0", "0")
        self.assertIn("等待持仓同步", self.reconcile())
        self.assertTrue(self.f.store.account("test")["enabled"])

    def test_leverage_change_is_not_treated_as_position_lag(self):
        self.pending()
        self.f.broker.state["leverages"]["XAUUSD1"] = 5
        self.f.broker.save()
        self.assertIn("杠杆被改变", self.reconcile())
        self.assertFalse(self.f.store.account("test")["enabled"])
        self.assertNotIn("position_sync", self.f.store.intent("test"))
        self.live.submit.assert_not_called()

    def test_later_match_does_not_reenable_a_paused_account(self):
        self.pending()
        self.observe("0", "0")
        for _ in range(3):
            self.reconcile()
            self.advance()
        self.observe("2", "2")
        self.reconcile()
        self.assertIsNone(self.f.store.intent("test"))
        self.assertFalse(self.f.store.account("test")["enabled"])
        self.live.submit.assert_not_called()

    def test_backwards_clock_does_not_cause_an_unbounded_wait(self):
        self.pending()
        self.observe("0", "0")
        self.reconcile()
        self.advance(-3600)
        self.reconcile()
        self.assertEqual(self.f.store.intent("test")["position_sync"]["checks"], 2)
        self.advance()
        self.reconcile()
        self.assertFalse(self.f.store.account("test")["enabled"])
        self.live.submit.assert_not_called()

    def test_delayed_positions_preserve_preexisting_baseline(self):
        self.f.broker.state["positions"].update({
            "XAUUSD1:" + side: {"qty": "1", "entry": "4400"} for side in ("LONG", "SHORT")
        })
        self.f.broker.save()
        self.pending()
        self.observe("1", "3")
        self.reconcile()
        self.advance()
        self.observe("3", "3")
        self.reconcile()
        progress = self.f.store.get("cycle:test")
        self.assertEqual(progress["baseline"], {"LONG": "1", "SHORT": "1"})
        self.assertEqual(progress["quantities"], {"LONG": "2", "SHORT": "2"})
        self.live.submit.assert_not_called()

    def test_account_worker_schedules_confirmation_without_pausing(self):
        self.pending()
        self.observe("0", "0")
        engine = Engine(self.f.store, market=self.f.market)
        delay = engine.tick_cycle_account(self.f.account, self.live, self.f.store.intent("test"))
        self.assertEqual(delay, 5)
        self.assertEqual(engine.views["test"]["status"], "reconciling")
        self.assertIn("等待持仓同步", engine.views["test"]["reason"])
        self.assertTrue(self.f.store.account("test")["enabled"])
        self.advance(delay)
        self.observe("2", "2")
        engine.tick_cycle_account(self.f.account, self.live, self.f.store.intent("test"))
        self.assertEqual(engine.views["test"]["status"], "running")
        self.assertIsNone(self.f.store.intent("test"))
        self.live.submit.assert_not_called()

    def test_confirmed_match_is_saved_even_if_later_repair_preparation_fails(self):
        submit = self.f.broker.submit
        with patch.object(self.f.broker, "submit", side_effect=lambda orders: [submit(orders[:1])[0], {"code": -2019}]):
            self.pending()
        self.observe("0", "0")
        self.reconcile()
        self.advance()
        self.observe("2", "0")
        snapshot = self.live.cycle_snapshot(["XAUUSD1"])
        with patch.object(self.live, "cycle_snapshot", return_value=snapshot), \
                patch.object(self.f.market, "book", side_effect=TradingError("stale book")), \
                self.assertRaisesRegex(TradingError, "stale book"):
            self.reconcile()
        self.assertNotIn("position_sync", self.f.store.intent("test"))
        self.live.submit.assert_not_called()

    def test_stale_snapshot_does_not_count_as_another_confirmation(self):
        self.pending()
        self.observe("0", "0")
        self.reconcile()
        snapshot = self.live.cycle_snapshot(["XAUUSD1"])
        self.advance(9)
        self.live.cycle_snapshot.side_effect = None
        self.live.cycle_snapshot.return_value = snapshot
        with self.assertRaises(TradingError):
            self.reconcile()
        self.assertEqual(self.f.store.intent("test")["position_sync"]["checks"], 1)
        self.assertTrue(self.f.store.account("test")["enabled"])
        self.live.submit.assert_not_called()

    def test_receipts_below_baseline_pause_without_waiting_for_positions(self):
        self.pending()
        self.paper.reconcile(self.f.account)
        intent = self.pending("close")
        repair = self.executor.order("XAUUSD1", "LONG", "SELL", dec(1), "impossible-repair")
        intent["repairs"].append(repair)
        intent["receipts"]["impossible-repair"] = {
            **repair, "clientOrderId": "impossible-repair", "status": "FILLED", "executedQty": "1", "avgPrice": "4400",
        }
        self.f.store.save_intent(intent)
        self.assertIn("回执推算持仓低于原始持仓", self.reconcile())
        self.assertFalse(self.f.store.account("test")["enabled"])
        self.assertNotIn("position_sync", self.f.store.intent("test"))
        self.live.submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
