"""Actual-fill FIFO spread and fixed taker fee reporting contracts."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import localcontext
from fractions import Fraction
from unittest import TestCase

from trading.cycle_cost import TAKER_RATE, calculate_cycle_costs
from trading.models import TradingError, decimal_value


DAY = datetime(2026, 9, 14, tzinfo=timezone.utc).timestamp()


def text(value):
    return format(decimal_value(Fraction(value), exact=True), "f")


def fill(trade_id, side, quantity="1", price="100", executed_at=DAY + 1,
         intent_id="open", account_id="first", symbol="XAUUSD1", **extra):
    return {"account_id": account_id, "intent_id": intent_id, "symbol": symbol, "trade_id": trade_id,
            "side": side, "quantity": quantity, "price": price, "notional": text(Fraction(quantity) * Fraction(price)),
            "executed_at": executed_at, **extra}


class CycleCostTests(TestCase):
    def calculate(self, fills, now=DAY + 100):
        return calculate_cycle_costs(fills, now)

    def test_empty_windows_and_fixed_rate_have_complete_stable_shape(self):
        result = self.calculate([])
        self.assertEqual(TAKER_RATE, Fraction(1, 8000))
        summary = {"taker_rate": "0.000125", "taker_rate_percent": "0.0125", "taker_fee": "0", "spread_cost": "0",
                   "total_cost": "0", "unmatched_notional": "0", "unmatched_fill_count": 0, "complete": True}
        self.assertEqual(result["daily"], {**summary, "utc_date": "2026-09-14"})
        self.assertEqual(result["rolling"], {**summary, "window_start": DAY + 100 - 86400, "window_end": DAY + 100})
        self.assertEqual(result["trades"], [])

    def test_both_legs_pay_fees_but_spread_is_charged_once_on_later_fill(self):
        result = self.calculate([fill("buy", "BUY", "2", "101"), fill("sell", "SELL", "2", "100", DAY + 2)])
        daily, trades = result["daily"], result["trades"]
        self.assertEqual((daily["taker_fee"], daily["spread_cost"], daily["total_cost"]), ("0.05025", "2", "2.05025"))
        self.assertTrue(daily["complete"])
        self.assertEqual((trades[0]["taker_fee"], trades[0]["spread_cost"], trades[0]["total_cost"]), ("0.02525", "0", "0.02525"))
        self.assertEqual((trades[1]["taker_fee"], trades[1]["spread_cost"], trades[1]["total_cost"]), ("0.025", "2", "2.025"))
        self.assertTrue(all(row["cost_complete"] and row["matched_quantity"] == "2" and row["unmatched_quantity"] == "0" for row in trades))

    def test_asymmetric_fills_keep_unmatched_cost_explicit(self):
        result = self.calculate([fill("buy", "BUY", "3", "101"), fill("sell", "SELL", "1", "100", DAY + 2)])
        daily, buy, sell = result["daily"], *result["trades"]
        self.assertEqual((daily["taker_fee"], daily["spread_cost"], daily["total_cost"]), ("0.050375", "1", "1.050375"))
        self.assertEqual((daily["unmatched_notional"], daily["unmatched_fill_count"], daily["complete"]), ("202", 1, False))
        self.assertEqual((buy["matched_quantity"], buy["unmatched_quantity"], buy["cost_complete"]), ("1", "2", False))
        self.assertTrue(sell["cost_complete"])

    def test_fifo_uses_partial_actual_quantities_and_preserves_favorable_spreads(self):
        rows = [fill("buy-1", "BUY", "2", "101", DAY + 1), fill("buy-2", "BUY", "1", "103", DAY + 2),
                fill("sell-1", "SELL", "2.5", "100", DAY + 3), fill("sell-2", "SELL", "0.5", "104", DAY + 4)]
        result = self.calculate(list(reversed(rows)))
        self.assertEqual(result, self.calculate(rows))
        self.assertEqual([row["spread_cost"] for row in result["trades"]], ["0", "0", "3.5", "-0.5"])
        self.assertEqual((result["daily"]["taker_fee"], result["daily"]["spread_cost"], result["daily"]["total_cost"]),
                         ("0.075875", "3", "3.075875"))
        self.assertTrue(result["daily"]["complete"])

    def test_sell_first_and_negative_total_are_not_absolute_or_clamped(self):
        result = self.calculate([fill("sell", "SELL", "2", "102"), fill("buy", "BUY", "2", "101", DAY + 2)])
        self.assertEqual([row["spread_cost"] for row in result["trades"]], ["0", "-2"])
        self.assertEqual(result["daily"]["total_cost"], "-1.94925")

    def test_open_and_close_batches_never_pair_with_each_other(self):
        rows = [fill("open-buy", "BUY", price="101", executed_at=DAY + 1),
                fill("close-sell", "SELL", price="103", executed_at=DAY + 2, intent_id="close"),
                fill("open-sell", "SELL", price="100", executed_at=DAY + 3),
                fill("close-buy", "BUY", price="102", executed_at=DAY + 4, intent_id="close")]
        result = self.calculate(rows)
        self.assertEqual([row["spread_cost"] for row in result["trades"]], ["0", "0", "1", "-1"])
        self.assertEqual(result["daily"]["taker_fee"], "0.05075")
        self.assertTrue(result["daily"]["complete"])

    def test_repair_in_same_intent_pairs_actual_opposite_side_and_pays_fee(self):
        rows = [fill("open-buy", "BUY", price="101", position_side="LONG", phase="open"),
                fill("repair-sell", "SELL", price="99", executed_at=DAY + 2, position_side="LONG", phase="repair")]
        result = self.calculate(rows)
        self.assertEqual((result["daily"]["taker_fee"], result["daily"]["spread_cost"]), ("0.025", "2"))
        self.assertTrue(result["daily"]["complete"])

    def test_different_intents_and_symbols_cannot_clear_each_others_pending_volume(self):
        for changed in ({"intent_id": "different"}, {"symbol": "CLUSD1"}):
            with self.subTest(changed=changed):
                result = self.calculate([fill("buy", "BUY", price="101"), fill("sell", "SELL", **changed)])
                self.assertEqual((result["daily"]["spread_cost"], result["daily"]["unmatched_notional"]), ("0", "201"))
                self.assertEqual(result["daily"]["unmatched_fill_count"], 2)
                self.assertFalse(result["daily"]["complete"])
        with self.assertRaisesRegex(TradingError, "不同账户"):
            self.calculate([fill("buy", "BUY"), fill("sell", "SELL", account_id="second")])

    def test_later_match_at_utc_midnight_charges_spread_to_new_day_only(self):
        rows = [fill("old-buy", "BUY", price="102", executed_at=DAY - 1),
                fill("new-sell", "SELL", price="100", executed_at=DAY)]
        before = self.calculate(rows, now=DAY - .001)
        self.assertEqual((before["daily"]["taker_fee"], before["daily"]["spread_cost"], before["daily"]["complete"]),
                         ("0.01275", "0", False))
        after = self.calculate(rows, now=DAY)
        self.assertEqual((after["daily"]["taker_fee"], after["daily"]["spread_cost"], after["daily"]["total_cost"]),
                         ("0.0125", "2", "2.0125"))
        self.assertEqual(after["rolling"]["total_cost"], "2.02525")
        self.assertEqual(after["trades"][0]["spread_cost"], "0")
        self.assertTrue(after["trades"][0]["cost_complete"])

    def test_rolling_boundary_excludes_old_fee_but_keeps_counterpart_context(self):
        now = DAY + 100
        rows = [fill("boundary-buy", "BUY", price="102", executed_at=now - 86400),
                fill("expired-unpaired", "BUY", price="999", executed_at=now - 86400 - 1, intent_id="old"),
                fill("now-sell", "SELL", price="100", executed_at=now)]
        result = self.calculate(rows, now)
        self.assertEqual((result["rolling"]["taker_fee"], result["rolling"]["spread_cost"]), ("0.0125", "2"))
        self.assertEqual((result["rolling"]["unmatched_notional"], result["rolling"]["unmatched_fill_count"]), ("0", 0))
        self.assertTrue(result["rolling"]["complete"])
        self.assertFalse(result["trades"][0]["cost_complete"])

    def test_future_fill_cannot_match_current_fill_or_enter_trade_details(self):
        rows = [fill("buy", "BUY", price="101", executed_at=DAY + 99),
                fill("future-sell", "SELL", price="100", executed_at=DAY + 101)]
        result = self.calculate(rows)
        self.assertEqual(len(result["trades"]), 1)
        self.assertEqual((result["daily"]["taker_fee"], result["daily"]["spread_cost"]), ("0.012625", "0"))
        self.assertEqual(result["daily"]["unmatched_notional"], "101")
        self.assertFalse(result["trades"][0]["cost_complete"])
        later = self.calculate(rows, DAY + 101)
        self.assertTrue(later["trades"][0]["cost_complete"])
        self.assertEqual([row["spread_cost"] for row in later["trades"]], ["0", "1"])

    def test_late_earlier_fill_and_equal_timestamp_ids_determine_stable_fifo(self):
        buy = fill("b", "BUY", price="101", executed_at=DAY + 1)
        sell = fill("c", "SELL", price="100", executed_at=DAY + 2)
        self.assertEqual(self.calculate([buy, sell])["daily"]["spread_cost"], "1")
        late = fill("a", "BUY", price="105", executed_at=DAY + 1)
        result = self.calculate([buy, sell, late])
        self.assertEqual([row["trade_id"] for row in result["trades"]], ["a", "b", "c"])
        self.assertEqual(result["daily"]["spread_cost"], "5")
        self.assertEqual(result["daily"]["unmatched_notional"], "101")
        self.assertTrue(result["trades"][0]["cost_complete"])
        self.assertFalse(result["trades"][1]["cost_complete"])

    def test_exact_decimal_fifo_avoids_nonterminating_average_and_context_rounding(self):
        rows = [fill("a", "BUY", "1", "0.1"), fill("b", "BUY", "2", "0.2", DAY + 2),
                fill("c", "SELL", "3", "0.3", DAY + 3)]
        with localcontext() as context:
            context.prec = 3
            result = self.calculate(rows)
        self.assertEqual((result["daily"]["taker_fee"], result["daily"]["spread_cost"], result["daily"]["total_cost"]),
                         ("0.000175", "-0.4", "-0.399825"))
        tiny = self.calculate([fill("tiny", "BUY", price="1e-100")])
        self.assertEqual(Fraction(tiny["daily"]["taker_fee"]), Fraction("1e-100") / 8000)

    def test_replayed_fills_deduplicate_and_input_is_not_mutated(self):
        rows = [fill("buy", "BUY", price="101"), fill("sell", "SELL", executed_at=DAY + 2)]
        original = deepcopy(rows)
        expected = self.calculate(rows)
        self.assertEqual(self.calculate([*rows, *deepcopy(rows)]), expected)
        self.assertEqual(rows, original)
        with self.assertRaisesRegex(TradingError, "冲突"):
            self.calculate([rows[0], {**rows[0], "intent_id": "different"}])

    def test_invalid_cost_input_fails_explicitly(self):
        for changed in ({"quantity": "0"}, {"price": "NaN"}, {"notional": "101"}, {"side": "UNKNOWN"},
                        {"executed_at": True}, {"executed_at": float("nan")}, {"account_id": "../other"}, {"intent_id": None}):
            with self.subTest(changed=changed), self.assertRaises(TradingError):
                self.calculate([{**fill("trade", "BUY"), **changed}])
        for now in (None, True, "123", float("inf"), -1, 10 ** 1000):
            with self.subTest(now=now), self.assertRaises(TradingError):
                self.calculate([], now)
