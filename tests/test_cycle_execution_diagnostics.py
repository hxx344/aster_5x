"""Execution failure details report exact evidence without changing actions."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import localcontext
from fractions import Fraction
import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from tests import test_cycle_execution as execution_cases, test_cycle_rolling_execution as rolling_cases
from tests import test_cycle_extra_margin_execution as margin_cases
from trading.cycle import DailyVolumeLimitError, RollingVolumeLimitError
from trading.cycle_diagnostics import diagnostic_number
from trading.models import dec, wire


DAY = datetime(2026, 9, 14, tzinfo=timezone.utc).timestamp()
SYMBOL = "XAUUSD1"


class CycleExecutionDiagnosticsTests(TestCase):
    progress_now = execution_cases.CycleExecutionTests.progress_now
    plan = execution_cases.CycleExecutionTests.plan
    open = execution_cases.CycleExecutionTests.open
    close_cycle = execution_cases.CycleExecutionTests.close_cycle
    quantities = execution_cases.CycleExecutionTests.quantities
    historical_fill = rolling_cases.RollingCycleExecutionTests.historical_fill
    set_wallet = margin_cases.CycleExtraMarginExecutionTests.set_wallet
    after_open_fill = margin_cases.CycleExtraMarginExecutionTests.after_open_fill

    def setUp(self):
        execution_cases.CycleExecutionTests.setUp(self)
        self.serial = 0

    def configure(self, **changes):
        self.f.account["cycle"].update(changes)
        self.f.store.save_account(self.f.account)
        progress = self.progress_now()
        progress["config"] = deepcopy(self.f.account["cycle"])
        self.f.store.put("cycle:test", progress)

    def require_room(self, now, plan=None):
        with patch("trading.cycle_execution.time", SimpleNamespace(time=lambda: now)):
            self.executor._require_daily_room(self.f.account, plan or self.plan("open"), self.f.account["cycle"])

    def test_daily_quota_failure_has_both_windows_used_remaining_cap_and_roundtrip(self):
        self.configure(daily_volume_limit="40000")
        self.historical_fill("10000", DAY + 1)
        with self.assertRaises(DailyVolumeLimitError) as caught:
            self.require_room(DAY + 100)
        error, detail = caught.exception, caught.exception.diagnostic
        self.assertIs(type(error), DailyVolumeLimitError)
        self.assertEqual((detail["code"], detail["symbol"], detail["phase"], detail["checked_at"]),
                         ("execution_daily_volume", SYMBOL, "open", DAY + 100))
        checks = {row["code"]: row for row in detail["checks"]}
        for key in ("daily_projected_volume", "rolling_projected_volume"):
            self.assertEqual((checks[key]["actual"], checks[key]["required"], checks[key]["unit"], checks[key]["passed"]),
                             ("45296.12", "≤ 40000", "USD1", False))
        context = {row["label"]: row["value"] for row in detail["context"]}
        self.assertEqual(context, {"本轮预计开平交易量": "35296.12", "UTC 日已用成交量": "10000",
                                   "UTC 日剩余额度": "30000", "近 24 小时已用成交量": "10000",
                                   "近 24 小时剩余额度": "30000", "成交量上限": "40000"})
        self.assertIn("预计开仓及平仓", str(error))
        self.assertIn("45296.12", str(error))
        self.assertIn("≤ 40000", str(error))
        self.assertEqual(json.loads(json.dumps(detail, ensure_ascii=False)), detail)
        self.assertIsNone(self.f.store.intent("test"))

    def test_rolling_quota_explains_the_tighter_window_without_failing_daily_check(self):
        self.configure(daily_volume_limit="40000")
        self.historical_fill("10000", DAY - 1)
        self.historical_fill("2000", DAY + 1)
        with self.assertRaises(RollingVolumeLimitError) as caught:
            self.require_room(DAY + 100)
        detail = caught.exception.diagnostic
        self.assertEqual(detail["code"], "execution_rolling_volume")
        daily, rolling = detail["checks"]
        self.assertEqual((daily["actual"], daily["required"], daily["passed"]), ("37296.12", "≤ 40000", True))
        self.assertEqual((rolling["actual"], rolling["required"], rolling["passed"]), ("47296.12", "≤ 40000", False))
        context = {row["label"]: row["value"] for row in detail["context"]}
        self.assertEqual((context["UTC 日已用成交量"], context["UTC 日剩余额度"]), ("2000", "38000"))
        self.assertEqual((context["近 24 小时已用成交量"], context["近 24 小时剩余额度"]), ("12000", "28000"))

    def test_quota_exact_boundary_and_precision_do_not_depend_on_display_formatting(self):
        plan = self.plan("open")
        roundtrip = 2 * (Fraction(plan.long_notional) + Fraction(plan.short_notional))
        self.configure(daily_volume_limit=wire(roundtrip))
        with patch("trading.cycle_execution.diagnostic_error", side_effect=AssertionError("no failure to explain")):
            self.require_room(DAY + 100, plan)
        limit = wire(roundtrip - Fraction("0.00000000000000000001"))
        self.configure(daily_volume_limit=limit)
        with localcontext() as context:
            context.prec = 5
            with self.assertRaises(DailyVolumeLimitError) as caught:
                self.require_room(DAY + 100, plan)
        daily = caught.exception.diagnostic["checks"][0]
        self.assertEqual((daily["actual"], daily["required"], daily["passed"]), ("35296.12", "≤ " + limit, False))

    def test_last_callback_fill_still_blocks_submission_and_uses_current_diagnostics(self):
        self.configure(daily_volume_limit="40000")
        def late_fill(snapshot):
            self.historical_fill("10000", DAY - 1)
        with patch("trading.cycle_execution.time", SimpleNamespace(time=lambda: DAY + 100)), \
             patch.object(self.f.broker, "submit", side_effect=AssertionError("over-limit order must not be sent")), \
             self.assertRaises(RollingVolumeLimitError) as caught:
            self.executor.start(self.f.account, self.f.broker.cycle_snapshot([SYMBOL]), self.plan("open"),
                                self.progress_now(), before_submit=late_fill)
        detail = caught.exception.diagnostic
        self.assertEqual(detail["checked_at"], DAY + 100)
        self.assertEqual(detail["checks"][1]["actual"], "45296.12")
        self.assertIsNone(self.f.store.intent("test"))
        self.assertEqual(self.quantities(), (0, 0))

    def test_actual_notional_rollback_explains_each_leg_and_configured_scope_range(self):
        for scope, minimum, maximum, execution_price, expected_long, expected_short, expected_total in (
                ("per_side", "0", "10000", "6000", "12000.02", "12000", "24000.02"),
                ("gross", "0", "18000", "4510", "9020.02", "9020", "18040.02"),
                ("per_side", "8700", "10000", "4300", "8600.02", "8600", "17200.02")):
            with self.subTest(scope=scope, minimum=minimum):
                self.configure(notional_scope=scope, min_notional=minimum, max_notional=maximum)
                original_book, original_submit = self.f.market.book, self.f.broker.submit
                execution_started = []
                def moved_book(symbol):
                    book = original_book(symbol)
                    if execution_started and symbol == SYMBOL:
                        book.bid = book.mark = dec(execution_price)
                        book.ask = dec(execution_price) + dec("0.01")
                    return book
                def slipped(orders):
                    execution_started.append(True)
                    return original_submit(orders)
                with patch.object(self.f.market, "book", side_effect=moved_book), \
                     patch.object(self.f.broker, "submit", side_effect=slipped) as submit:
                    self.open()
                reason = self.progress_now()["reason"]
                self.assertIn("实际成交金额超出本轮范围", reason)
                self.assertIn("多头 " + expected_long + " USD1", reason)
                self.assertIn("空头 " + expected_short + " USD1", reason)
                self.assertIn("多空合计 " + expected_total + " USD1", reason)
                label = "每边" if scope == "per_side" else "多空合计"
                self.assertIn("要求" + label + "金额 " + minimum + "–" + maximum + " USD1", reason)
                self.assertEqual(submit.call_count, 2)
                self.assertEqual(self.quantities(), (0, 0))
                self.assertIsNone(self.progress_now()["opened_at"])
                self.assertNotIn("diagnostic", self.executor.last_completed_intent)

    def test_actual_margin_rollback_reports_ratio_effective_cap_occupied_and_equity(self):
        for base, wallet, cap in (("0.5", "15000", "55"), ("0.98", "8000", "100")):
            with self.subTest(base=base):
                self.f.account["policy"]["margin_limit"] = base
                self.f.store.save_account(self.f.account)
                observed = []
                def changed_equity():
                    self.set_wallet(wallet)
                    observed.append(self.f.broker.cycle_snapshot([SYMBOL]))
                with self.after_open_fill(changed_equity) as submit:
                    self.open()
                snapshot = observed[0]
                occupied, equity = snapshot.occupied_margin_exact, Fraction(snapshot.equity)
                reason = self.progress_now()["reason"]
                self.assertIn("成交后实际保证金占用超过账户上限", reason)
                self.assertIn("当前 " + diagnostic_number(occupied / equity, 100) + "%", reason)
                self.assertIn("要求 ≤ " + cap + "%", reason)
                self.assertIn("账户总占用 " + diagnostic_number(occupied) + " USD1", reason)
                self.assertIn("总权益 " + diagnostic_number(equity) + " USD1", reason)
                self.assertEqual(submit.call_count, 2)
                self.assertEqual(self.quantities(), (0, 0))
                self.assertIsNone(self.progress_now()["opened_at"])
                self.assertNotIn("diagnostic", self.executor.last_completed_intent)

    def test_diagnostic_building_is_not_required_for_successful_open_or_close(self):
        with patch("trading.cycle_execution.diagnostic_error", side_effect=AssertionError("no failure to explain")), \
             patch("trading.cycle_execution.diagnostic_number", side_effect=AssertionError("no failure to format")):
            self.open()
            self.close_cycle()
        self.assertEqual(self.quantities(), (0, 0))
        self.assertEqual(self.progress_now()["completed_cycles"], 1)

    def test_diagnostic_building_is_not_required_for_partial_open_repair(self):
        original = self.f.broker.submit
        def single_leg(orders):
            return [original(orders[:1])[0], {"code": -2019}] if len(orders) == 2 else original(orders)
        with patch.object(self.f.broker, "submit", side_effect=single_leg) as submit, \
             patch("trading.cycle_execution.diagnostic_error", side_effect=AssertionError("repair must proceed")), \
             patch("trading.cycle_execution.diagnostic_number", side_effect=AssertionError("repair must proceed")):
            self.open()
        self.assertEqual(submit.call_count, 2)
        self.assertEqual(self.quantities(), (0, 0))
