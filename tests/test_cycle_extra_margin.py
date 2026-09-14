"""Cycle-wide five-point allowance, shared occupancy and quota coexistence."""
from fractions import Fraction
from decimal import localcontext
from unittest import TestCase

from tests import test_cycle_planning as planning_cases, test_cycle_engine as engine_cases
from tests.helpers import account
from trading.models import Position, TradingError, cycle_margin_limit, dec, opening_margin_limit


class CycleExtraMarginTests(TestCase):
    setUp = planning_cases.CyclePlanningTests.setUp
    plan = planning_cases.CyclePlanningTests.plan

    def prepare(self, base="0.5", leverage=2):
        self.account["policy"]["margin_limit"] = base
        self.account["cycle"].update(leverage=leverage, max_notional="1000000")
        self.snapshot.equity = self.snapshot.available = dec(10000)
        self.snapshot.fees[planning_cases.SYMBOL] = dec(0)
        for position in self.snapshot.positions:
            position.leverage = leverage

    def test_all_cycle_leverages_can_use_the_whole_account_limit_without_separate_five_percent_cap(self):
        for leverage in (1, 2, 5, 10, 20, 125):
            with self.subTest(leverage=leverage):
                self.prepare(leverage=leverage)
                plan = self.plan()
                self.assertEqual(plan.projected_ratio, dec("0.55"))
                self.assertEqual(Fraction(plan.qty), Fraction(5500 * leverage, 200))
                self.assertGreater(plan.projected_ratio, dec("0.05"))

    def test_other_markets_and_both_directions_share_one_bonus(self):
        self.prepare(base="0.9")
        self.snapshot.positions.extend([
            Position("CLUSD1", "LONG", dec(45), dec(100), dec(100), 1),
            Position("CLUSD1", "SHORT", dec(45), dec(100), dec(100), 1),
        ])
        plan = self.plan()
        self.assertEqual((plan.qty, plan.projected_ratio), (dec(5), dec("0.95")))
        # Occupying the shared allowance leaves no room for another cycle pair.
        self.snapshot.positions[2].qty = dec(50)
        with self.assertRaisesRegex(TradingError, "最小委托"):
            self.plan()

    def test_hundred_percent_ceiling_accepts_equality_without_an_extra_full_five_points(self):
        for base in ("0.95", "0.98", "1"):
            with self.subTest(base=base):
                self.prepare(base=base)
                self.snapshot.positions = self.snapshot.positions[:2] + [
                    Position("CLUSD1", "LONG", dec(98), dec(100), dec(100), 1)]
                plan = self.plan()
                self.assertEqual((plan.qty, plan.projected_ratio), (dec(2), dec(1)))

    def test_fee_reserve_still_reduces_quantity_below_the_effective_limit(self):
        self.prepare()
        self.snapshot.fees[planning_cases.SYMBOL] = dec("0.0004")
        plan = self.plan()
        quantity = Fraction(plan.qty)
        self.assertLess(quantity, 55)
        self.assertLessEqual(100 * quantity, Fraction("0.55") * (10000 - Fraction("0.08") * quantity))
        increased = quantity + Fraction(self.rule.step)
        self.assertGreater(100 * increased, Fraction("0.55") * (10000 - Fraction("0.08") * increased))
        self.snapshot.available = dec(100)
        self.assertLess(self.plan().qty, 1)

    def test_daily_and_rolling_limits_remain_independent_gates(self):
        self.prepare()
        for daily, rolling in (("200", "1000"), ("1000", "200")):
            with self.subTest(daily=daily, rolling=rolling):
                plan = self.plan(daily_remaining=daily, rolling_remaining=rolling)
                self.assertEqual(plan.qty, dec("0.5"))
                self.assertEqual(2 * (plan.long_notional + plan.short_notional), 200)

    def test_limit_math_preserves_precision_and_does_not_relax_ordinary_five_times(self):
        policy = {"margin_limit": "0.930000000000000000000000000001"}
        with localcontext() as context:
            context.prec = 6
            self.assertEqual(cycle_margin_limit(policy), dec("0.980000000000000000000000000001"))
        self.assertEqual(opening_margin_limit(policy, 5), dec(policy["margin_limit"]))
        self.assertEqual(policy["margin_limit"], "0.930000000000000000000000000001")
        for invalid in ("0", "-0.01", "1.000001", "NaN"):
            with self.subTest(invalid=invalid), self.assertRaises(TradingError):
                cycle_margin_limit({"margin_limit": invalid})


class CycleExtraMarginStateTests(TestCase):
    setUp = engine_cases.CycleEngineTests.setUp

    def test_state_exposes_each_accounts_effective_limit_without_changing_saved_base(self):
        self.engine.configure("test", {"margin_limit": "0.9", "cycle": {"enabled": True}})
        other = account("second")
        other["policy"]["margin_limit"] = "0.98"
        self.f.store.save_account(other)
        result = {row["id"]: row for row in self.engine.state()["accounts"]}
        self.assertEqual(result["test"]["risk_limits"]["cycle"], "0.95")
        self.assertEqual(result["second"]["risk_limits"]["cycle"], "1")
        self.assertEqual(self.f.store.account("test")["policy"]["margin_limit"], "0.9")
        self.assertEqual(self.f.store.account("second")["policy"]["margin_limit"], "0.98")
