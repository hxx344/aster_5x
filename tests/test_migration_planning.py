from copy import deepcopy
from dataclasses import replace
from fractions import Fraction
import time
import unittest

from trading.depth import DepthSnapshot
from trading.migration import DEFAULT_MIGRATION, migration_symbols, plan_migration, validate_migration
from trading.models import AccountSnapshot, Book, Position, Rules, TradingError, dec, hedge_balanced


SOURCE, TARGET, OTHER = "XAUUSD1", "SPCXUSD1", "CLUSD1"
BRACKETS = [{"notionalFloor": "0", "notionalCap": "1000000", "initialLeverage": 20,
             "maintMarginRatio": "0.025", "cum": "0"}]


def depth(book, bids=None, asks=None):
    return DepthSnapshot(tuple((Fraction(dec(p)), Fraction(dec(q))) for p, q in
                               (bids or [(book.bid, "10000")])),
                         tuple((Fraction(dec(p)), Fraction(dec(q))) for p, q in
                               (asks or [(book.ask, "10000")])), book.timestamp)


class MigrationPlanningTests(unittest.TestCase):
    def setUp(self):
        now = time.time()
        self.account = {"policy": {"symbols": [SOURCE], "min_open_leverage": 5,
                                   "margin_limit": "0.5", "threshold": "100000",
                                   "order_notional": "1000", "spread_limit": "0.0005"},
                        "migration": {**DEFAULT_MIGRATION, "enabled": True}}
        positions = [Position(symbol, side, dec(20 if symbol == SOURCE else 0), dec(100 if symbol == SOURCE else 10),
                              dec(100 if symbol == SOURCE else 10), 5)
                     for symbol in (SOURCE, TARGET, OTHER) for side in ("LONG", "SHORT")]
        self.snapshot = AccountSnapshot(dec(25000), dec(100), dec(24000), dec(25000), dec(0), positions, [],
                                        True, False, True, now,
                                        {s: dec("0.0004") for s in (SOURCE, TARGET, OTHER)},
                                        {s: deepcopy(BRACKETS) for s in (SOURCE, TARGET, OTHER)})
        self.source_book = Book(dec("99.99"), dec("100.01"), dec(10000), dec(10000), dec(100), now)
        self.target_book = Book(dec("9.999"), dec("10.001"), dec(10000), dec(10000), dec(10), now)
        self.source_depth, self.target_depth = depth(self.source_book), depth(self.target_book)
        self.source_rule = Rules(SOURCE, dec(".001"), dec(".01"), dec(".001"), dec(10000), dec(5))
        self.target_rule = replace(self.source_rule, symbol=TARGET)
        self.capacities = {str(v): dec(100000) for v in (5, 10, 20)}

    def plan(self, **changes):
        args = {"account": self.account, "snapshot": self.snapshot, "source_book": self.source_book,
                "target_book": self.target_book, "source_depth": self.source_depth, "target_depth": self.target_depth,
                "source_rule": self.source_rule, "target_rule": self.target_rule,
                "capacities": self.capacities, "origin_leverage": 5}
        args.update(changes)
        return plan_migration(**args)

    def source(self, long, short=None):
        for p, q in zip(self.snapshot.pair(SOURCE), (long, long if short is None else short)):
            p.qty = dec(q)

    def target(self, quantity=0, leverage=5):
        for p in self.snapshot.pair(TARGET):
            p.qty, p.leverage = dec(quantity), leverage

    def flat_prices(self):
        self.source_book = replace(self.source_book, bid=dec(100), ask=dec(100))
        self.target_book = replace(self.target_book, bid=dec(10), ask=dec(10))
        self.source_depth, self.target_depth = depth(self.source_book), depth(self.target_book)

    def test_config_defaults_are_complete_and_detached(self):
        first = validate_migration()
        self.assertEqual(first, DEFAULT_MIGRATION)
        first["enabled"] = True
        self.assertFalse(DEFAULT_MIGRATION["enabled"])
        self.assertEqual(validate_migration({"spread_limit_bp": "5e0"})["spread_limit_bp"], "5")

    def test_config_rejects_invalid_fields_types_and_exact_boundaries(self):
        invalid = ({"unknown": "x"}, {"enabled": 1}, {"batch_notional": 1000},
                   {"spread_limit_bp": "0"}, {"spread_limit_bp": "100.00000000000000000000000000001"},
                   {"batch_notional": "499.999999999999999999999999999"}, {"batch_notional": "1000001"},
                   {"notional_tolerance": "-0.0001"}, {"notional_tolerance": "0.5000000000000000000000000001"},
                   {"notional_tolerance": "NaN"})
        for config in invalid:
            with self.subTest(config=config), self.assertRaises(TradingError):
                validate_migration(config)
        self.assertEqual(validate_migration({"batch_notional": "500"})["batch_notional"], "500")

    def test_migration_symbols_expands_only_enabled_accounts(self):
        self.assertEqual(migration_symbols(self.account), [SOURCE, TARGET, OTHER])
        self.account["migration"]["enabled"] = False
        self.assertEqual(migration_symbols(self.account), [SOURCE])

    def test_largest_batch_uses_actual_depth_and_both_directional_amounts(self):
        plan = self.plan()
        self.assertEqual(plan.target_qty, dec("99.99"))
        self.assertEqual(plan.source_leverage, 5)
        self.assertEqual(plan.target_leverage, 5)
        self.assertEqual(plan.spread, dec("0.0002"))
        for side in ("LONG", "SHORT"):
            self.assertLessEqual(plan.source_notionals[side], dec(1000))
            self.assertLessEqual(plan.target_notionals[side], dec(1000))
            self.assertLessEqual(abs(plan.target_notionals[side] - plan.source_notionals[side]),
                                 plan.source_notionals[side] * dec("0.05"))
        self.assertGreater((plan.target_qty + self.target_rule.step) * self.target_book.ask, dec(1000))

    def test_zero_5x_blocks_even_with_higher_tier_capacity(self):
        self.capacities["5"] = dec(0)
        with self.assertRaisesRegex(TradingError, "5x"):
            self.plan()

    def test_capacity_gate_ignores_ordinary_threshold_and_charges_both_sides(self):
        self.flat_prices()
        self.capacities = {"5": dec(1200)}
        plan = self.plan()
        self.assertEqual(plan.target_qty, dec(60))
        self.assertEqual(sum(plan.target_notionals.values()), dec(1200))
        self.assertLess(sum(plan.target_notionals.values()), dec(self.account["policy"]["threshold"]))

    def test_non_tail_capacity_cannot_shrink_below_500(self):
        self.capacities = {"5": dec(999)}
        with self.assertRaisesRegex(TradingError, "额度"):
            self.plan()

    def test_original_current_source_and_existing_target_leverages_are_all_floors(self):
        self.assertEqual(self.plan(origin_leverage=10).target_leverage, 10)
        for p in self.snapshot.pair(SOURCE):
            p.leverage = 10
        self.assertEqual(self.plan().target_leverage, 10)
        self.target(leverage=20)
        self.assertEqual(self.plan().target_leverage, 20)
        self.target(leverage=25)
        with self.assertRaisesRegex(TradingError, "档位"):
            self.plan()

    def test_account_minimum_leverage_is_respected(self):
        self.account["policy"]["min_open_leverage"] = 20
        self.assertEqual(self.plan().target_leverage, 20)

    def test_actual_tier_capacity_is_required_when_5x_exists(self):
        self.target(leverage=10)
        self.capacities = {"5": dec(100000), "10": dec(0), "20": dec(0)}
        with self.assertRaisesRegex(TradingError, "额度"):
            self.plan()

    def test_upgrade_capacity_must_cover_existing_target_and_new_gross(self):
        self.flat_prices()
        self.target(quantity=100)
        self.capacities = {"5": dec(100000), "10": dec(3000)}
        plan = self.plan(origin_leverage=10)
        self.assertEqual(plan.target_leverage, 10)
        self.assertEqual(plan.target_qty, dec(50))
        self.capacities["10"] = dec(2999)
        with self.assertRaisesRegex(TradingError, "额度"):
            self.plan(origin_leverage=10)
        self.target(quantity=100, leverage=10)
        self.assertGreater(self.plan(origin_leverage=10).target_qty, dec(50))

    def test_account_bracket_room_includes_existing_gross(self):
        self.flat_prices()
        self.target(quantity=100)
        self.snapshot.brackets[TARGET][0]["notionalCap"] = "3100"
        self.assertEqual(self.plan().target_qty, dec(55))

    def test_exact_five_bp_is_allowed_but_microscopic_excess_is_not(self):
        self.target_book = replace(self.target_book, bid=dec("9.9975"), ask=dec("10.0025"))
        self.target_depth = depth(self.target_book)
        self.assertEqual(self.plan().spread, dec("0.0005"))
        self.target_depth = depth(self.target_book, asks=[("10.0025000000000000000000000000000000000001", "10000")])
        with self.assertRaisesRegex(TradingError, "价差"):
            self.plan()

    def test_deep_spread_can_shrink_a_batch_while_bbo_is_unchanged(self):
        self.target_depth = depth(self.target_book, bids=[("9.999", "60"), ("9.9", "10000")],
                                  asks=[("10.001", "60"), ("10.1", "10000")])
        plan = self.plan()
        self.assertGreater(plan.target_qty, dec(60))
        self.assertLess(plan.target_qty, dec(61))
        self.assertLessEqual(plan.spread, dec("0.0005"))
        self.assertEqual(self.target_book.spread, dec("0.0002"))

    def test_depth_has_stricter_freshness_than_display(self):
        with self.assertRaisesRegex(TradingError, "过期"):
            self.plan(source_depth=replace(self.source_depth, timestamp=time.time() - 4))
        with self.assertRaisesRegex(TradingError, "过期"):
            self.plan(target_depth=replace(self.target_depth, timestamp=time.time() - 4))

    def test_stale_account_and_books_block(self):
        for field, value in (("snapshot", self.snapshot), ("source_book", self.source_book), ("target_book", self.target_book)):
            with self.subTest(field=field), self.assertRaisesRegex(TradingError, "过期"):
                self.plan(**{field: replace(value, timestamp=time.time() - 20)})

    def test_missing_or_crossed_depth_blocks(self):
        for invalid in (DepthSnapshot((), self.target_depth.asks, time.time()),
                        depth(self.target_book, bids=[("11", "100")])):
            with self.subTest(invalid=invalid), self.assertRaises(TradingError):
                self.plan(target_depth=invalid)

    def test_temporary_occupancy_does_not_subtract_planned_source_close(self):
        self.capacities = {"5": dec(100000)}
        snapshot = replace(self.snapshot, equity=dec(1600), available=dec(10000))
        self.assertEqual(snapshot.ratio, dec("0.5"))
        with self.assertRaisesRegex(TradingError, "未平 XAU"):
            self.plan(snapshot=snapshot)

    def test_cash_and_four_leg_costs_are_budgeted(self):
        self.flat_prices()
        self.capacities = {"5": dec(100000)}
        plan = self.plan(snapshot=replace(self.snapshot, available=dec(250)))
        self.assertGreaterEqual(plan.target_qty * dec(10), dec(500))
        self.assertLess(plan.target_qty * dec(10), dec(1000))
        exact_fee = sum(plan.source_notionals.values()) * dec(".0004") + sum(plan.target_notionals.values()) * dec(".0004")
        self.assertEqual(plan.cost_budget, exact_fee)
        self.assertLessEqual(plan.target_qty * dec(20) / 5 + plan.cost_budget, dec(250))

    def test_both_source_and_target_spread_are_charged(self):
        plan = self.plan()
        source_cost = sum(plan.source_notionals.values()) * dec(".0004")
        source_cost += plan.source_quantities["LONG"] * dec(100) - plan.source_notionals["LONG"]
        source_cost += plan.source_notionals["SHORT"] - plan.source_quantities["SHORT"] * dec(100)
        target_cost = sum(plan.target_notionals.values()) * dec(".0004")
        target_cost += plan.target_notionals["LONG"] - plan.target_qty * dec(10)
        target_cost += plan.target_qty * dec(10) - plan.target_notionals["SHORT"]
        self.assertEqual(plan.cost_budget, source_cost + target_cost)

    def test_mark_revaluation_never_credits_one_sided_unconfirmed_profit(self):
        self.source("20", "19.99")
        baseline = self.plan()
        source_book = replace(self.source_book, mark=dec(110))
        changed = self.plan(source_book=source_book)
        self.assertGreater(changed.cost_budget, baseline.cost_budget)
        self.assertGreater(changed.projected_ratio, baseline.projected_ratio)

    def test_small_actual_tail_is_allowed_below_application_500(self):
        self.source("4.41")
        plan = self.plan()
        self.assertTrue(plan.is_tail)
        self.assertEqual(plan.source_quantities, {"LONG": dec("4.41"), "SHORT": dec("4.41")})
        self.assertEqual(plan.target_qty, dec("44.1"))
        self.assertLess(plan.target_qty * self.target_book.mark, dec(500))

    def test_tail_preserves_two_distinct_source_quantities(self):
        self.source("4.41", "4.414")
        plan = self.plan()
        self.assertTrue(plan.is_tail)
        self.assertEqual(plan.source_quantities, {"LONG": dec("4.41"), "SHORT": dec("4.414")})

    def test_untradeable_tail_has_an_explicit_reason(self):
        self.source(".01")
        with self.assertRaisesRegex(TradingError, "尾仓"):
            self.plan()

    def test_last_two_batches_may_split_below_500(self):
        self.flat_prices()
        self.source("6")
        self.capacities = {"5": dec(600)}
        plan = self.plan()
        self.assertEqual(plan.target_qty, dec(30))
        self.assertTrue(plan.is_tail)
        self.assertEqual(plan.source_quantities["LONG"], dec(3))

    def test_small_capacity_is_not_a_tail_exception_with_over_1000_left(self):
        self.flat_prices()
        self.source("10.001")
        self.capacities = {"5": dec(600)}
        with self.assertRaisesRegex(TradingError, "额度"):
            self.plan()

    def test_planning_avoids_exchange_dust_in_source_remainder(self):
        self.flat_prices()
        self.source("10.02")
        plan = self.plan()
        for side in ("LONG", "SHORT"):
            remaining = dec("10.02") - plan.source_quantities[side]
            self.assertTrue(remaining == 0 or remaining * dec(100) >= dec(5))

    def test_cumulative_over_opening_selects_more_source_close_to_compensate(self):
        self.flat_prices()
        self.capacities = {"5": dec(1200)}
        baseline = self.plan()
        progress = {"cumulative_notional_delta": {"LONG": "20", "SHORT": "20"},
                    "migrated_notional": {"LONG": "1000", "SHORT": "1000"}}
        adjusted = self.plan(progress=progress)
        self.assertEqual(adjusted.target_qty, baseline.target_qty)
        self.assertEqual(adjusted.source_quantities["LONG"], dec("6.2"))
        for side in ("LONG", "SHORT"):
            self.assertEqual(dec(20) + adjusted.target_notionals[side] - adjusted.source_notionals[side], 0)

    def test_cumulative_under_opening_selects_less_source_close(self):
        self.flat_prices()
        self.capacities = {"5": dec(1200)}
        plan = self.plan(progress={"cumulative_notional_delta": {"LONG": "-20", "SHORT": "-20"}})
        self.assertEqual(plan.source_quantities["LONG"], dec("5.8"))

    def test_reductions_retain_source_hedge_even_at_original_tolerance_boundary(self):
        self.source("20", "19.98")
        plan = self.plan()
        remaining = [p.qty - plan.source_quantities[p.side] for p in self.snapshot.pair(SOURCE)]
        self.assertTrue(hedge_balanced(*remaining))

    def test_zero_amount_tolerance_requires_exact_quantized_match(self):
        self.flat_prices()
        self.account["migration"]["notional_tolerance"] = "0"
        plan = self.plan()
        self.assertEqual(plan.target_notionals, plan.source_notionals)

    def test_exact_five_percent_amount_error_is_allowed_without_decimal_rounding(self):
        self.flat_prices()
        self.source(5)
        self.source_rule = replace(self.source_rule, step=dec(5), min_qty=dec(5))
        self.target_rule = replace(self.target_rule, step=dec("47.5"), min_qty=dec("47.5"))
        plan = self.plan()
        self.assertEqual(plan.target_qty, dec("47.5"))
        self.assertEqual(plan.source_notionals["LONG"], dec(500))
        self.assertEqual(plan.target_notionals["LONG"], dec(475))
        self.account["migration"]["notional_tolerance"] = "0.0499999999999999999999999999999999999999"
        with self.assertRaises(TradingError):
            self.plan()

    def test_tail_target_does_not_deliberately_use_the_entire_error_allowance(self):
        self.flat_prices()
        self.source("4.41")
        self.account["migration"]["notional_tolerance"] = ".5"
        plan = self.plan()
        self.assertEqual(plan.target_qty, dec("44.1"))
        self.assertEqual(plan.target_notionals, plan.source_notionals)

    def test_tail_compensates_prior_amount_delta_before_rounding(self):
        self.flat_prices()
        self.source("4.41")
        plan = self.plan(progress={"cumulative_notional_delta": {"LONG": "10", "SHORT": "10"}})
        self.assertEqual(plan.target_qty, dec("43.1"))
        self.assertTrue(plan.is_tail)
        self.assertEqual(plan.source_quantities["LONG"], dec("4.41"))

    def test_rule_maximum_quantity_is_an_independent_limit(self):
        self.flat_prices()
        plan = self.plan(target_rule=replace(self.target_rule, max_qty=dec(60)))
        self.assertEqual(plan.target_qty, dec(60))
        plan = self.plan(source_rule=replace(self.source_rule, max_qty=dec(6)))
        self.assertLessEqual(plan.source_quantities["LONG"], dec(6))

    def test_plan_provides_exact_unrounded_spread_for_candidate_ranking(self):
        plan = self.plan()
        self.assertEqual(plan.spread_exact, Fraction(1, 5000))

    def test_wrong_modes_unbalanced_positions_and_missing_fees_fail_closed(self):
        for change in ({"hedge_mode": False}, {"multi_assets": True}, {"can_trade": False}, {"open_orders": [{}]},
                       {"fees": {}}, {"equity": dec(0)}):
            with self.subTest(change=change), self.assertRaises(TradingError):
                self.plan(snapshot=replace(self.snapshot, **change))
        self.source(20, 19)
        with self.assertRaisesRegex(TradingError, "0.1%"):
            self.plan()

    def test_only_xau_to_two_configured_markets_is_supported(self):
        with self.assertRaisesRegex(TradingError, "仅支持"):
            self.plan(target_rule=replace(self.target_rule, symbol=SOURCE))
        self.assertEqual(self.plan(target_rule=replace(self.target_rule, symbol=OTHER)).target_symbol, OTHER)


if __name__ == "__main__":
    unittest.main()
