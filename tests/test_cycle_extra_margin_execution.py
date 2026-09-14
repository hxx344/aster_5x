"""Cycle post-fill allowance, restart recovery and reduction boundaries."""
from copy import deepcopy
from fractions import Fraction
from unittest import TestCase
from unittest.mock import patch

from tests import test_cycle_execution as execution_cases
from trading.cycle_execution import CycleExecutor
from trading.models import AccountSnapshot, wire
from trading.paper import PaperBroker
from trading.store import Store


SYMBOL = "XAUUSD1"


class CycleExtraMarginExecutionTests(TestCase):
    setUp = execution_cases.CycleExecutionTests.setUp
    progress_now = execution_cases.CycleExecutionTests.progress_now
    plan = execution_cases.CycleExecutionTests.plan
    open = execution_cases.CycleExecutionTests.open
    close_cycle = execution_cases.CycleExecutionTests.close_cycle
    quantities = execution_cases.CycleExecutionTests.quantities

    def set_base(self, value):
        self.f.account["policy"]["margin_limit"] = value
        self.f.store.save_account(self.f.account)

    def set_wallet(self, value):
        self.f.broker.state["wallet"] = value
        self.f.broker.save()

    def set_equity(self, value):
        snapshot = self.f.broker.cycle_snapshot([SYMBOL])
        self.set_wallet(wire(Fraction(value) - Fraction(snapshot.unrealized)))

    def after_open_fill(self, change):
        original = self.f.broker.submit
        submitted = []
        def submit(orders):
            response = original(orders)
            if not submitted:
                submitted.append(True)
                change()
            return response
        return patch.object(self.f.broker, "submit", side_effect=submit)

    def test_2x_actual_margin_between_base_and_bonus_keeps_full_cycle_holding(self):
        with self.after_open_fill(lambda: self.set_wallet("17000")) as submit:
            self.open()
        snapshot = self.f.broker.cycle_snapshot([SYMBOL])
        ratio = snapshot.occupied_margin_exact / Fraction(snapshot.equity)
        self.assertGreater(ratio, Fraction("0.5"))
        self.assertLess(ratio, Fraction("0.55"))
        # The extra allowance raises the total ceiling; it is not a separate
        # five-percent cap on the cycle's own position margin.
        self.assertGreater(sum((position.occupied_margin_exact for position in snapshot.pair(SYMBOL)), Fraction(0)),
                           Fraction(snapshot.equity) * Fraction("0.05"))
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(self.quantities(), (2, 2))
        self.assertEqual(self.progress_now()["phase"], "holding")
        self.assertIsNone(self.f.store.intent("test"))

    def test_exact_effective_cap_and_one_hundred_percent_clamp_are_allowed(self):
        for base, cap in (("0.45", "0.5"), ("0.98", "1")):
            with self.subTest(base=base):
                self.set_base(base)
                def exact_equity():
                    snapshot = self.f.broker.cycle_snapshot([SYMBOL])
                    self.set_equity(snapshot.occupied_margin_exact / Fraction(cap))
                with self.after_open_fill(exact_equity) as submit:
                    self.open()
                snapshot = self.f.broker.cycle_snapshot([SYMBOL])
                self.assertEqual(snapshot.occupied_margin_exact, Fraction(cap) * Fraction(snapshot.equity))
                self.assertEqual(submit.call_count, 1)
                self.assertEqual(self.progress_now()["phase"], "holding")
                self.close_cycle()

    def test_tiny_actual_excess_over_effective_cap_rolls_back(self):
        self.set_base("0.45")
        def below_boundary_equity():
            snapshot = self.f.broker.cycle_snapshot([SYMBOL])
            self.set_equity(2 * snapshot.occupied_margin_exact - Fraction("0.000000000001"))
        with self.after_open_fill(below_boundary_equity) as submit:
            self.open()
        self.assertEqual(submit.call_count, 2)
        self.assertEqual(self.quantities(), (0, 0))
        progress = self.progress_now()
        self.assertEqual(progress["phase"], "waiting_open")
        self.assertIsNone(progress["opened_at"])
        self.assertIn("保证金占用", progress["reason"])

    def test_restart_pending_open_uses_bonus_without_resubmitting(self):
        with self.after_open_fill(lambda: self.set_wallet("17000")), \
             patch.object(self.executor, "reconcile", return_value="crash before position confirmation"):
            self.open()
        restored_store = Store(self.f.store.path)
        restored = PaperBroker("test", self.f.market, restored_store)
        executor = CycleExecutor(restored_store, restored, self.f.market)
        with patch.object(restored, "submit", side_effect=AssertionError("confirmed opening must not be resent")):
            executor.reconcile(restored_store.account("test"))
        self.assertEqual(self.progress_now()["phase"], "holding")
        self.assertEqual(self.quantities(), (2, 2))
        self.assertIsNone(restored_store.intent("test"))

    def test_restart_pending_open_above_bonus_only_sends_reductions(self):
        with self.after_open_fill(lambda: self.set_wallet("15000")), \
             patch.object(self.executor, "reconcile", return_value="crash before position confirmation"):
            self.open()
        restored_store = Store(self.f.store.path)
        restored = PaperBroker("test", self.f.market, restored_store)
        executor = CycleExecutor(restored_store, restored, self.f.market)
        with patch.object(restored, "submit", wraps=restored.submit) as submit:
            executor.reconcile(restored_store.account("test"))
        self.assertEqual(submit.call_count, 1)
        self.assertEqual([(order["positionSide"], order["side"], order["quantity"])
                          for order in submit.call_args.args[0]], [("LONG", "SELL", "2"), ("SHORT", "BUY", "2")])
        self.assertEqual(self.quantities(), (0, 0))
        self.assertIn("保证金占用", self.progress_now()["reason"])

    def test_all_other_symbols_count_but_rollback_preserves_their_positions(self):
        for symbol, quantity in (("CLUSD1", "100"), ("SPCXUSD1", "10")):
            for side in ("LONG", "SHORT"):
                self.f.broker.state["positions"][symbol + ":" + side] = {
                    "qty": quantity, "entry": wire(self.f.market.book(symbol).mark)}
        self.f.broker.save()
        external = {key: deepcopy(value) for key, value in self.f.broker.state["positions"].items()
                    if not key.startswith(SYMBOL + ":")}
        observed = []
        def observe_full_open():
            snapshot = self.f.broker.cycle_snapshot([SYMBOL])
            observed.append(snapshot)
        with self.after_open_fill(observe_full_open) as submit:
            self.open()
        snapshot = observed[0]
        cycle_margin = sum((position.occupied_margin_exact for position in snapshot.pair(SYMBOL)), Fraction(0))
        self.assertLess(cycle_margin, Fraction(snapshot.equity) * Fraction("0.55"))
        self.assertGreater(snapshot.occupied_margin_exact, Fraction(snapshot.equity) * Fraction("0.55"))
        self.assertEqual(submit.call_count, 2)
        self.assertTrue(all(order["symbol"] == SYMBOL for batch in submit.call_args_list for order in batch.args[0]))
        self.assertEqual(self.quantities(), (0, 0))
        self.assertEqual({key: value for key, value in self.f.broker.state["positions"].items()
                          if not key.startswith(SYMBOL + ":")}, external)

    def test_close_still_bypasses_opening_margin_checks_with_nonpositive_equity(self):
        self.open()
        self.set_wallet("-1")
        with patch.object(AccountSnapshot, "margin_exceeds", side_effect=AssertionError("closing must bypass opening cap")):
            self.close_cycle()
        self.assertEqual(self.quantities(), (0, 0))
        self.assertEqual(self.progress_now()["completed_cycles"], 1)

    def test_partial_open_repair_never_requires_opening_margin_room(self):
        original = self.f.broker.submit
        def single_leg(orders):
            if len(orders) == 2:
                result = [original(orders[:1])[0], {"code": -2019}]
                self.set_wallet("-1")
                return result
            return original(orders)
        with patch.object(self.f.broker, "submit", side_effect=single_leg) as submit, \
             patch.object(AccountSnapshot, "margin_exceeds", side_effect=AssertionError("repair must bypass opening cap")):
            self.open()
        self.assertEqual(submit.call_count, 2)
        self.assertEqual(self.quantities(), (0, 0))
        self.assertEqual(self.progress_now()["phase"], "waiting_open")
