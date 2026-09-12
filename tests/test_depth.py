"""Depth sweep arithmetic and display polling, with no live exchange calls."""
import copy
from fractions import Fraction
import unittest
from unittest.mock import Mock, patch

from trading.depth import DEPTH_MAX_AGE, DEPTH_POLL_INTERVAL, DEPTH_WEIGHT, DepthSnapshot
from trading.engine import CAPACITY_MONITOR_RESERVE, Engine, PUBLIC_POLL_ALLOWANCE
from trading.exchange import ExchangeError, MarketData
from trading.models import SYMBOLS, TradingError, dec, decimal_value
from tests.helpers import Fixture


def response(bids=None, asks=None, timestamp=100):
    return {"E": int(timestamp * 1000), "T": 1000, "lastUpdateId": 12,
            "bids": [["99", "1000"]] if bids is None else bids,
            "asks": [["100", "1000"]] if asks is None else asks}


def snapshot(data=None):
    return DepthSnapshot.from_response(response() if data is None else data, requested_at=100, now=100)


class DepthArithmeticTests(unittest.TestCase):
    def test_single_level_matches_bbo_and_uses_quote_currency_not_base_quantity(self):
        result = snapshot().display()
        for row in result["spreads"].values():
            self.assertEqual(row["status"], "ok")
            self.assertEqual(dec(row["buy_average"]), 100)
            self.assertEqual(dec(row["sell_average"]), 99)
            self.assertEqual(dec(row["spread"]), decimal_value(Fraction(2, 199)))

    def test_sweeps_multiple_levels_and_only_the_required_part_of_the_last_level(self):
        data = response(bids=[["100", "30"], ["80", "1000"]], asks=[["100", "50"], ["125", "1000"]])
        rows = snapshot(data).display()["spreads"]
        buy, sell = Fraction(10000, 90), Fraction(10000) / Fraction(235, 2)
        self.assertEqual(dec(rows["10000"]["buy_average"]), decimal_value(buy))
        self.assertEqual(dec(rows["10000"]["sell_average"]), decimal_value(sell))
        self.assertEqual(dec(rows["10000"]["spread"]), decimal_value(2 * (buy - sell) / (buy + sell)))
        self.assertGreater(dec(rows["50000"]["spread"]), dec(rows["10000"]["spread"]))

    def test_one_notional_can_be_available_while_the_other_is_insufficient(self):
        rows = snapshot(response(bids=[["100", "100"]], asks=[["100", "1000"]])).display()["spreads"]
        self.assertEqual(rows["10000"]["spread"], "0")
        self.assertEqual(rows["50000"]["status"], "insufficient")
        self.assertIsNone(rows["50000"]["spread"])
        self.assertIsNone(rows["50000"]["sell_average"])
        self.assertEqual(rows["50000"]["buy_average"], "100")

    def test_sub_decimal_precision_shortfall_is_not_rounded_up(self):
        quantity = "99.9999999999999999999999999999999999"
        row = snapshot(response(asks=[["100", quantity]])).display()["spreads"]["10000"]
        self.assertEqual(row["status"], "insufficient")
        self.assertIsNone(row["buy_average"])
        self.assertIsNone(row["spread"])

    def test_empty_or_zero_quantity_sides_are_insufficient_not_zero_spread(self):
        for bids, asks in (([], []), ([["99", "0"]], [["100", "0"]]), ([], [["100", "1000"]])):
            with self.subTest(bids=bids, asks=asks):
                for row in snapshot(response(bids, asks)).display()["spreads"].values():
                    self.assertEqual(row["status"], "insufficient")
                    self.assertIsNone(row["spread"])

    def test_invalid_or_unsorted_snapshots_are_rejected(self):
        invalid = [None, [], {}, response(bids="invalid"), response(bids=[["99"]]),
                   response(bids=[["NaN", "1"]]), response(bids=[["0", "1"]]),
                   response(bids=[["99", "-1"]]), response(bids=[["99", "1e101"]]),
                   response(bids=[["99", "1"], ["100", "1"]]),
                   response(bids=[["99", "1"], ["99", "2"]]),
                   response(asks=[["101", "1"], ["100", "1"]]),
                   response(bids=[["101", "1"]]), response(bids=[["99", "1"]] * 1001)]
        for data in invalid:
            with self.subTest(data=str(data)[:100]), self.assertRaises(TradingError):
                DepthSnapshot.from_response(data, requested_at=100, now=100)

    def test_snapshot_output_time_is_independent_of_last_transaction_and_mark_price(self):
        depth = snapshot()  # T=1s, E=100s; there is no mark price field.
        self.assertEqual(depth.timestamp, 100)
        depth.require_fresh(115)
        with self.assertRaises(TradingError):
            depth.require_fresh(115.001)

    def test_stale_future_missing_time_and_slow_requests_are_rejected(self):
        for event, requested in ((84.999, 100), (101.001, 100), (100, 84.999)):
            with self.subTest(event=event, requested=requested), self.assertRaises(TradingError):
                DepthSnapshot.from_response(response(timestamp=event), requested_at=requested, now=100)
        for event in (None, True, "NaN"):
            data = response()
            data["E"] = event
            with self.subTest(event=event), self.assertRaises(TradingError):
                DepthSnapshot.from_response(data, requested_at=100, now=100)


