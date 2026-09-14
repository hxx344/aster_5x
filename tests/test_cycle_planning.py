from copy import deepcopy
from dataclasses import replace
from decimal import localcontext
from fractions import Fraction
import time
import unittest
from unittest.mock import patch

from trading.cycle import (CyclePositionError, DEFAULT_CYCLE, cycle_symbols,
                           plan_cycle, validate_cycle, validate_cycle_positions)
from trading.depth import DepthSnapshot
from trading.models import AccountSnapshot, Book, Position, Rules, TradingError, dec


SYMBOL = "XAUUSD1"
BRACKETS = [{"notionalFloor": "0", "notionalCap": "1000000", "initialLeverage": 125,
             "maintMarginRatio": "0.004", "cum": "0"}]


def depth(now, bids=None, asks=None, **kwargs):
    return DepthSnapshot(tuple((Fraction(dec(p)), Fraction(dec(q)))
                               for p, q in (bids or [("100", "10000")])),
                         tuple((Fraction(dec(p)), Fraction(dec(q)))
                               for p, q in (asks or [("100", "10000")])), now, **kwargs)


class CyclePlanningTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.account = {"policy": {"symbols": ["CLUSD1"], "min_open_leverage": 5,
                                   "margin_limit": "0.5", "threshold": "100000",
                                   "order_notional": "500", "spread_limit": "0.0005"},
                        "cycle": {**DEFAULT_CYCLE, "enabled": True}}
        self.snapshot = AccountSnapshot(
            dec(100000), dec(0), dec(100000), dec(100000), dec(0),
            [Position(SYMBOL, side, dec(0), dec(100), dec(100), 2) for side in ("LONG", "SHORT")],
            [], True, False, True, self.now, {SYMBOL: dec("0.0004")}, {SYMBOL: deepcopy(BRACKETS)})
        self.book = Book(dec(100), dec(100), dec(10000), dec(10000), dec(100), self.now)
        self.depth = depth(self.now)
        self.rule = Rules(SYMBOL, dec("0.001"), dec("0.01"), dec("0.001"), dec(10000), dec(5))
        self.progress = {"run_id": "run-test", "phase": "waiting_open", "quantities": {"LONG": "0", "SHORT": "0"},
                         "opened_at": None, "completed_cycles": 0, "config": deepcopy(self.account["cycle"])}

    def plan(self, **changes):
        args = dict(account=self.account, snapshot=self.snapshot, book=self.book, depth=self.depth,
                    rule=self.rule, progress=self.progress, now=self.now)
        args.update(changes)
        return plan_cycle(**args)

    def hold(self, qty="100", elapsed=60):
        self.progress.update(phase="holding", quantities={"LONG": qty, "SHORT": qty},
                             opened_at=self.now - elapsed, config=deepcopy(self.account["cycle"]))
        for p in self.snapshot.positions:
            p.qty = dec(qty)

    def test_account_current_leverage_cap_replaces_tiers_for_a_flat_cycle(self):
        self.snapshot.brackets = {}
        self.snapshot.current_leverage_caps = {SYMBOL: (2, dec("1000"))}
        result = self.plan()
        self.assertEqual(result.qty, dec("5"))
        self.assertEqual(result.long_notional + result.short_notional, dec("1000"))

    def test_account_current_cap_is_not_transferred_to_another_leverage(self):
        self.snapshot.current_leverage_caps = {SYMBOL: (5, dec("1000000"))}
        with self.assertRaisesRegex(TradingError, "当前杠杆不匹配"):
            self.plan()

    def test_current_account_cap_takes_precedence_over_a_larger_old_tier(self):
        self.snapshot.current_leverage_caps = {SYMBOL: (2, dec("1000"))}
        self.assertEqual(self.plan().qty, dec("5"))
        self.snapshot.current_leverage_caps[SYMBOL] = (2, dec("2000"))
        self.assertEqual(self.plan().qty, dec("10"))

    def test_zero_current_cap_blocks_new_exposure_but_does_not_block_closing(self):
        self.snapshot.current_leverage_caps = {SYMBOL: (2, dec("0"))}
        with self.assertRaises(TradingError):
            self.plan()
        self.hold(qty="5")
        self.snapshot.brackets = {}
        result = self.plan()
        self.assertEqual((result.phase, result.qty), ("close", dec("5")))

    def test_malformed_current_cap_cannot_authorize_a_plan(self):
        for value in ((), (2,), (True, "1000"), (2, "NaN"), (2, "-1"), "1000"):
            with self.subTest(value=value), self.assertRaises(TradingError):
                self.snapshot.current_leverage_caps = {SYMBOL: value}
                self.plan()

    def test_config_defaults_and_exact_normalization(self):
        self.assertEqual(validate_cycle(), DEFAULT_CYCLE)
        config = validate_cycle({"leverage": 1, "hold_seconds": 604800, "spread_limit_bp": "0",
                                 "spread_notional": "1e4", "min_notional": "1000000", "max_notional": "1e6"})
        self.assertEqual(config["spread_notional"], "10000")
        self.assertEqual(config["max_notional"], "1000000")
        config["enabled"] = True
        self.assertFalse(DEFAULT_CYCLE["enabled"])

    def test_invalid_config_types_and_boundaries(self):
        invalid = [[], {"unknown": True}, {"enabled": 1}, {"symbol": "XAU"}, {"symbol": []},
                   {"leverage": True}, {"leverage": "2"}, {"leverage": 2.0}, {"leverage": 0},
                   {"leverage": 126}, {"hold_seconds": True}, {"hold_seconds": 0}, {"hold_seconds": 1.5},
                   {"hold_seconds": 604801}, {"notional_scope": "both"}, {"spread_notional": 10000},
                   {"spread_notional": "0"}, {"spread_notional": "1000000.00000000000000000001"},
                   {"spread_limit_bp": "100.00000000000000000001"}, {"spread_limit_bp": "-0.00001"},
                   {"spread_limit_bp": "NaN"}, {"spread_limit_bp": 0.1}, {"min_notional": "-1"},
                   {"min_notional": "10001"}, {"max_notional": "0"}, {"max_notional": "1000001"}]
        for config in invalid:
            with self.subTest(config=config), self.assertRaises(TradingError):
                validate_cycle(config)

    def test_symbols_only_select_cycle_when_enabled(self):
        self.assertEqual(cycle_symbols(self.account), [SYMBOL])
        self.account["cycle"]["enabled"] = False
        self.assertEqual(cycle_symbols(self.account), ["CLUSD1"])
        self.account["migration"] = {"enabled": True}
        self.assertEqual(set(cycle_symbols(self.account)), {"CLUSD1", "XAUUSD1", "SPCXUSD1"})

    def test_two_times_opens_largest_each_side_and_does_not_mutate_inputs(self):
        before = deepcopy((self.account, self.snapshot, self.progress))
        plan = self.plan()
        self.assertEqual((plan.phase, plan.symbol, plan.qty, plan.leverage), ("open", SYMBOL, dec(100), 2))
        self.assertEqual((plan.long_notional, plan.short_notional), (dec(10000), dec(10000)))
        self.assertEqual(plan.spread_bp, dec(0))
        self.assertEqual((self.account, self.snapshot, self.progress), before)

    def test_gross_scope_limits_sum_and_applies_minimum_to_sum(self):
        self.account["cycle"].update(notional_scope="gross", min_notional="9999.9")
        plan = self.plan()
        self.assertEqual(plan.qty, dec(50))
        self.assertEqual(plan.long_notional + plan.short_notional, dec(10000))

    def test_no_old_five_times_or_five_hundred_floor(self):
        self.account["cycle"].update(max_notional="50", min_notional="5")
        plan = self.plan()
        self.assertEqual(plan.qty, dec("0.5"))
        self.assertEqual(plan.leverage, 2)
        self.account["cycle"].update(max_notional="4.9", min_notional="0")
        with self.assertRaisesRegex(TradingError, "最小委托"):
            self.plan()

    def test_exact_bp_threshold_accepts_boundary_and_rejects_subdecimal_excess(self):
        # Bid/ask midpoint 200000 makes their 2-unit spread exactly 0.1 bp.
        self.depth = depth(self.now, bids=[("199999", "10")], asks=[("200001", "10")])
        self.book = replace(self.book, bid=dec(199999), ask=dec(200001), mark=dec(200000))
        plan = self.plan()
        self.assertEqual(plan.spread_bp, dec("0.1"))
        self.account["cycle"]["spread_limit_bp"] = "0.09999999999999999999999999999999999999"
        with localcontext() as context:
            context.prec = 6
            with self.assertRaisesRegex(TradingError, "采样深度价差"):
                self.plan()

    def test_reference_sweep_requires_complete_depth_on_both_sides(self):
        self.depth = depth(self.now, bids=[("100", "99.999")])
        with self.assertRaisesRegex(TradingError, "采样金额.*深度不足"):
            self.plan()

    def test_reference_spread_uses_full_notional_not_best_quote(self):
        self.depth = depth(self.now, bids=[("100", "1"), ("99", "200")],
                           asks=[("100", "1"), ("101", "200")])
        with self.assertRaisesRegex(TradingError, "采样深度价差"):
            self.plan()

    def test_actual_quantity_spread_also_limits_larger_order(self):
        self.account["cycle"].update(spread_notional="100", max_notional="20000")
        self.depth = depth(self.now, bids=[("100", "1"), ("99", "1000")],
                           asks=[("100", "1"), ("101", "1000")])
        plan = self.plan()
        self.assertEqual(plan.qty, dec(1))
        self.assertEqual(plan.spread_bp, dec(0))

    def test_exact_swept_amount_limits_each_side(self):
        self.account["cycle"]["spread_limit_bp"] = "100"
        self.depth = depth(self.now, bids=[("100", "1000")],
                           asks=[("100", "10"), ("100.1", "1000")])
        plan = self.plan()
        qty = Fraction(plan.qty)
        self.assertLessEqual(Fraction(plan.long_notional), 10000)
        self.assertGreater(Fraction(plan.long_notional) + Fraction("0.001") * Fraction("100.1"), 10000)
        self.assertEqual(Fraction(plan.long_notional), 1000 + (qty - 10) * Fraction("100.1"))

    def test_quantization_and_market_quantity_max_are_respected(self):
        self.rule = replace(self.rule, step=dec("0.03"), max_qty=dec("7.031"))
        self.assertEqual(self.plan().qty, dec("7.02"))

    def test_minimum_configured_amount_waits_instead_of_partial_below_minimum(self):
        self.account["cycle"]["min_notional"] = "9999.99"
        self.rule = replace(self.rule, step=dec("0.03"))
        with self.assertRaisesRegex(TradingError, "最小金额"):
            self.plan()

    def test_open_ignores_external_orders_but_requires_selected_flat_position(self):
        expected = self.plan().qty
        for orders in (None, [{"symbol": "CLUSD1"}]):
            with self.subTest(orders=orders):
                self.assertEqual(self.plan(snapshot=replace(self.snapshot, open_orders=orders)).qty, expected)
        self.snapshot.positions[0].qty = dec("0.001")
        with self.assertRaises(CyclePositionError):
            self.plan()

    def test_open_requires_exact_leverage_and_usd1_rules(self):
        for p in self.snapshot.positions:
            p.leverage = 5
        with self.assertRaisesRegex(TradingError, "2x"):
            self.plan()
        for rule in (replace(self.rule, margin_asset="USDT"), replace(self.rule, symbol="CLUSD1")):
            with self.subTest(rule=rule), self.assertRaisesRegex(TradingError, "USD1"):
                self.plan(rule=rule)

    def test_stale_invalid_and_unsorted_depth_never_plans(self):
        cases = [replace(self.depth, timestamp=self.now - 3.001),
                 replace(self.depth, timestamp=self.now + 1.01),
                 replace(self.depth, validity=lambda: False),
                 depth(self.now, bids=[("100", "1000"), ("101", "1000")]),
                 depth(self.now, bids=[("101", "1000")], asks=[("100", "1000")])]
        for snapshot in cases:
            with self.subTest(snapshot=snapshot), self.assertRaises(TradingError):
                self.plan(depth=snapshot)

    def test_stream_invalidation_during_search_cancels_plan(self):
        valid = iter((True, False))
        self.depth = replace(self.depth, validity=lambda: next(valid))
        with self.assertRaisesRegex(TradingError, "失效"):
            self.plan()

    def test_computation_cannot_outlive_depth_freshness(self):
        with patch("trading.cycle.time.monotonic", side_effect=(100, 104)):
            with self.assertRaisesRegex(TradingError, "深度已过期"):
                self.plan()

    def test_all_account_positions_share_the_cycle_five_point_margin_bonus(self):
        self.account["cycle"]["leverage"] = 10
        for p in self.snapshot.positions:
            p.leverage = 10
        self.snapshot.equity = dec(10000)
        self.snapshot.positions.append(Position("CLUSD1", "LONG", dec(48), dec(100), dec(100), 1))
        plan = self.plan()
        self.assertGreater(plan.qty, dec(10))
        self.assertLessEqual(plan.projected_ratio, dec("0.55"))
        # At +1 quantity step the shared account cap, with fees, is exceeded.
        q = Fraction(plan.qty) + Fraction(self.rule.step)
        self.assertGreater(4800 + 20 * q, Fraction("0.55") * (10000 - Fraction("0.08") * q))

    def test_available_cash_includes_margin_and_both_taker_fees(self):
        self.snapshot.available = dec(100)
        plan = self.plan()
        q = Fraction(plan.qty)
        self.assertLessEqual(q * Fraction("100.08"), 100)
        self.assertGreater((q + Fraction(self.rule.step)) * Fraction("100.08"), 100)

    def test_gross_bracket_cap_does_not_net_opposite_positions(self):
        self.snapshot.brackets[SYMBOL][0]["notionalCap"] = "10000"
        plan = self.plan()
        self.assertEqual(plan.qty, dec(50))
        self.snapshot.brackets[SYMBOL][0]["initialLeverage"] = 1
        with self.assertRaisesRegex(TradingError, "杠杆.*档位"):
            self.plan()

    def test_hold_duration_starts_from_completed_open_and_boundary_closes(self):
        self.hold(elapsed=59.999)
        with self.assertRaisesRegex(TradingError, "计时.*1 秒"):
            self.plan()
        self.progress["opened_at"] = self.now - 60
        plan = self.plan()
        self.assertEqual((plan.phase, plan.qty), ("close", dec(100)))

    def test_close_uses_frozen_parameters_and_tracked_quantity(self):
        self.hold(qty="10")
        self.account["cycle"].update(symbol="CLUSD1", leverage=20, hold_seconds=604800,
                                      max_notional="1", spread_limit_bp="0")
        plan = self.plan()
        self.assertEqual((plan.phase, plan.symbol, plan.qty, plan.leverage), ("close", SYMBOL, dec(10), 2))
        self.assertEqual(plan.long_notional, dec(1000))

    def test_close_ignores_opening_equity_cash_notional_minimum_and_margin_limits(self):
        self.hold(qty="0.001")
        self.snapshot.equity = dec(-1)
        self.snapshot.available = dec(-1)
        self.snapshot.brackets = {}
        self.snapshot.fees = {}
        self.assertEqual(self.plan().qty, dec("0.001"))

    def test_close_still_requires_reference_and_actual_order_spreads(self):
        self.account["cycle"]["spread_notional"] = "100"
        self.hold()
        self.depth = depth(self.now, bids=[("100", "1"), ("99", "1000")],
                           asks=[("100", "1"), ("101", "1000")])
        with self.assertRaisesRegex(TradingError, "实际平仓.*价差"):
            self.plan()

    def test_close_never_reduces_untracked_or_asymmetric_holdings(self):
        self.hold()
        self.snapshot.positions[0].qty = dec("100.001")
        with self.assertRaises(CyclePositionError):
            validate_cycle_positions(self.account, self.snapshot, self.progress)
        self.snapshot.positions[0].qty = dec(100)
        for p in self.snapshot.positions:
            p.leverage = 5
        with self.assertRaises(CyclePositionError):
            self.plan()

    def test_invalid_progress_and_future_timing_pause(self):
        self.hold()
        for change in ({"phase": "opening"}, {"quantities": {"LONG": "1", "SHORT": "2"}},
                       {"opened_at": True}, {"opened_at": float("nan")},
                       {"opened_at": self.now + 1}, {"opened_at": None}, {"config": None}):
            with self.subTest(change=change), self.assertRaises(CyclePositionError):
                self.plan(progress={**self.progress, **change})

    def test_close_requires_full_depth_without_external_orders_read(self):
        self.hold(qty="200")
        with self.assertRaisesRegex(TradingError, "全部平仓.*深度不足"):
            self.plan(depth=depth(self.now, bids=[("100", "100")]))
        self.assertEqual(self.plan(snapshot=replace(self.snapshot, open_orders=None)).qty, dec(200))


if __name__ == "__main__":
    unittest.main()
