"""Independent position marks, using only fake responses and an in-memory transport."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx

from trading.exchange import API, ExchangeError, MarketData, RateBudget
from trading.models import MarkPrice, TradingError, dec
from .test_exchange_hardening import FixtureAPI


SYMBOL = "XAUUSD1"
PREMIUM = "/fapi/v3/premiumIndex"
BBO = "/fapi/v3/ticker/bookTicker"


def mark_response(**changes):
    return {"symbol": SYMBOL, "markPrice": "100.5", "time": 100000, **changes}


def market_fixture(response=None):
    api = FixtureAPI({PREMIUM: mark_response() if response is None else response})
    stream = SimpleNamespace(book=Mock(return_value=None), mark_price=Mock(return_value=None))
    return MarketData(api, stream=stream), api, stream


class MarkPriceValueTests(unittest.TestCase):
    def test_immutable_mark_accepts_exact_source_time_boundaries(self):
        for stamp in (97, 100, 101):
            mark = MarkPrice(SYMBOL, dec("100.5"), stamp)
            mark.require_fresh(now=100)
            with self.assertRaises(FrozenInstanceError):
                mark.price = dec(1)

    def test_invalid_price_or_source_time_cannot_authorize_a_mark(self):
        original = MarkPrice(SYMBOL, dec(100), 100)
        cases = [{"price": value} for value in (dec(0), dec(-1), Decimal("NaN"), Decimal("Infinity"), None, True)]
        cases += [{"timestamp": value} for value in (96.999, 101.001, float("nan"), float("inf"), None, True)]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(TradingError):
                replace(original, **changes).require_fresh(now=100)

    def test_monotonic_deadline_survives_wall_clock_rollback(self):
        mark = MarkPrice(SYMBOL, dec(100), 100, expires_monotonic=103)
        mark.require_fresh(now=103, monotonic=103)
        with self.assertRaises(TradingError):
            mark.require_fresh(now=100, monotonic=103.001)


class MarkPriceMarketTests(unittest.TestCase):
    def test_rest_needs_only_premium_index_and_preserves_source_time(self):
        for stamp in (97000, 100000, 101000):
            with self.subTest(stamp=stamp), patch("trading.exchange.time.time", return_value=100):
                market, api, stream = market_fixture(mark_response(time=stamp))
                mark = market.mark_price(SYMBOL)
                self.assertEqual((mark.symbol, mark.price, mark.timestamp), (SYMBOL, dec("100.5"), stamp / 1000))
                self.assertEqual([(call[0], call[1], call[2]) for call in api.calls],
                                 [("GET", PREMIUM, ({"symbol": SYMBOL},))])
                stream.book.assert_not_called()
                self.assertEqual(market.books, {})

    def test_bad_responses_are_not_cached_and_valid_retry_recovers(self):
        invalid = [[], "bad", {"symbol": "CLUSD1", "markPrice": "100", "time": 100000}]
        invalid += [mark_response(**{field: value}) for field, values in (
            ("time", (None, True, 0, "NaN", 96999, 101001)),
            ("markPrice", (None, True, "NaN", "0", "-1", "bad")),
        ) for value in values]
        for response in invalid:
            with self.subTest(response=response), patch("trading.exchange.time.time", return_value=100):
                market, api, _ = market_fixture(response)
                with self.assertRaises(TradingError):
                    market.mark_price(SYMBOL)
                api.responses[PREMIUM] = mark_response()
                self.assertEqual(market.mark_price(SYMBOL).price, dec("100.5"))
                self.assertEqual(len(api.calls), 2)

    def test_time_failure_reports_symbol_age_and_query_duration(self):
        for stamp, detail in ((96000, "落后程序时间 4.000 秒"), (102000, "领先程序时间 2.000 秒")):
            market, api, _ = market_fixture(mark_response(time=stamp))
            ticks = [10.0]
            original = api.call

            def delayed(*args, **kwargs):
                ticks[0] += .125
                return original(*args, **kwargs)

            api.call = delayed
            with self.subTest(stamp=stamp), patch("trading.exchange.time.time", return_value=100), \
                 patch("trading.exchange.time.monotonic", side_effect=lambda: ticks[0]), \
                 self.assertRaises(TradingError) as caught:
                market.mark_price(SYMBOL)
            for expected in (SYMBOL, "标记价", detail, "3 秒", "1 秒", "查询耗时 0.125 秒"):
                self.assertIn(expected, str(caught.exception))
            self.assertEqual(len(api.calls), 1)

    def test_rest_cache_has_one_second_lifetime_and_is_separate_from_books(self):
        market, api, _ = market_fixture()
        with patch("trading.exchange.time.time", return_value=100), \
             patch("trading.exchange.time.monotonic", return_value=10) as ticks:
            first = market.mark_price(SYMBOL)
            ticks.return_value = 10.999
            self.assertIs(market.mark_price(SYMBOL), first)
            self.assertEqual(len(api.calls), 1)
            ticks.return_value = 11
            self.assertIsNot(market.mark_price(SYMBOL), first)
            self.assertEqual(len(api.calls), 2)
            self.assertEqual(market.books, {})
            api.responses[BBO] = {"symbol": SYMBOL, "bidPrice": "100", "askPrice": "101",
                                  "bidQty": "2", "askQty": "2", "time": 100000}
            market.book(SYMBOL)
            self.assertEqual([call[1] for call in api.calls], [PREMIUM, PREMIUM, PREMIUM, BBO])

    def test_slow_rest_does_not_receive_a_new_full_cache_second(self):
        market, api, _ = market_fixture()
        ticks = [10.0]
        original = api.call

        def delayed(*args, **kwargs):
            ticks[0] += 1.1
            return original(*args, **kwargs)

        api.call = delayed
        with patch("trading.exchange.time.time", return_value=100), \
             patch("trading.exchange.time.monotonic", side_effect=lambda: ticks[0]):
            market.mark_price(SYMBOL)
            market.mark_price(SYMBOL)
        self.assertEqual(len(api.calls), 2)

    def test_expired_cache_cannot_revive_after_wall_clock_rollback(self):
        market, api, _ = market_fixture()
        with patch("trading.exchange.time.time", return_value=100) as wall, \
             patch("trading.exchange.time.monotonic", return_value=10) as ticks:
            market.mark_price(SYMBOL)
            wall.return_value, ticks.return_value = 104, 10.1
            with self.assertRaises(TradingError):
                market.mark_price(SYMBOL)
            wall.return_value, ticks.return_value = 100, 10.2
            market.mark_price(SYMBOL)
        self.assertEqual(len(api.calls), 3)

    def test_ws_mark_has_priority_and_is_not_retained_in_rest_cache(self):
        market, api, stream = market_fixture()
        with patch("trading.exchange.time.time", return_value=100):
            live = MarkPrice(SYMBOL, dec(123), 100)
            stream.mark_price.return_value = live
            self.assertIs(market.mark_price(SYMBOL), live)
            self.assertEqual(api.calls, [])
            stream.mark_price.return_value = None
            self.assertEqual(market.mark_price(SYMBOL).price, dec("100.5"))
            self.assertEqual(len(api.calls), 1)
            stream.book.assert_not_called()

    def test_local_read_rejects_bad_marks_without_rest(self):
        market, api, stream = market_fixture()
        for mark in (None, MarkPrice(SYMBOL, dec(100), 96), MarkPrice(SYMBOL, dec(100), 102),
                     MarkPrice("CLUSD1", dec(100), 100)):
            with self.subTest(mark=mark), patch("trading.exchange.time.time", return_value=100):
                stream.mark_price.return_value = mark
                self.assertIsNone(market._stream_mark_price(SYMBOL))
        self.assertEqual(api.calls, [])

    def test_concurrent_rest_misses_share_one_request(self):
        market, api, _ = market_fixture()
        entered, release = threading.Event(), threading.Event()
        original = api.call

        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError("REST test release missing")
            return original(*args, **kwargs)

        api.call = delayed
        with patch("trading.exchange.time.time", return_value=100), ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(market.mark_price, SYMBOL) for _ in range(8)]
            try:
                self.assertTrue(entered.wait(1))
            finally:
                release.set()
            marks = [future.result(timeout=2) for future in futures]
        self.assertTrue(all(mark is marks[0] for mark in marks))
        self.assertEqual(len(api.calls), 1)

    def test_recovered_ws_does_not_wait_for_inflight_rest(self):
        market, api, stream = market_fixture()
        entered, release = threading.Event(), threading.Event()
        original = api.call

        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError("REST test release missing")
            return original(*args, **kwargs)

        api.call = delayed
        with patch("trading.exchange.time.time", return_value=100), ThreadPoolExecutor(max_workers=2) as executor:
            pending = executor.submit(market.mark_price, SYMBOL)
            try:
                self.assertTrue(entered.wait(1))
                live = MarkPrice(SYMBOL, dec(123), 100)
                stream.mark_price.return_value = live
                recovered = executor.submit(market.mark_price, SYMBOL)
                self.assertIs(recovered.result(timeout=1), live)
                self.assertFalse(pending.done())
            finally:
                release.set()
            pending.result(timeout=2)

    def test_exchange_failure_preserves_retry_metadata_and_is_not_cached(self):
        failure = ExchangeError("wait", code=-1003, retry_after=180, http_status=429)
        market, api, _ = market_fixture(failure)
        with patch("trading.exchange.time.time", return_value=100):
            with self.assertRaises(ExchangeError) as caught:
                market.mark_price(SYMBOL)
            self.assertIs(caught.exception, failure)
            self.assertEqual((failure.code, failure.retry_after, failure.http_status), (-1003, 180, 429))
            api.responses[PREMIUM] = mark_response()
            market.mark_price(SYMBOL)
        self.assertEqual(len(api.calls), 2)

    def test_public_mark_transport_consumes_one_weight_without_bbo_or_fees(self):
        requests = []
        api = API(transport=httpx.MockTransport(lambda request: (
            requests.append(request) or httpx.Response(200, json=mark_response()))), budget=RateBudget())
        self.addCleanup(api.close)
        market = MarketData(api, stream=SimpleNamespace(mark_price=lambda symbol: None))
        with patch("trading.exchange.time.time", return_value=100):
            market.mark_price(SYMBOL)
            market.mark_price(SYMBOL)
        self.assertEqual([(request.method, request.url.path, request.url.params.get("symbol"))
                          for request in requests], [("GET", PREMIUM, SYMBOL)])
        self.assertEqual(api.budget.local_weight, 1)


if __name__ == "__main__":
    unittest.main()
