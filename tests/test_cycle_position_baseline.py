"""Round trips preserve original quantities, including unequal or single legs."""
import copy
from dataclasses import replace
import time
import unittest
from unittest.mock import patch

from tests import test_cycle_execution as execution_cases
from tests import test_cycle_planning as planning_cases
from trading.cycle import CyclePlan, CyclePositionError, plan_cycle, validate_cycle_positions
from trading.cycle_execution import CycleExecutor
from trading.models import TradingError, dec
from trading.paper import PaperBroker
from trading.store import Store


class CycleBaselineExecutionTests(unittest.TestCase):
    setUp = execution_cases.CycleExecutionTests.setUp
    plan = execution_cases.CycleExecutionTests.plan
    open = execution_cases.CycleExecutionTests.open
    close_cycle = execution_cases.CycleExecutionTests.close_cycle
    progress_now = execution_cases.CycleExecutionTests.progress_now
    quantities = execution_cases.CycleExecutionTests.quantities

    def seed(self, long="1", short="0.5"):
        for side, qty in (("LONG", long), ("SHORT", short)):
            self.f.broker.state["positions"]["XAUUSD1:" + side] = {"qty": qty, "entry": "4412"}
        self.f.broker.save()

    def test_unequal_and_single_sided_original_positions_survive_multiple_rounds(self):
        for baseline in (("1", "0.5"), ("0", "1"), ("1", "0")):
            self.seed(*baseline)
            for _ in range(2):
                self.open()
                self.assertEqual(self.quantities(), tuple(dec(q) + 2 for q in baseline))
                self.assertEqual(self.progress_now()["quantities"], {"LONG": "2", "SHORT": "2"})
                self.close_cycle()
                self.assertEqual(self.quantities(), tuple(dec(q) for q in baseline))
                self.assertEqual(self.f.broker.state["leverages"]["XAUUSD1"], 2)
        self.assertEqual(self.progress_now()["completed_cycles"], 6)

    def test_partial_open_rolls_back_only_the_newly_filled_leg(self):
        self.seed()
        original = self.f.broker.submit
        def partial(orders):
            return [original(orders[:1])[0], {"code": -2019}] if len(orders) == 2 else original(orders)
        with patch.object(self.f.broker, "submit", side_effect=partial) as send:
            self.open()
        self.assertEqual(self.quantities(), (dec(1), dec("0.5")))
        self.assertEqual(send.call_count, 2)
        repair = send.call_args_list[1].args[0][0]
        self.assertEqual((repair["positionSide"], repair["side"], repair["quantity"]), ("LONG", "SELL", "2"))
        self.assertEqual(self.progress_now()["completed_cycles"], 0)

    def test_partial_close_recovers_after_restart_without_reducing_the_baseline(self):
        self.seed()
        self.open()
        original = self.f.broker.submit
        def partial(orders):
            return [original(orders[:1])[0], {"code": -2019}]
        with patch.object(self.f.broker, "submit", side_effect=partial), patch.object(self.executor, "reconcile"):
            self.close_cycle()
        reopened = Store(self.f.store.path)
        broker = PaperBroker("test", self.f.market, reopened)
        executor = CycleExecutor(reopened, broker, self.f.market)
        with patch.object(broker, "submit", wraps=broker.submit) as send:
            executor.reconcile(self.f.account)
            executor.reconcile(self.f.account)
        self.assertEqual(send.call_count, 1)
        repair = send.call_args.args[0][0]
        self.assertEqual((repair["positionSide"], repair["quantity"]), ("SHORT", "2"))
        self.assertEqual(self.quantities(), (dec(1), dec("0.5")))
        self.assertEqual(self.progress_now()["completed_cycles"], 1)

    def test_external_change_during_recovery_pauses_and_does_not_try_to_restore_by_adding(self):
        self.seed()
        with patch.object(self.executor, "reconcile"):
            self.open()
        self.f.broker.state["positions"]["XAUUSD1:LONG"]["qty"] = "0.5"
        self.f.broker.save()
        with patch.object(self.f.broker, "submit") as send:
            self.executor.reconcile(self.f.account)
        send.assert_not_called()
        self.assertEqual(self.f.store.intent("test")["status"], "attention")

    def test_changed_leverage_during_hold_cannot_authorize_reduction(self):
        self.seed()
        self.open()
        self.f.broker.state["leverages"]["XAUUSD1"] = 5
        self.f.broker.save()
        with patch.object(self.f.broker, "submit") as send, self.assertRaises(CyclePositionError):
            self.close_cycle()
        send.assert_not_called()

    def test_tampered_baseline_cannot_reclassify_original_holdings_as_cycle_quantity(self):
        self.seed()
        self.open()
        progress = self.progress_now()
        progress.update(opened_at=time.time() - 61, phase="waiting_close")
        self.f.store.put("cycle:test", progress)
        forged = copy.deepcopy(progress)
        forged["baseline"]["LONG"] = "0"
        with patch.object(self.f.broker, "submit") as send, self.assertRaises(TradingError):
            self.executor.start(self.f.account, self.f.broker.cycle_snapshot(["XAUUSD1"]), self.plan("close"), forged)
        send.assert_not_called()

    def test_selection_change_at_final_callback_prevents_a_second_symbol_from_trading(self):
        def change(_snapshot):
            account = self.f.store.account("test")
            account["cycle"]["symbol"] = "CLUSD1"
            self.f.store.save_account(account)
        with patch.object(self.f.broker, "submit") as send, self.assertRaises(TradingError):
            self.executor.start(self.f.account, self.f.broker.cycle_snapshot(["XAUUSD1"]), self.plan("open"), self.progress_now(), before_submit=change)
        send.assert_not_called()
        self.assertIsNone(self.f.store.intent("test"))


