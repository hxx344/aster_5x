"""Migration uses one account-wide five-point allowance at every supported tier."""
from dataclasses import replace
from fractions import Fraction
import unittest

from trading.models import TradingError, dec, migration_margin_limit, opening_margin_limit, plan_pair
from . import test_migration_planning as planning


class MigrationMarginTests(unittest.TestCase):
    def setUp(self):
        # Compose the existing in-memory fixture without inheriting its tests.
        self.f = planning.MigrationPlanningTests()
        self.f.setUp()
        self.f.flat_prices()
        self.f.source(2350)
        self.f.account["policy"].update(margin_limit=".93", threshold="0", order_notional="10000")
        self.f.account["migration"]["batch_notional"] = "10000"
        self.f.snapshot = replace(self.f.snapshot, equity=dec(100000), wallet=dec(100000), available=dec(6000),
                                  fees=dict.fromkeys(self.f.snapshot.fees, dec(0)))
        self.f.capacities = {"5": dec(1000000)}

    def test_five_x_migration_above_base_needs_no_upgrade_and_accepts_exact_limit(self):
        self.assertEqual(self.f.snapshot.ratio, dec(".94"))
        plan = self.f.plan()
        self.assertEqual(plan.source_leverage, 5)
        self.assertEqual(plan.target_leverage, 5)
        self.assertEqual(plan.target_qty, dec(1000))
        self.assertEqual(plan.source_quantities, {"LONG": dec(100), "SHORT": dec(100)})
        self.assertEqual(plan.cost_budget, 0)
        self.assertEqual(plan.projected_ratio, dec(".98"))
        occupied = self.f.snapshot.occupied_margin_exact + 2 * Fraction(plan.target_qty) * 10 / 5
        self.assertEqual(occupied, Fraction(98, 100) * Fraction(self.f.snapshot.equity))

    def test_fees_and_spreads_shrink_batch_without_spending_beyond_allowance(self):
        for fees, spread in ((True, False), (False, True), (True, True)):
            with self.subTest(fees=fees, spread=spread):
                self.f.flat_prices()
                self.f.snapshot.fees = dict.fromkeys(self.f.snapshot.fees, dec(".0004") if fees else dec(0))
                if spread:
                    self.f.source_book = replace(self.f.source_book, bid=dec("99.99"), ask=dec("100.01"))
                    self.f.target_book = replace(self.f.target_book, bid=dec("9.999"), ask=dec("10.001"))
                    self.f.source_depth = planning.depth(self.f.source_book)
                    self.f.target_depth = planning.depth(self.f.target_book)
                plan = self.f.plan()
                self.assertGreater(plan.target_qty, 0)
                self.assertLess(plan.target_qty, 1000)
                # Independently charge the four executable legs and mark losses.
                source_amounts = {s: Fraction(v) for s, v in plan.source_notionals.items()}
                target_amounts = {s: Fraction(v) for s, v in plan.target_notionals.items()}
                source_quantities = {s: Fraction(v) for s, v in plan.source_quantities.items()}
                quantity = Fraction(plan.target_qty)
                cost = (sum(source_amounts.values()) * Fraction(self.f.snapshot.fees[planning.SOURCE])
                        + sum(target_amounts.values()) * Fraction(self.f.snapshot.fees[planning.TARGET])
                        + source_quantities["LONG"] * 100 - source_amounts["LONG"]
                        + source_amounts["SHORT"] - source_quantities["SHORT"] * 100
                        + target_amounts["LONG"] - quantity * 10
                        + quantity * 10 - target_amounts["SHORT"])
                source_margin = 2 * Fraction(2350) * Fraction(self.f.source_book.ask) / 5
                target_margin = 2 * quantity * Fraction(self.f.target_book.ask) / 5
                self.assertGreater(cost, 0)
                self.assertEqual(Fraction(plan.cost_budget), cost)
                self.assertLessEqual(source_margin + target_margin,
                                     Fraction(98, 100) * (Fraction(self.f.snapshot.equity) - cost))
                self.assertLessEqual(plan.projected_ratio, dec(".98"))

    def test_five_ten_and_twenty_share_one_allowance_without_stacking(self):
        self.f.account["migration"]["batch_notional"] = "100000"
        for leverage in (5, 10, 20):
            with self.subTest(leverage=leverage):
                self.f.target(leverage=leverage)
                self.f.capacities = {"5": dec(1000000), str(leverage): dec(1000000)}
                plan = self.f.plan()
                self.assertEqual(plan.target_leverage, leverage)
                self.assertEqual(plan.target_qty, dec(200 * leverage))
                self.assertEqual(plan.projected_ratio, dec(".98"))
                self.assertEqual(migration_margin_limit(self.f.account["policy"]), dec(".98"))
        self.assertEqual(self.f.account["policy"]["margin_limit"], ".93")

    def test_migration_allowance_caps_at_one_hundred_percent(self):
        for base in (".95", ".98", "1"):
            with self.subTest(base=base):
                self.assertEqual(migration_margin_limit({"margin_limit": base}), dec(1))
        self.f.account["policy"]["margin_limit"] = ".98"
        self.f.source(2475)
        # Leave cash headroom so only the 100% risk ceiling sizes this batch.
        self.f.snapshot.available = dec(10000)
        self.assertEqual(self.f.snapshot.ratio, dec(".99"))
        plan = self.f.plan()
        self.assertEqual(plan.target_qty, dec(250))
        self.assertEqual(plan.projected_ratio, dec(1))

    def test_ordinary_five_x_opening_still_uses_base_limit(self):
        policy = self.f.account["policy"]
        self.assertEqual(opening_margin_limit(policy, 5), dec(".93"))
        for leverage in (10, 20):
            self.assertEqual(opening_margin_limit(policy, leverage), dec(".98"))
        ordinary = plan_pair(self.f.snapshot, self.f.target_book, self.f.target_rule,
                             {5: dec(1000000)}, policy)
        self.assertEqual(ordinary.qty, 0)
        self.assertIn("保证金占用率", ordinary.reason)
        self.assertGreater(self.f.plan().target_qty, 0)

    def test_allowance_never_bypasses_zero_capacity_or_insufficient_cash(self):
        with self.assertRaisesRegex(TradingError, "5x"):
            self.f.plan(capacities={"5": dec(0), "10": dec(1000000), "20": dec(1000000)})
        with self.assertRaisesRegex(TradingError, "余额"):
            self.f.plan(snapshot=replace(self.f.snapshot, available=dec(199)))


if __name__ == "__main__":
    unittest.main()
