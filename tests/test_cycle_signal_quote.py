from copy import deepcopy
from dataclasses import replace
from decimal import localcontext
import unittest
from unittest.mock import Mock

from tests import test_cycle_planning as planning
from trading.cycle_signal import cycle_signal_quote
from trading.models import TradingError, dec


class CycleSignalQuoteTests(unittest.TestCase):
    def setUp(self):
        planning.CyclePlanningTests.setUp(self)
        self.account["enabled"] = True

    def quote(self, **changes):
        args = dict(account=self.account, progress=self.progress, book=self.book,
                    depth=self.depth, rule=self.rule, now=self.now)
        args.update(changes)
        return cycle_signal_quote(**args)

    def assert_minimum_quantity(self, quantity):
        # Check both sides of the exchange maximum boundary without exporting
        # private-worker sizing inputs in the public wake signal.
        exact = dec(quantity)
        self.assertIsNotNone(self.quote(rule=replace(self.rule, max_qty=exact)))
        self.assertIsNone(self.quote(rule=replace(self.rule, max_qty=exact - dec("0.000001"))))

    def hold(self, qty="1", elapsed=60):
        planning.CyclePlanningTests.hold(self, qty=qty, elapsed=elapsed)

    def test_open_hint_is_smallest_public_quantity_and_does_not_read_private_risk(self):
        self.account.pop("policy")
        self.account.update(balance="0", available="0", can_trade=False)
        self.account["cycle"]["daily_volume_limit"] = "1"
        self.progress.update(daily_remaining="0", rolling_remaining="0")
        self.assertEqual(self.quote(), {"phase": "open"})

    def test_disabled_account_cycle_and_migration_do_not_read_books(self):
        for change in ("account", "cycle", "migration"):
            with self.subTest(change=change):
                account = deepcopy(self.account)
                if change == "account":
                    account["enabled"] = False
                elif change == "cycle":
                    account["cycle"]["enabled"] = False
                else:
                    account["migration"] = {"enabled": True}
                book = Mock()
                self.assertIsNone(self.quote(account=account, book=book))
                book.require_fresh.assert_not_called()

    def test_wrong_market_or_margin_asset_never_hints(self):
        for rule in (replace(self.rule, symbol="CLUSD1"), replace(self.rule, margin_asset="USDT")):
            with self.subTest(rule=rule):
                self.assertIsNone(self.quote(rule=rule))

    def test_high_frequency_bad_reference_spread_never_hints(self):
        self.depth = planning.depth(self.now, bids=[("100", "1"), ("99", "1000")],
                                    asks=[("100", "1"), ("101", "1000")])
        for _ in range(100):
            self.assertIsNone(self.quote())

    def test_reference_requires_complete_notional_on_both_sides(self):
        for bids, asks in (([("100", "99.999")], None), (None, [("100", "99.999")])):
            with self.subTest(bids=bids, asks=asks):
                self.assertIsNone(self.quote(depth=planning.depth(self.now, bids=bids, asks=asks)))
        self.assertIsNotNone(self.quote(depth=planning.depth(self.now, bids=[("100", "100")],
                                                             asks=[("100", "100")])))

    def test_reference_pass_does_not_hide_minimum_order_spread_failure(self):
        self.account["cycle"]["spread_notional"] = "100"
        self.depth = planning.depth(self.now, bids=[("100", "1"), ("99", "100")],
                                    asks=[("100", "1"), ("101", "100")])
        self.rule = replace(self.rule, min_qty=dec("2"))
        self.assertIsNone(self.quote())
        self.rule = replace(self.rule, min_qty=dec("1"))
        self.assert_minimum_quantity("1")

    def test_exchange_minimum_uses_mark_and_rounds_up_to_a_valid_step(self):
        self.rule = replace(self.rule, step=dec("0.03"), min_qty=dec("0.04"), min_notional=dec("7"))
        self.assert_minimum_quantity("0.09")
        self.book = replace(self.book, mark=dec("10"))
        self.assert_minimum_quantity("0.72")
        self.rule = replace(self.rule, max_qty=dec("0.71"))
        self.assertIsNone(self.quote())

    def test_complete_reference_cannot_replace_missing_minimum_quantity_depth(self):
        self.account["cycle"]["spread_notional"] = "100"
        self.rule = replace(self.rule, min_qty=dec("2"))
        for bids, asks in (([("100", "1")], None), (None, [("100", "1")])):
            with self.subTest(bids=bids, asks=asks):
                self.assertIsNone(self.quote(depth=planning.depth(self.now, bids=bids, asks=asks)))

    def test_per_side_minimum_uses_smaller_leg_and_maximum_uses_larger_leg(self):
        self.account["cycle"].update(min_notional="200", max_notional="200.02", spread_limit_bp="2")
        self.depth = planning.depth(self.now, bids=[("100", "1000")], asks=[("100.01", "1000")])
        self.assert_minimum_quantity("2")
        self.account["cycle"]["max_notional"] = "200.0199999999999999999999999999999999"
        with localcontext() as context:
            context.prec = 6
            self.assertIsNone(self.quote())

    def test_gross_scope_minimum_and_maximum_use_sum_of_both_legs(self):
        self.account["cycle"].update(notional_scope="gross", min_notional="200", max_notional="200")
        self.assert_minimum_quantity("1")
        self.account["cycle"].update(min_notional="0", max_notional="9.9999999999999999999999999999")
        self.assertIsNone(self.quote())
        self.account["cycle"]["max_notional"] = "10"
        self.assert_minimum_quantity("0.05")

    def test_gross_scope_sums_different_leg_amounts_at_exact_boundary(self):
        self.account["cycle"].update(notional_scope="gross", min_notional="200.01",
                                      max_notional="200.01", spread_limit_bp="2")
        self.depth = planning.depth(self.now, bids=[("100", "1000")], asks=[("100.01", "1000")])
        self.assert_minimum_quantity("1")
        self.account["cycle"].update(min_notional="0", max_notional="10.0004999999999999999999999999")
        self.assertIsNone(self.quote())
        self.account["cycle"]["max_notional"] = "10.0005"
        self.assert_minimum_quantity("0.05")

    def test_configured_minimum_must_fit_after_quantization(self):
        self.rule = replace(self.rule, step=dec("0.03"))
        self.account["cycle"].update(min_notional="100", max_notional="100.99999999999999999")
        self.assertIsNone(self.quote())
        self.account["cycle"]["max_notional"] = "102"
        self.assert_minimum_quantity("1.02")
        self.rule = replace(self.rule, max_qty=dec("1.01"))
        self.assertIsNone(self.quote())

    def test_exact_decimal_threshold_never_rounds_a_failure_into_success(self):
        self.depth = planning.depth(self.now, bids=[("199999", "10")], asks=[("200001", "10")])
        self.book = replace(self.book, bid=dec("199999"), ask=dec("200001"), mark=dec("200000"))
        self.assertEqual(self.quote(), {"phase": "open"})
        self.account["cycle"]["spread_limit_bp"] = "0.09999999999999999999999999999999999999"
        with localcontext() as context:
            context.prec = 6
            self.assertIsNone(self.quote())

    def test_nonterminating_valid_spread_does_not_drop_a_real_opportunity(self):
        self.depth = planning.depth(self.now, bids=[("100", "1000")], asks=[("100.0001", "1000")])
        self.book = replace(self.book, ask=dec("100.0001"))
        with localcontext() as context:
            context.prec = 6
            hint = self.quote()
        self.assertEqual(hint["phase"], "open")
        self.account["cycle"]["spread_limit_bp"] = "0"
        self.assertIsNone(self.quote())

    def test_actual_minimum_quantity_spread_has_its_own_exact_threshold(self):
        self.account["cycle"].update(spread_notional="100", spread_limit_bp="1")
        self.rule = replace(self.rule, min_qty=dec("2"))
        self.depth = planning.depth(self.now, bids=[("100", "1"), ("99.99", "100")],
                                    asks=[("100", "1"), ("100.01", "100")])
        self.assertEqual(self.quote(), {"phase": "open"})
        self.account["cycle"]["spread_limit_bp"] = "0.999999999999999999999999999999"
        with localcontext() as context:
            context.prec = 6
            self.assertIsNone(self.quote())

    def test_hold_and_waiting_close_honor_exact_frozen_duration(self):
        for phase in ("holding", "waiting_close"):
            with self.subTest(phase=phase):
                self.hold(elapsed=59.999)
                self.progress["phase"] = phase
                self.assertIsNone(self.quote())
                self.progress["opened_at"] = self.now - 60
                self.assertEqual(self.quote()["phase"], "close")

    def test_open_cooldown_honors_boundary_without_extending_or_mutating_it(self):
        self.progress["retry_at"] = self.now + 0.001
        before = deepcopy(self.progress)
        self.assertIsNone(self.quote())
        self.assertEqual(self.progress, before)
        self.progress["retry_at"] = self.now
        self.assertIsNotNone(self.quote())
        for retry_at in (True, "0", float("inf"), float("nan")):
            with self.subTest(retry_at=retry_at):
                self.progress["retry_at"] = retry_at
                self.assertIsNone(self.quote())

    def test_invalid_open_time_does_not_bypass_hold(self):
        self.hold()
        for opened_at in (None, True, "1", 0, self.now + 1, float("inf"), float("nan")):
            with self.subTest(opened_at=opened_at):
                self.progress["opened_at"] = opened_at
                self.assertIsNone(self.quote())

    def test_holding_uses_frozen_market_threshold_and_time_not_new_open_config(self):
        self.hold()
        self.account["cycle"].update(symbol="CLUSD1", hold_seconds=10000, min_notional="0", max_notional="1")
        self.assertEqual(self.quote(), {"phase": "close"})
        self.progress["config"]["enabled"] = False
        self.assertIsNone(self.quote())

    def test_close_requires_full_tracked_quantity_on_both_sides(self):
        self.account["cycle"]["spread_notional"] = "100"
        self.hold(qty="2")
        for bids, asks in (([("100", "1")], None), (None, [("100", "1")])):
            with self.subTest(bids=bids, asks=asks):
                self.assertIsNone(self.quote(depth=planning.depth(self.now, bids=bids, asks=asks)))
        self.assert_minimum_quantity("2")

    def test_close_rejects_bad_actual_spread_even_when_reference_passes(self):
        self.account["cycle"]["spread_notional"] = "100"
        self.hold(qty="2")
        self.depth = planning.depth(self.now, bids=[("100", "1"), ("99", "100")],
                                    asks=[("100", "1"), ("101", "100")])
        self.assertIsNone(self.quote())

    def test_close_obeys_step_and_quantity_bounds_but_ignores_open_amount_floors(self):
        self.hold(qty="0.01")
        self.assertEqual(self.quote()["phase"], "close")
        for rule in (replace(self.rule, step=dec("0.03")),
                     replace(self.rule, min_qty=dec("0.02")), replace(self.rule, max_qty=dec("0.009"))):
            with self.subTest(rule=rule):
                self.assertIsNone(self.quote(rule=rule))

    def test_expired_revoked_empty_and_invalid_depth_never_return_a_hint(self):
        cases = [replace(self.depth, timestamp=self.now - 3.001),
                 replace(self.depth, timestamp=self.now + 1.001),
                 replace(self.depth, validity=lambda: False),
                 replace(self.depth, bids=()), replace(self.depth, asks=()),
                 planning.depth(self.now, bids=[("101", "1000"), ("102", "1000")]),
                 planning.depth(self.now, bids=[("101", "1000")], asks=[("100", "1000")])]
        for depth in cases:
            with self.subTest(depth=depth), self.assertRaises(TradingError):
                self.quote(depth=depth)
        with self.assertRaises(TradingError):
            self.quote(book=replace(self.book, timestamp=self.now - 3.001))

    def test_corrupt_tracking_is_rejected_and_inputs_stay_unchanged(self):
        before = deepcopy((self.account, self.progress, self.book, self.depth, self.rule))
        self.quote()
        self.assertEqual((self.account, self.progress, self.book, self.depth, self.rule), before)
        self.hold()
        self.progress["quantities"]["SHORT"] = "0.999"
        before = deepcopy((self.account, self.progress))
        with self.assertRaises(TradingError):
            self.quote()
        self.assertEqual((self.account, self.progress), before)


if __name__ == "__main__":
    unittest.main()
