from dataclasses import replace
import unittest
from unittest.mock import patch

from trading.engine import Engine, snapshot_json
from trading.execution import Executor
from trading.models import Position, TradingError, dec, plan_pair
from .helpers import Fixture


class OccupancyTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.snapshot = self.f.broker.snapshot(["XAUUSD1"])
        self.book = self.f.market.book("XAUUSD1")
        self.rules = self.f.market.rules["XAUUSD1"]

    def plan(self, snapshot=None, book=None):
        return plan_pair(snapshot or self.snapshot, book or self.book, self.rules, {4: dec(500000)}, self.f.account["policy"])

    def test_all_positions_and_leverages_count_without_long_short_netting(self):
        positions = [
            Position("XAUUSD1", "LONG", dec(10), dec(800), dec(1000), 4),
            Position("XAUUSD1", "SHORT", dec(10), dec(1200), dec(1000), 4),
            Position("SPCXUSD1", "LONG", dec(50), dec(50), dec(100), 10),
            Position("CLUSD1", "SHORT", dec(10), dec(90), dec(100), 5),
        ]
        snapshot = replace(self.snapshot, positions=positions, equity=dec(20000), maintenance=dec(1))
        self.assertEqual(snapshot.occupied_margin, 5700)
        self.assertEqual(snapshot.ratio, dec(".285"))
        wire = snapshot_json(snapshot, ["XAUUSD1"])
        self.assertEqual(wire["occupied_margin"], "5700")
        self.assertEqual(wire["positions"][0]["notional"], "10000")
        self.assertEqual(wire["positions"][0]["occupied_margin"], "2500")

    def test_maintenance_margin_does_not_determine_opening_limit(self):
        changed = replace(self.snapshot, maintenance=self.snapshot.equity)
        self.assertEqual(changed.ratio, 0)
        self.assertGreater(self.plan(changed).qty, 0)
        self.assertEqual(self.plan(changed).qty, self.plan().qty)

    def test_projected_exact_fifty_percent_is_allowed(self):
        snapshot = replace(self.snapshot, equity=dec(1000), available=dec(1000), fees={"XAUUSD1": dec(0)})
        book = replace(self.book, bid=dec(100), ask=dec(100), mark=dec(100))
        plan = self.plan(snapshot, book)
        self.assertEqual(plan.qty, 10)
        self.assertEqual(plan.projected_ratio, dec(".5"))

    def test_fees_reduce_equity_and_prevent_the_next_quantity_step(self):
        snapshot = replace(self.snapshot, equity=dec(1000), available=dec(1000))
        book = replace(self.book, bid=dec(100), ask=dec(100), mark=dec(100))
        plan = self.plan(snapshot, book)
        self.assertLess(plan.qty, 10)
        self.assertLessEqual(plan.projected_ratio, dec(".5"))
        larger = plan.qty + self.rules.step
        independent_ratio = (2 * larger * 100 / 4) / (1000 - 2 * larger * 100 * dec(".0004"))
        self.assertGreater(independent_ratio, dec(".5"))

    def test_invalid_position_leverage_and_nonpositive_equity_are_rejected(self):
        for leverage in (0, -1, True, 126):
            with self.subTest(leverage=leverage), self.assertRaises(TradingError):
                replace(self.snapshot.positions[0], leverage=leverage).occupied_margin
        with self.assertRaises(TradingError):
            replace(self.snapshot, equity=dec(0)).ratio

    def test_high_existing_occupancy_can_release_margin_before_adding(self):
        self.f.broker.state["wallet"] = "10000"
        self.f.broker.state["leverages"]["XAUUSD1"] = 1
        for side in ("LONG", "SHORT"):
            self.f.broker.state["positions"]["XAUUSD1:" + side] = {"qty": "1", "entry": "4412"}
        self.f.broker.save()
        engine = Engine(self.f.store, market=self.f.market)
        engine.brokers["test"] = self.f.broker
        engine.poll_market("XAUUSD1")
        self.assertGreater(self.f.broker.snapshot(["XAUUSD1"]).ratio, dec(".5"))
        engine.enable("test", True)
        with patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            engine.tick_account("test")
            submit.assert_not_called()
            engine.tick_account("test")
            submit.assert_not_called()
            after_upgrade = self.f.broker.snapshot(["XAUUSD1"])
            self.assertEqual(after_upgrade.pair("XAUUSD1")[0].leverage, 4)
            self.assertLess(after_upgrade.ratio, dec(".5"))
            engine.tick_account("test")
            submit.assert_called_once()
        final = self.f.broker.snapshot(["XAUUSD1"])
        self.assertGreater(final.pair("XAUUSD1")[0].qty, 1)
        self.assertLessEqual(final.ratio, dec(".5"))

    def test_actual_occupancy_over_limit_after_fill_persistently_pauses(self):
        engine = Engine(self.f.store, market=self.f.market)
        engine.brokers["test"] = self.f.broker
        engine.poll_market("XAUUSD1")
        original = self.f.broker.submit
        def fill_then_equity_changes(orders):
            receipts = original(orders)
            self.f.broker.state["wallet"] = "100"
            self.f.broker.save()
            return receipts
        with patch.object(self.f.broker, "submit", side_effect=fill_then_equity_changes):
            engine.tick_account("test")
        self.assertFalse(self.f.store.account("test")["enabled"])
        self.assertGreater(self.f.broker.snapshot(["XAUUSD1"]).ratio, dec(".5"))
        self.assertIn("保证金占用率超过", engine.state()["accounts"][0]["reason"])

    def test_recovered_fill_and_crash_before_post_fill_check_both_pause_on_over_limit(self):
        for leave_pending in (True, False):
            with self.subTest(leave_pending=leave_pending):
                f = Fixture()
                try:
                    snapshot = f.broker.snapshot(["XAUUSD1"])
                    book = f.market.book("XAUUSD1")
                    plan = plan_pair(snapshot, book, f.market.rules["XAUUSD1"], {4: dec(500000)}, f.account["policy"])
                    executor = Executor(f.store, f.broker, f.market)
                    if leave_pending:
                        with patch.object(executor, "reconcile", return_value="simulated process interruption"):
                            executor.open_pair(f.account, snapshot, "XAUUSD1", plan, book)
                    else:
                        executor.open_pair(f.account, snapshot, "XAUUSD1", plan, book)
                        self.assertTrue(f.store.get("post_fill_check:test"))
                    f.broker.state["wallet"] = "100"
                    f.broker.save()
                    engine = Engine(f.store, market=f.market)
                    engine.brokers["test"] = f.broker
                    engine.poll_market("XAUUSD1")
                    with patch.object(f.broker, "submit", side_effect=AssertionError("must not add before risk check")), \
                         patch.object(f.broker, "set_leverage", side_effect=AssertionError("must pause this unchecked fill")):
                        engine.tick_account("test")
                    self.assertFalse(f.store.account("test")["enabled"])
                    self.assertIsNone(f.store.intent("test"))
                    self.assertIsNone(f.store.get("post_fill_check:test"))
                    self.assertEqual(engine.state()["accounts"][0]["status"], "attention")
                finally:
                    f.close()
