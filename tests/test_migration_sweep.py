from fractions import Fraction
import unittest

from trading.migration import _Sweep
from trading.models import TradingError


def execution_oracle(levels, quantity):
    """Walk each executable level independently of the planner's lookup index."""
    remaining, amount = quantity, Fraction(0)
    for price, available in levels:
        if not available:
            continue
        taken = min(remaining, available)
        amount += price * taken
        remaining -= taken
        if remaining == 0:
            return amount, price
    raise AssertionError("Oracle requires an executable quantity")


def budget_oracle(levels, amount):
    remaining, quantity = max(Fraction(0), amount), Fraction(0)
    for price, available in levels:
        if price * available >= remaining:
            return quantity + remaining / price
        quantity += available
        remaining -= price * available
    return quantity


class MigrationSweepTests(unittest.TestCase):
    def assert_queries_match_execution(self, levels, *, bids, quantities, amounts):
        sweep = _Sweep(levels, bids=bids)
        for quantity in quantities:
            with self.subTest(bids=bids, quantity=quantity):
                expected_amount, expected_price = execution_oracle(levels, quantity)
                self.assertEqual(sweep.amount(quantity), expected_amount)
                self.assertEqual(sweep.last_price(quantity), expected_price)
                self.assertEqual(sweep.quantity_for(expected_amount), quantity)
        for amount in amounts:
            with self.subTest(bids=bids, amount=amount):
                self.assertEqual(sweep.quantity_for(amount), budget_oracle(levels, amount))

    def test_single_level_and_nonpositive_budget(self):
        levels = [(Fraction("101.0000000000000000000000000001"), Fraction(".25"))]
        for bids in (False, True):
            self.assert_queries_match_execution(levels, bids=bids,
                quantities=[Fraction(0), Fraction(".125"), Fraction(".25")],
                amounts=[Fraction(-1), Fraction(0), Fraction(".1"), Fraction(100)])

    def test_both_sides_zero_levels_and_microscopic_boundary_changes(self):
        epsilon = Fraction(1, 10**40)
        for bids in (False, True):
            prices = [Fraction(100) + (-i if bids else i) * Fraction(".001") for i in range(5)]
            levels = list(zip(prices, map(Fraction, (0, "1.25", 0, "2.75", 0))))
            first_amount = prices[1] * Fraction("1.25")
            self.assert_queries_match_execution(levels, bids=bids,
                quantities=[Fraction(0), Fraction("1.25") - epsilon, Fraction("1.25"),
                            Fraction("1.25") + epsilon, Fraction(4) - epsilon, Fraction(4)],
                amounts=[Fraction(-1), Fraction(0), first_amount - epsilon,
                         first_amount, first_amount + epsilon, Fraction(1000)])
            self.assertEqual(_Sweep(levels, bids=bids).levels, [levels[1], levels[3]])

    def test_full_thousand_level_book_matches_selected_deep_boundaries(self):
        epsilon = Fraction(1, 10**40)
        for bids in (False, True):
            levels = [(Fraction(100000 + (-i if bids else i), 100), Fraction(i % 7 + 1, 1000))
                      for i in range(1000)]
            quantities, amounts = [Fraction(0)], []
            for count in (1, 499, 500, 999, 1000):
                quantity = sum((q for _, q in levels[:count]), Fraction(0))
                amount = sum((p * q for p, q in levels[:count]), Fraction(0))
                quantities.extend((quantity - epsilon, quantity))
                if count < len(levels):
                    quantities.append(quantity + epsilon)
                amounts.extend((amount - epsilon, amount, amount + epsilon))
            self.assert_queries_match_execution(levels, bids=bids, quantities=quantities, amounts=amounts)

    def test_queries_never_extrapolate_past_available_depth(self):
        levels = [(Fraction(100), Fraction(1)), (Fraction(101), Fraction(2))]
        sweep = _Sweep(levels, bids=False)
        epsilon = Fraction(1, 10**40)
        for quantity in (-epsilon, Fraction(3) + epsilon):
            with self.subTest(quantity=quantity), self.assertRaisesRegex(TradingError, "深度不足"):
                sweep.amount(quantity)
        with self.assertRaisesRegex(TradingError, "深度不足"):
            sweep.last_price(Fraction(3) + epsilon)
        self.assertEqual(sweep.quantity_for(Fraction(302) + epsilon), Fraction(3))
        self.assertEqual(sweep.last_price(Fraction(0)), Fraction(100))

    def test_invalid_order_is_rejected_even_at_zero_quantity_levels(self):
        for bids in (False, True):
            first, second = ("101", "100") if bids else ("100", "101")
            invalid = ([(first, "1"), (first, "1")],
                       [(first, "1"), (first, "0"), (second, "1")],
                       [(second, "1"), (first, "1")])
            for levels in invalid:
                with self.subTest(bids=bids, levels=levels), self.assertRaisesRegex(TradingError, "顺序"):
                    _Sweep(levels, bids=bids)

    def test_invalid_structure_prices_quantities_and_empty_depth_are_rejected(self):
        invalid = (None, {}, [], [()], [("1",)], [("1", "1", "1")],
                   [("0", "1")], [(Fraction(-1), Fraction(1))], [("1", "-1")],
                   [(Fraction(1), Fraction(-1))],
                   [("1", "0")], [("1", "NaN")], [("NaN", "1")],
                   [(Fraction(i + 1), Fraction(1)) for i in range(1001)])
        for levels in invalid:
            with self.subTest(levels=levels), self.assertRaises(TradingError):
                _Sweep(levels, bids=False)


if __name__ == "__main__":
    unittest.main()
