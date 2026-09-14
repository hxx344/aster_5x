"""Only completed cycle condition checks may be condensed in the event log."""
from unittest import TestCase
from unittest.mock import patch

from tests import test_cycle_engine as engine_cases
from trading.cycle_diagnostics import CycleConditionError, diagnostic_error
from trading.exchange import BudgetWait, ExchangeError
from trading.models import AccountModeError, SYMBOLS, TradingError


class CycleCheckEventEngineTests(TestCase):
    setUp = engine_cases.CycleEngineTests.setUp
    select = engine_cases.CycleEngineTests.select

    def start(self, parallel=False):
        if parallel:
            owner = self.f.store.account("test")
            owner["policy"]["symbols"] = list(SYMBOLS)
            self.f.store.save_account(owner)
        self.select()
        self.engine.enable("test", True)

    def condition(self, value="0.6", code="reference_spread", phase="open"):
        return diagnostic_error(code, "循环检查条件未满足", symbol="XAUUSD1", phase=phase,
                                checked_at=100, checks=[{"label": "价差", "actual": value,
                                "required": "<= 0.3", "unit": "bp", "passed": False}])

    def tick_failure(self, exc):
        with patch.object(self.engine, "tick_cycle_account", side_effect=exc):
            return self.engine.tick_account("test")

    def checks(self):
        return [event for event in self.f.store.events() if event["kind"] == "cycle_check"]

    def exercise_repeated_wait(self, parallel):
        self.start(parallel)
        expected_delay = 5 if parallel else 10
        first = self.condition()
        self.assertEqual(self.tick_failure(first), expected_delay)
        before = self.checks()[0]
        self.assertEqual(self.tick_failure(first), expected_delay)
        latest = self.condition("0.44", code="cycle_minimum_order")
        self.assertEqual(self.tick_failure(latest), expected_delay)
        self.assertEqual(len(self.checks()), 1)
        current = self.checks()[0]
        self.assertEqual(current["id"], before["id"])
        self.assertEqual(current["message"], str(latest))
        self.assertEqual(current["cycle_check"]["count"], 3)
        self.assertEqual(current["cycle_check"]["first_at"], before["cycle_check"]["first_at"])
        self.assertEqual(current["cycle_check"]["diagnostic"], latest.diagnostic)
        self.assertEqual(self.f.broker.state["orders"], {})
        self.assertIsNone(self.f.store.intent("test"))
        self.assertTrue(self.f.store.account("test")["enabled"])

    def test_cycle_only_repeated_numeric_and_condition_changes_share_one_event(self):
        self.exercise_repeated_wait(False)

    def test_parallel_repeated_numeric_and_condition_changes_share_one_event(self):
        self.exercise_repeated_wait(True)

    def test_ordinary_branch_condition_error_keeps_original_error_event(self):
        self.start(parallel=True)
        error = self.condition()
        with patch.object(self.engine, "tick_cycle_account", return_value=5), \
             patch.object(self.f.broker, "snapshot", side_effect=error):
            self.assertEqual(self.engine.tick_account("test"), 10)
        self.assertEqual(self.checks(), [])
        self.assertEqual(self.f.store.events()[0]["kind"], "error")
        self.assertNotIn("cycle_check", self.f.store.events()[0])

    def test_operational_errors_remain_separate_and_break_check_groups(self):
        self.start()
        errors = (TradingError("循环费用字段缺失"), ExchangeError("循环接口请求失败"), BudgetWait("预算不足"))
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.tick_failure(self.condition())
                previous = self.checks()[0]["id"]
                self.tick_failure(error)
                event = self.f.store.events()[0]
                self.assertEqual(event["kind"], "wait" if isinstance(error, BudgetWait) else "error")
                self.assertEqual(event["message"], str(error))
                self.assertNotIn("cycle_check", event)
                self.tick_failure(self.condition())
                self.assertNotEqual(self.checks()[0]["id"], previous)

    def test_parallel_unstructured_cycle_error_is_not_a_check(self):
        self.start(parallel=True)
        self.assertEqual(self.tick_failure(TradingError("循环权限信息缺失")), 5)
        self.assertEqual(self.checks(), [])
        self.assertEqual(self.f.store.events()[0]["kind"], "wait")

    def test_new_pending_batch_during_failed_check_prevents_condensing(self):
        self.start(parallel=True)
        pending = {"id": "pending-check", "account_id": "test", "kind": "cycle", "status": "submitted"}
        def submitted_then_failed(*_):
            self.f.store.save_intent(pending)
            raise self.condition()
        with patch.object(self.engine, "tick_cycle_account", side_effect=submitted_then_failed):
            self.assertEqual(self.engine.tick_account("test"), 10)
        self.assertEqual(self.checks(), [])
        self.assertEqual(self.f.store.events()[0]["kind"], "error")
        self.assertEqual(self.f.store.intent("test"), pending)

    def test_mode_failure_keeps_attention_and_account_pause(self):
        self.start()
        self.tick_failure(AccountModeError("账户模式异常"))
        self.assertEqual(self.checks(), [])
        self.assertFalse(self.f.store.account("test")["enabled"])
        self.assertEqual(self.engine.views["test"]["status"], "attention")
        self.assertEqual(self.f.store.events()[0]["kind"], "error")

    def test_phase_change_and_check_without_diagnostic_use_actual_progress(self):
        self.start()
        self.tick_failure(self.condition())
        self.tick_failure(self.condition(phase="close"))
        self.assertEqual(len(self.checks()), 2)
        self.f.store.put("cycle:test", {"opened_at": 123})
        self.tick_failure(CycleConditionError("循环平仓条件未满足"))
        self.assertEqual(len(self.checks()), 2)
        self.assertEqual(self.checks()[0]["cycle_check"]["phase"], "close")
        self.assertEqual(self.checks()[0]["cycle_check"]["count"], 2)