class DepthMarketTests(unittest.TestCase):
    def setUp(self):
        self.api, self.stream = Mock(), Mock()
        self.api.call.return_value = response()
        self.market = MarketData(self.api, stream=self.stream)

    def test_one_unsigned_request_supplies_both_amounts_without_bbo_or_mark_requests(self):
        with patch("trading.exchange.time.time", return_value=100):
            result = self.market.depth("CLUSD1").display()
        self.api.call.assert_called_once_with("GET", "/fapi/v3/depth", {"symbol": "CLUSD1", "limit": 1000}, weight=20)
        self.stream.book.assert_not_called()
        self.assertEqual(set(result["spreads"]), {"10000", "50000"})

    def test_symbol_mismatch_and_unrequested_symbols_are_rejected(self):
        self.api.call.return_value["symbol"] = "XAUUSD1"
        with self.assertRaises(TradingError):
            self.market.depth("CLUSD1")
        self.api.call.reset_mock()
        with self.assertRaises(TradingError):
            self.market.depth("BTCUSDT")
        self.api.call.assert_not_called()

    def test_network_duration_is_not_hidden_by_wall_clock_moving_backwards(self):
        with patch("trading.exchange.time.time", return_value=100), \
             patch("trading.exchange.time.monotonic", side_effect=[100, 100 + DEPTH_MAX_AGE + .001]), \
             self.assertRaisesRegex(TradingError, "耗时过长"):
            self.market.depth("CLUSD1")

    def test_exchange_failure_is_not_replaced_by_bbo_liquidity(self):
        self.api.call.side_effect = ExchangeError("rate limited", retry_after=60)
        with self.assertRaises(ExchangeError):
            self.market.depth("CLUSD1")
        self.stream.book.assert_not_called()


class DepthPollingTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.engine = Engine(self.f.store, market=self.f.market)

    def test_failure_preserves_last_sample_with_error_then_recovery_clears_error(self):
        self.assertEqual(self.engine.poll_depth("CLUSD1"), DEPTH_POLL_INTERVAL)
        last = copy.deepcopy(self.engine.markets["CLUSD1"]["depth"])
        with patch.object(self.f.market, "depth", side_effect=ExchangeError("接口限流", retry_after=60)):
            self.assertEqual(self.engine.poll_depth("CLUSD1"), 60)
        row = self.engine.state()["markets"]["CLUSD1"]
        self.assertEqual(row["depth"], last)
        self.assertEqual(row["depth_error"], "接口限流")
        self.engine.poll_depth("CLUSD1")
        self.assertNotIn("depth_error", self.engine.markets["CLUSD1"])

    def test_first_failure_does_not_invent_a_depth_sample(self):
        with patch.object(self.f.market, "depth", side_effect=TradingError("暂不可用")):
            self.engine.poll_depth("CLUSD1")
        self.assertNotIn("depth", self.engine.markets["CLUSD1"])

    def test_stale_snapshot_cannot_be_published_as_a_new_success(self):
        with patch.object(self.f.market, "depth", return_value=snapshot()):
            self.engine.poll_depth("CLUSD1")
        self.assertNotIn("depth", self.engine.markets["CLUSD1"])
        self.assertIn("已过期", self.engine.markets["CLUSD1"]["depth_error"])

    def test_stopped_engine_does_not_query_depth(self):
        self.engine.shutdown.set()
        with patch.object(self.f.market, "depth") as depth:
            self.assertEqual(self.engine.poll_depth("CLUSD1"), DEPTH_POLL_INTERVAL)
        depth.assert_not_called()

    def test_public_schedule_accounts_for_depth_and_existing_fallback_cost(self):
        self.assertGreaterEqual(PUBLIC_POLL_ALLOWANCE,
                                CAPACITY_MONITOR_RESERVE + 120 + len(SYMBOLS) * DEPTH_WEIGHT * 60 / DEPTH_POLL_INTERVAL)


if __name__ == "__main__":
    unittest.main()
