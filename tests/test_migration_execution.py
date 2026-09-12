import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from trading.exchange import AmbiguousOrder, ExchangeError, RequestNotSent
from trading.migration_execution import MigrationExecutor
from trading.models import TradingError, dec
from trading.paper import PaperBroker
from trading.store import Store
from .helpers import Fixture


SOURCE, TARGET = "XAUUSD1", "SPCXUSD1"
SIDES = ("LONG", "SHORT")


class MigrationExecutionTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.f.account["policy"]["symbols"] = [SOURCE, TARGET, "CLUSD1"]
        self.f.account["migration"] = {"enabled": True, "spread_limit_bp": "5",
                                        "batch_notional": "1000", "notional_tolerance": "0.05"}
        self.f.store.save_account(self.f.account)
        for side in SIDES:
            self.f.broker.state["positions"][SOURCE + ":" + side] = {"qty": "5", "entry": "4412"}
            self.f.broker.state["positions"][TARGET + ":" + side] = {"qty": "2", "entry": "724"}
        self.f.broker.save()
        self.f.store.put("migration:test", {"run_id": "run", "source_initial_qty": dict.fromkeys(SIDES, "5"),
                                            "source_remaining_qty": dict.fromkeys(SIDES, "5"), "source_leverage": 5,
                                            "migrated_notional": dict.fromkeys(SIDES, "0"),
                                            "cumulative_notional_delta": dict.fromkeys(SIDES, "0"),
                                            "completed_batches": 0})
        self.executor = MigrationExecutor(self.f.store, self.f.broker, self.f.market)
        self.plan = SimpleNamespace(source_symbol=SOURCE, target_symbol=TARGET,
                                    source_quantities=dict.fromkeys(SIDES, dec(".227")), target_qty=dec("1.38"),
                                    source_leverage=5, target_leverage=5, notional_tolerance=dec(".05"))

    def start(self):
        snapshot = self.f.broker.snapshot(self.f.account["policy"]["symbols"])
        return self.executor.start(self.f.account, snapshot, self.plan, "run")

    def quantities(self, symbol):
        return tuple(p.qty for p in self.f.broker.snapshot([symbol]).pair(symbol))

    def assert_complete(self):
        self.assertIsNone(self.f.store.intent("test"))
        self.assertIsNotNone(self.executor.last_snapshot)
        self.assertEqual(self.executor.last_completed_intent["status"], "complete")
        result = self.executor.last_completed_intent["result"]
        for side in SIDES:
            source, target = dec(result["source_notional"][side]), dec(result["target_notional"][side])
            self.assertLessEqual(abs(target - source), source * dec(".05"))

    def test_opens_both_target_sides_before_reducing_source(self):
        with patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            self.start()
        self.assertEqual([[o["symbol"] for o in call.args[0]] for call in submit.call_args_list],
                         [[TARGET, TARGET], [SOURCE, SOURCE]])
        self.assertEqual(self.quantities(TARGET), (dec("3.38"), dec("3.38")))
        self.assertEqual(self.quantities(SOURCE), (dec("4.774"), dec("4.774")))
        self.assert_complete()
        self.assertEqual(self.f.store.get("migration:test")["completed_batches"], 1)
        self.assertIsNone(self.f.store.get("campaign:test"))
        self.assertEqual(self.f.store.get("post_fill_check:test"),
                         {"kind": "migration", "symbol": TARGET, "leverage": 5})

    def test_target_single_leg_rejection_rolls_back_only_new_target(self):
        original = self.f.broker.submit
        first = True
        def submit(orders):
            nonlocal first
            if first:
                first = False
                return [original(orders[:1])[0], {"code": -2019}]
            self.assertTrue(all(o["symbol"] == TARGET for o in orders))
            return original(orders)
        with patch.object(self.f.broker, "submit", side_effect=submit):
            self.start()
        self.assertEqual(self.quantities(SOURCE), (5, 5))
        self.assertEqual(self.quantities(TARGET), (2, 2))
        self.assertIsNone(self.f.store.intent("test"))
        self.assertEqual(self.executor.last_completed_intent["status"], "aborted")
        self.assertEqual(self.f.store.get("migration:test")["completed_batches"], 0)

    def test_partial_target_pair_sizes_source_from_real_fills(self):
        original = self.f.market.book
        def book(symbol):
            quote = original(symbol)
            return replace(quote, ask_qty=dec(".7"), bid_qty=dec(".7")) if symbol == TARGET else quote
        with patch.object(self.f.market, "book", side_effect=book):
            self.start()
        self.assertEqual(self.quantities(TARGET), (dec("2.7"), dec("2.7")))
        self.assertEqual(self.quantities(SOURCE), (dec("4.886"), dec("4.886")))
        self.assert_complete()

    def test_source_single_leg_rejection_is_repaired_with_source_reduction(self):
        original = self.f.broker.submit
        source_first = True
        def submit(orders):
            nonlocal source_first
            if orders[0]["symbol"] == SOURCE and source_first:
                source_first = False
                return [original(orders[:1])[0], {"code": -2019}]
            if orders[0]["symbol"] == SOURCE:
                self.assertEqual([(o["positionSide"], o["side"]) for o in orders], [("SHORT", "BUY")])
            return original(orders)
        with patch.object(self.f.broker, "submit", side_effect=submit):
            self.start()
        self.assertEqual(self.quantities(SOURCE), (dec("4.774"), dec("4.774")))
        self.assert_complete()

    def test_partial_source_pair_trims_target_without_reopening_source(self):
        original_book, original_submit = self.f.market.book, self.f.broker.submit
        target_opened = False
        def book(symbol):
            quote = original_book(symbol)
            if symbol == SOURCE and target_opened:
                return replace(quote, ask_qty=dec(".1"), bid_qty=dec(".1"))
            return quote
        def submit(orders):
            nonlocal target_opened
            result = original_submit(orders)
            if orders[0]["symbol"] == TARGET:
                target_opened = True
            else:
                self.assertTrue(all(o["side"] == ("SELL" if o["positionSide"] == "LONG" else "BUY") for o in orders))
            return result
        with patch.object(self.f.market, "book", side_effect=book), patch.object(self.f.broker, "submit", side_effect=submit):
            self.start()
        self.assertEqual(self.quantities(SOURCE), (dec("4.9"), dec("4.9")))
        self.assertLess(self.quantities(TARGET)[0], dec("3.38"))
        self.assertGreater(self.quantities(TARGET)[0], 2)
        self.assert_complete()

    def test_unknown_target_result_never_closes_source_or_resends(self):
        with patch.object(self.f.broker, "submit", side_effect=AmbiguousOrder("timeout")) as submit, \
             patch.object(self.f.broker, "query", side_effect=ExchangeError("not visible", code=-2013)):
            self.start()
            intent = self.f.store.intent("test")
            intent["order_times"] = dict.fromkeys(intent["order_times"], time.time() - 121)
            self.f.store.save_intent(intent)
            self.executor.reconcile(self.f.account)
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(self.quantities(SOURCE), (5, 5))
        self.assertEqual(self.f.store.intent("test")["status"], "attention")

    def test_restart_after_target_acceptance_does_not_duplicate_opening(self):
        original = self.f.broker.submit
        def accepted(orders):
            original(orders)
            raise RuntimeError("process lost")
        with patch.object(self.f.broker, "submit", side_effect=accepted), self.assertRaises(RuntimeError):
            self.start()
        store = Store(self.f.store.path)
        broker = PaperBroker("test", self.f.market, store)
        self.executor = MigrationExecutor(store, broker, self.f.market)
        with patch.object(broker, "submit", wraps=broker.submit) as submit, patch.object(broker, "query", wraps=broker.query) as query:
            self.executor.reconcile(self.f.account)
        self.assertTrue(all(call.args[0][0]["symbol"] == SOURCE for call in submit.call_args_list))
        self.assertTrue(all(call.args[0] == TARGET for call in query.call_args_list))
        self.f.broker.reload()
        self.assert_complete()

    def test_restart_after_source_acceptance_does_not_duplicate_reduction(self):
        original = self.f.broker.submit
        def accepted(orders):
            result = original(orders)
            if orders[0]["symbol"] == SOURCE:
                raise RuntimeError("process lost")
            return result
        with patch.object(self.f.broker, "submit", side_effect=accepted), self.assertRaises(RuntimeError):
            self.start()
        store = Store(self.f.store.path)
        broker = PaperBroker("test", self.f.market, store)
        self.executor = MigrationExecutor(store, broker, self.f.market)
        with patch.object(broker, "submit", side_effect=AssertionError("do not repeat")), patch.object(broker, "query", wraps=broker.query) as query:
            self.executor.reconcile(self.f.account)
        self.assertTrue(all(call.args[0] == SOURCE for call in query.call_args_list))
        self.assert_complete()

    def test_switch_off_and_account_pause_still_finish_submitted_migration(self):
        with patch.object(self.executor, "reconcile", return_value="pending"):
            self.start()
        self.f.account["enabled"] = False
        self.f.account["migration"]["enabled"] = False
        with patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            self.executor.reconcile(self.f.account)
        self.assertTrue(all(call.args[0][0]["symbol"] == SOURCE for call in submit.call_args_list))
        self.assert_complete()

    def test_external_position_change_stops_before_source_writes(self):
        with patch.object(self.executor, "reconcile", return_value="pending"):
            self.start()
        self.f.broker.state["positions"][SOURCE + ":LONG"]["qty"] = "4.9"
        self.f.broker.save()
        with patch.object(self.f.broker, "submit") as submit:
            self.executor.reconcile(self.f.account)
        submit.assert_not_called()
        self.assertEqual(self.f.store.intent("test")["status"], "attention")

    def test_leverage_change_stops_before_source_writes(self):
        with patch.object(self.executor, "reconcile", return_value="pending"):
            self.start()
        self.f.broker.state["leverages"][TARGET] = 10
        self.f.broker.save()
        with patch.object(self.f.broker, "submit") as submit:
            self.executor.reconcile(self.f.account)
        submit.assert_not_called()
        self.assertIn("杠杆", self.f.store.intent("test")["last_error"])

    def test_source_repairs_are_bounded_and_never_exceed_batch_budget(self):
        original = self.f.broker.submit
        first_source = True
        def submit(orders):
            nonlocal first_source
            if orders[0]["symbol"] != SOURCE:
                return original(orders)
            if first_source:
                first_source = False
                return [original(orders[:1])[0], {"code": -2019}]
            self.assertLessEqual(dec(orders[0]["quantity"]), self.plan.source_quantities["SHORT"])
            return [{"code": -2019} for _ in orders]
        with patch.object(self.f.broker, "submit", side_effect=submit):
            self.start()
        intent = self.f.store.intent("test")
        self.assertEqual(intent["status"], "attention")
        self.assertEqual(intent["attempts"]["balance_source"], 3)
        self.assertEqual(self.quantities(SOURCE), (dec("4.774"), 5))

    def test_tail_below_ordinary_500_floor_can_clear_source(self):
        for side in SIDES:
            self.f.broker.state["positions"][SOURCE + ":" + side]["qty"] = ".1"
        self.f.broker.save()
        self.plan.source_quantities = dict.fromkeys(SIDES, dec(".1"))
        self.plan.target_qty = dec(".609")
        self.plan.is_tail = True
        self.start()
        self.assertEqual(self.quantities(SOURCE), (0, 0))
        self.assert_complete()
        self.assertEqual(self.f.store.get("migration:test")["phase"], "complete")

    def test_bad_plan_and_disabled_switch_do_not_create_intent(self):
        self.plan.target_leverage = 1
        with patch.object(self.f.broker, "submit") as submit, self.assertRaises(TradingError):
            self.start()
        submit.assert_not_called()
        self.assertIsNone(self.f.store.intent("test"))
        self.plan.target_leverage = 5
        self.f.account["migration"]["enabled"] = False
        with self.assertRaises(TradingError):
            self.start()

    def test_final_admission_failure_does_not_persist_or_submit(self):
        snapshot = self.f.broker.snapshot(self.f.account["policy"]["symbols"])
        with patch.object(self.f.broker, "submit") as submit, self.assertRaisesRegex(TradingError, "quota changed"):
            self.executor.start(self.f.account, snapshot, self.plan, "run",
                                before_submit=lambda _: (_ for _ in ()).throw(TradingError("quota changed")))
        submit.assert_not_called()
        self.assertIsNone(self.f.store.intent("test"))

    def test_locally_rejected_target_orders_are_known_absent(self):
        with patch.object(self.f.broker, "submit", side_effect=RequestNotSent("budget", retry_after=60)), \
             patch.object(self.f.broker, "query", side_effect=AssertionError("known absent")):
            self.start()
        self.assertIsNone(self.f.store.intent("test"))
        self.assertEqual(self.quantities(SOURCE), (5, 5))
        self.assertEqual(self.quantities(TARGET), (2, 2))

    def test_repeated_completion_is_idempotent(self):
        self.start()
        intent = self.executor.last_completed_intent
        progress = self.f.store.get("migration:test")
        self.f.store.complete_migration(intent, intent["result"], progress["source_remaining_qty"])
        self.assertEqual(self.f.store.get("migration:test"), progress)

    def test_small_ordinary_plan_is_rejected_without_tail_exception(self):
        self.plan.target_qty = dec(".609")
        with patch.object(self.f.broker, "submit") as submit, self.assertRaisesRegex(TradingError, "500"):
            self.start()
        submit.assert_not_called()
        self.assertIsNone(self.f.store.intent("test"))

    def test_target_trim_single_leg_rejection_recovers_without_touching_baseline(self):
        original_book, original_submit = self.f.market.book, self.f.broker.submit
        target_opened, rejected_trim = False, False
        def book(symbol):
            quote = original_book(symbol)
            if symbol == SOURCE and target_opened:
                return replace(quote, ask_qty=dec(".1"), bid_qty=dec(".1"))
            return quote
        def submit(orders):
            nonlocal target_opened, rejected_trim
            reducing_target = orders[0]["symbol"] == TARGET and orders[0]["side"] == ("SELL" if orders[0]["positionSide"] == "LONG" else "BUY")
            if reducing_target and not rejected_trim:
                rejected_trim = True
                return [original_submit(orders[:1])[0], {"code": -2019}]
            result = original_submit(orders)
            if orders[0]["symbol"] == TARGET:
                target_opened = True
            return result
        with patch.object(self.f.market, "book", side_effect=book), patch.object(self.f.broker, "submit", side_effect=submit):
            self.start()
        self.assertTrue(rejected_trim)
        self.assertEqual(self.quantities(TARGET)[0], self.quantities(TARGET)[1])
        self.assertGreater(self.quantities(TARGET)[0], 2)
        self.assert_complete()

    def test_zero_tolerance_is_accepted_and_frozen_before_submission(self):
        self.plan.notional_tolerance = dec(0)
        with patch.object(self.executor, "reconcile", return_value="pending"):
            self.start()
        intent = self.f.store.intent("test")
        self.assertEqual(intent["notional_tolerance"], "0")
        self.f.account["migration"]["notional_tolerance"] = ".5"
        self.executor.reconcile(self.f.account)
        intent = self.f.store.intent("test")
        self.assertEqual(intent["notional_tolerance"], "0")
        self.assertEqual(intent["status"], "attention")

    def test_repair_budget_denial_is_refunded_and_recovery_uses_fresh_ids(self):
        original = self.f.broker.submit
        opened, denied = False, False
        def submit(orders):
            nonlocal opened, denied
            if not opened:
                opened = True
                return [original(orders[:1])[0], {"code": -2019}]
            denied = True
            raise RequestNotSent("budget", retry_after=60)
        with patch.object(self.f.broker, "submit", side_effect=submit), self.assertRaises(RequestNotSent):
            self.start()
        self.assertTrue(denied)
        intent = self.f.store.intent("test")
        self.assertEqual(intent["attempts"]["balance_target"], 0)
        previous_ids = {o["newClientOrderId"] for o in intent["orders"]}
        with patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            self.executor.reconcile(self.f.account)
        self.assertEqual(self.quantities(TARGET), (2, 2))
        self.assertIsNone(self.f.store.intent("test"))
        self.assertTrue(all(o["newClientOrderId"] not in previous_ids for call in submit.call_args_list for o in call.args[0]))


if __name__ == "__main__":
    unittest.main()
