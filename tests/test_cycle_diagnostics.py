"""Exact failure explanations without changing cycle admission or order sizing."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal, ROUND_DOWN, localcontext
from fractions import Fraction
import json
from unittest import TestCase
from unittest.mock import patch

from tests import test_cycle_planning as planning
from trading.cycle import CycleConditionError, DailyVolumeLimitError
from trading.cycle_diagnostics import diagnostic_error, diagnostic_number
from trading.migration import _Sweep
from trading.models import Position, TradingError, dec


class DiagnosticFormattingTests(TestCase):
    def test_finite_precision_and_approximation_are_independent_of_decimal_context(self):
        exact = "0.10000000000000000000000000000000000001"
        expected = diagnostic_number(Fraction(2, 3))
        with localcontext() as context:
            context.prec = 3
            context.rounding = ROUND_DOWN
            self.assertEqual(diagnostic_number(Decimal(exact)), exact)
            self.assertEqual(diagnostic_number(Fraction("0.0055"), 100), "0.55")
            self.assertEqual(diagnostic_number(Fraction(2, 3)), expected)
            self.assertEqual(diagnostic_number(Fraction(1, 10**100)), "0." + "0" * 99 + "1")
        self.assertTrue(expected.startswith("≈0.666666666666666666666666666666666667"))
        self.assertEqual(diagnostic_number(0), "0")
        self.assertEqual(diagnostic_number(Fraction(-1, 8)), "-0.125")

    def test_legacy_message_only_and_error_types_remain_compatible_and_details_are_copied(self):
        self.assertIsNone(DailyVolumeLimitError("legacy").diagnostic)
        self.assertIsInstance(CycleConditionError("legacy"), TradingError)
        checks = [{"code": "example", "label": "可用余额", "actual": "1", "required": "≥ 2",
                   "unit": "USD1", "passed": False}]
        error = diagnostic_error("test", "原有前缀", symbol=planning.SYMBOL, phase="open", checked_at=123,
                                 checks=checks, error_type=DailyVolumeLimitError)
        self.assertIs(type(error), DailyVolumeLimitError)
        self.assertIsInstance(error, DailyVolumeLimitError)
        self.assertIn("原有前缀：可用余额 1 USD1（要求 ≥ 2 USD1）", str(error))
        checks[0]["actual"] = "changed"
        self.assertEqual(error.diagnostic["checks"][0]["actual"], "1")
        self.assertNotIn("123", str(error))
        json.dumps(error.diagnostic, allow_nan=False)


class CycleDiagnosticsTests(TestCase):
    setUp = planning.CyclePlanningTests.setUp
    plan = planning.CyclePlanningTests.plan
    hold = planning.CyclePlanningTests.hold

    def failed(self, **kwargs):
        with self.assertRaises(CycleConditionError) as caught:
            self.plan(**kwargs)
        diagnostic = caught.exception.diagnostic
        self.assertEqual(diagnostic["checked_at"], self.now)
        self.assertEqual(diagnostic["symbol"], planning.SYMBOL)
        json.dumps(diagnostic, allow_nan=False)
        return caught.exception, {row["code"]: row for row in diagnostic["checks"]}

    def test_reference_spread_reports_exact_boundary_original_threshold_and_sample_prices(self):
        self.depth = planning.depth(self.now, bids=[("199999", "10")], asks=[("200001", "10")])
        self.book = replace(self.book, mark=dec(200000))
        threshold = "0.09999999999999999999999999999999999999"
        self.account["cycle"]["spread_limit_bp"] = threshold
        with localcontext() as context:
            context.prec = 6
            error, checks = self.failed()
        self.assertEqual(error.diagnostic["code"], "reference_spread")
        self.assertEqual(checks["reference_spread"]["actual"], "0.1")
        self.assertEqual(checks["reference_spread"]["required"], "≤ " + threshold)
        self.assertFalse(checks["reference_spread"]["passed"])
        values = {row["label"]: row["value"] for row in error.diagnostic["context"]}
        self.assertEqual(values["采样每边金额"], "10000")
        self.assertEqual((values["买入均价"], values["卖出均价"]), ("200001", "199999"))
        self.assertEqual(Fraction(values["价差超出"]), Fraction("0.1") - Fraction(threshold))
        self.assertIn("买入均价 200001 USD1", str(error))
        self.assertIn("循环采样深度价差超过配置阈值（bp）", str(error))

    def test_nonterminating_reference_vwap_is_marked_approximate(self):
        self.account["cycle"]["spread_notional"] = "200"
        self.depth = planning.depth(self.now, bids=[("100", "1"), ("99", "200")],
                                    asks=[("100", "1"), ("101", "200")])
        with localcontext() as context:
            context.prec = 3
            error, _ = self.failed()
        values = {row["label"]: row["value"] for row in error.diagnostic["context"]}
        self.assertEqual(values["买入均价"], diagnostic_number(Fraction(20200, 201)))
        self.assertEqual(values["卖出均价"], diagnostic_number(Fraction(19800, 199)))
        self.assertTrue(values["买入均价"].startswith("≈"))

    def test_missing_reference_depth_is_not_a_zero_spread_or_a_pass(self):
        self.depth = planning.depth(self.now, bids=[("100", "99.999")])
        error, checks = self.failed()
        self.assertFalse(checks["reference_sell_depth"]["passed"])
        self.assertEqual(checks["reference_sell_depth"]["actual"], "9999.9")
        self.assertTrue(checks["reference_buy_depth"]["passed"])
        self.assertIsNone(checks["reference_spread"]["actual"])
        self.assertIsNone(checks["reference_spread"]["passed"])
        self.assertEqual(error.diagnostic["code"], "reference_depth")

    def test_minimum_order_lists_all_real_binding_limits_at_the_same_minimum_quantity(self):
        self.account["cycle"]["max_notional"] = "4.9"
        self.snapshot.available = dec(2)
        self.snapshot.brackets[planning.SYMBOL][0]["notionalCap"] = "8"
        before = deepcopy((self.account, self.snapshot, self.progress))
        error, checks = self.failed()
        for code in ("configured_max_notional", "available_margin", "leverage_cap"):
            self.assertFalse(checks[code]["passed"], code)
        self.assertEqual(checks["configured_max_notional"]["actual"], "5")
        self.assertEqual(checks["available_margin"]["required"], "≥ 5.004")
        self.assertEqual(checks["leverage_cap"]["actual"], "10")
        self.assertEqual(checks["exchange_min_qty"]["actual"], "0.05")
        self.assertIn("循环风险、余额或深度不足以满足交易所最小委托", str(error))
        self.assertEqual((self.account, self.snapshot, self.progress), before)
        values = {row["label"]: row["value"] for row in error.diagnostic["context"]}
        self.assertEqual(values["风控手续费预留率"], "0.04")
        self.assertEqual(values["可用余额缺口"], "3.004")

    def test_minimum_quantity_is_rounded_up_to_the_exchange_step(self):
        self.rule = replace(self.rule, min_qty=dec("1.001"), step=dec("0.03"))
        self.account["cycle"]["max_notional"] = "100"
        _, checks = self.failed()
        self.assertEqual(checks["quantity_step"]["actual"], "1.02")
        self.assertTrue(checks["quantity_step"]["passed"])
        self.assertEqual(checks["configured_max_notional"]["actual"], "102")
        self.assertFalse(checks["configured_max_notional"]["passed"])

    def test_depth_shortage_never_extrapolates_minimum_order_prices(self):
        self.rule = replace(self.rule, min_qty=dec(200))
        self.depth = planning.depth(self.now, bids=[("100", "100")], asks=[("100", "150")])
        original = _Sweep.amount
        def within_book(sweep, quantity):
            self.assertLessEqual(quantity, sweep.quantity)
            return original(sweep, quantity)
        with patch.object(_Sweep, "amount", within_book):
            _, checks = self.failed()
        self.assertFalse(checks["depth_buy_quantity"]["passed"])
        self.assertFalse(checks["depth_sell_quantity"]["passed"])
        for code in ("configured_max_notional", "order_spread", "leverage_cap", "projected_equity", "projected_margin_ratio"):
            self.assertIsNone(checks[code]["actual"], code)
            self.assertIsNone(checks[code]["passed"], code)
        self.assertIsNone(checks["available_margin"]["required"])
        self.assertIsNone(checks["available_margin"]["passed"])

    def test_config_minimum_beyond_all_observed_depth_marks_financial_checks_unknown(self):
        self.account["cycle"].update(spread_notional="100", min_notional="500")
        self.depth = planning.depth(self.now, bids=[("100", "2")], asks=[("100", "3")])
        error, checks = self.failed()
        self.assertFalse(checks["configured_min_notional"]["passed"])
        self.assertEqual(checks["configured_min_notional"]["actual"], "200")
        self.assertIsNone(checks["available_margin"]["passed"])
        self.assertIsNone(checks["leverage_cap"]["passed"])
        self.assertIn("下界", error.diagnostic["note"])

    def test_config_minimum_not_a_failed_binary_probe_determines_needed_resources(self):
        self.account["cycle"].update(min_notional="99.99", max_notional="100")
        self.rule = replace(self.rule, step=dec("0.03"))
        error, checks = self.failed()
        self.assertIn("循环可执行金额低于配置的最小金额", str(error))
        self.assertEqual(checks["quantity_step"]["actual"], "1.02")
        self.assertTrue(checks["configured_min_notional"]["passed"])
        self.assertFalse(checks["configured_max_notional"]["passed"])
        self.assertEqual(checks["configured_max_notional"]["actual"], "102")
        self.account["cycle"].update(notional_scope="gross")
        _, checks = self.failed()
        self.assertEqual(checks["quantity_step"]["actual"], "0.51")
        self.assertEqual(checks["configured_max_notional"]["actual"], "102")
        self.assertIn("多空合计", checks["configured_min_notional"]["label"])

    def test_actual_minimum_quantity_spread_is_a_distinct_bottleneck(self):
        self.account["cycle"]["spread_notional"] = "10"
        self.rule = replace(self.rule, min_qty=dec(1))
        self.depth = planning.depth(self.now, bids=[("100", "0.1"), ("99", "1000")],
                                    asks=[("100", "0.1"), ("101", "1000")])
        error, checks = self.failed()
        self.assertEqual(error.diagnostic["code"], "cycle_minimum_order")
        self.assertFalse(checks["order_spread"]["passed"])
        self.assertEqual(checks["order_spread"]["actual"], "180")
        self.assertTrue(checks["available_margin"]["passed"])

    def test_available_balance_boundary_is_exact_and_does_not_change_success(self):
        self.snapshot.available = dec("5.004")
        with patch("trading.cycle._minimum_diagnostic", side_effect=AssertionError("successful plan needs no diagnostic")):
            self.assertEqual(self.plan().qty, dec("0.05"))
        actual = "5.00399999999999999999999999999999999999"
        self.snapshot.available = dec(actual)
        with localcontext() as context:
            context.prec = 3
            _, checks = self.failed()
        self.assertFalse(checks["available_margin"]["passed"])
        self.assertEqual(checks["available_margin"]["actual"], actual)
        self.assertEqual(checks["available_margin"]["required"], "≥ 5.004")

    def test_projected_equity_and_all_account_occupancy_keep_original_risk_formula(self):
        self.snapshot.equity = dec("0.003")
        _, checks = self.failed()
        self.assertFalse(checks["projected_equity"]["passed"])
        self.assertEqual(checks["projected_equity"]["actual"], "-0.001")
        self.assertIsNone(checks["projected_margin_ratio"]["actual"])
        self.assertIsNone(checks["projected_margin_ratio"]["passed"])
        self.snapshot.equity = dec(100)
        self.snapshot.fees[planning.SYMBOL] = dec(0)
        self.snapshot.positions.append(Position("CLUSD1", "LONG", dec("0.5"), dec(100), dec(100), 1))
        self.assertEqual(self.plan().qty, dec("0.05"))
        self.account["policy"]["margin_limit"] = "0.49999999999999999999999999999999999999"
        with localcontext() as context:
            context.prec = 3
            _, checks = self.failed()
        self.assertEqual(checks["projected_margin_ratio"]["actual"], "55")
        self.assertFalse(checks["projected_margin_ratio"]["passed"])
        self.assertEqual(checks["projected_margin_ratio"]["required"], "≤ 54.999999999999999999999999999999999999")

    def test_quota_classes_and_nonquota_precedence_are_preserved(self):
        error, checks = self.failed(daily_remaining="19.999")
        self.assertIs(type(error), DailyVolumeLimitError)
        self.assertFalse(checks["daily_volume"]["passed"])
        self.assertEqual(checks["daily_volume"]["required"], "≥ 20")
        self.assertNotIn("rolling_volume", checks)
        self.account["cycle"]["max_notional"] = "4.9"
        error, checks = self.failed(daily_remaining="0")
        self.assertIs(type(error), CycleConditionError)
        self.assertFalse(checks["configured_max_notional"]["passed"])
        self.assertFalse(checks["daily_volume"]["passed"])

    def test_close_quantity_depth_and_spread_diagnostics_do_not_apply_opening_gates(self):
        self.hold(qty="200")
        self.rule = replace(self.rule, max_qty=dec(100))
        error, checks = self.failed()
        self.assertEqual(error.diagnostic["phase"], "close")
        self.assertFalse(checks["close_max_qty"]["passed"])
        self.rule = replace(self.rule, max_qty=dec(10000))
        self.depth = planning.depth(self.now, bids=[("100", "100")])
        error, checks = self.failed()
        self.assertEqual(error.diagnostic["code"], "close_depth")
        self.assertFalse(checks["close_sell_depth"]["passed"])
        self.progress["config"]["spread_notional"] = "100"
        self.depth = planning.depth(self.now, bids=[("100", "1"), ("99", "1000")],
                                    asks=[("100", "1"), ("101", "1000")])
        error, checks = self.failed()
        self.assertEqual(error.diagnostic["code"], "close_spread")
        self.assertFalse(checks["close_spread"]["passed"])