class CycleBaselinePlanningTests(unittest.TestCase):
    setUp = planning_cases.CyclePlanningTests.setUp
    plan = planning_cases.CyclePlanningTests.plan

    def test_current_leverage_limit_deducts_both_original_legs(self):
        self.snapshot.positions[0].qty = dec(3)
        self.snapshot.positions[1].qty = dec(1)
        self.snapshot.current_leverage_caps = {"XAUUSD1": (2, dec(1000))}
        self.assertEqual(self.plan().qty, dec(3))  # 400 held + 600 added = 1000.
        self.snapshot.current_leverage_caps["XAUUSD1"] = (2, dec(400))
        with self.assertRaises(TradingError):
            self.plan()

    def test_each_supported_symbol_and_actual_leverage_is_selected_without_configuration_mutation(self):
        for symbol, leverage in (("XAUUSD1", 2), ("SPCXUSD1", 17), ("CLUSD1", 5)):
            self.account["cycle"]["symbol"] = symbol
            self.snapshot.positions = [replace(p, symbol=symbol, qty=dec(1), leverage=leverage) for p in self.snapshot.positions]
            self.snapshot.fees = {symbol: dec("0.0004")}
            self.snapshot.current_leverage_caps = {symbol: (leverage, dec(1000000))}
            before = copy.deepcopy(self.account)
            result = self.plan(rule=replace(self.rule, symbol=symbol))
            self.assertEqual((result.symbol, result.leverage), (symbol, leverage))
            self.assertEqual(self.account, before)

    def test_only_added_quantity_is_planned_for_close_even_without_new_opening_room(self):
        self.progress.update(phase="holding", baseline={"LONG": "3", "SHORT": "1"},
                             quantities={"LONG": "2", "SHORT": "2"}, opened_at=self.now - 61)
        self.snapshot.positions[0].qty = dec(5)
        self.snapshot.positions[1].qty = dec(3)
        self.snapshot.current_leverage_caps = {"XAUUSD1": (2, dec(0))}
        self.snapshot.equity = self.snapshot.available = dec(-1)
        self.assertEqual((self.plan().phase, self.plan().qty), ("close", dec(2)))
        self.snapshot.positions[1].qty = dec("3.001")
        with self.assertRaises(CyclePositionError):
            self.plan()
