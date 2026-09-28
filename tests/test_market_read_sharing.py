"""Public valuation and quote consumers share REST reads, never account data."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import threading
from unittest import TestCase
from unittest.mock import Mock, patch

import httpx

from trading.exchange import API, ExchangeError, MarketData, RateBudget
from trading.models import Book, MarkPrice, TradingError, dec


SYMBOL = "XAUUSD1"
OTHER = "CLUSD1"
MARK = "/fapi/v3/premiumIndex"
BBO = "/fapi/v3/ticker/bookTicker"


class MarketReadSharingTests(TestCase):
    def setUp(self):
        self.wall, self.ticks = 100.0, 10.0
        for target, getter in (("time.time", lambda: self.wall), ("time.monotonic", lambda: self.ticks)):
            clock = patch(target, side_effect=getter)
            clock.start()
            self.addCleanup(clock.stop)
        self.calls = []
        self.responses = {}
        for symbol in (SYMBOL, OTHER):
            self.responses[symbol, MARK] = {"symbol": symbol, "markPrice": "100.5", "time": 100000}
            self.responses[symbol, BBO] = {"symbol": symbol, "bidPrice": "100", "askPrice": "101",
                                           "bidQty": "2", "askQty": "3", "time": 100000}

        def transport(request):
            key = request.url.params["symbol"], request.url.path
            self.calls.append(key)
            response = self.responses[key]
            if isinstance(response, Exception):
                raise response
            return httpx.Response(200, json=response)

        self.api = API(transport=httpx.MockTransport(transport), budget=RateBudget())
        self.addCleanup(self.api.close)
        self.stream = Mock(spec=["book", "mark_price"])
        self.stream.book.return_value = self.stream.mark_price.return_value = None
        self.market = MarketData(self.api, stream=self.stream)

    def test_sequential_consumers_share_the_mark_in_either_order(self):
        for first in ("mark_price", "book"):
            with self.subTest(first=first):
                market = MarketData(self.api, stream=self.stream)
                self.calls.clear()
                self.responses[SYMBOL, MARK]["time"] = 99000
                getattr(market, first)(SYMBOL)
                mark, book = market.mark_price(SYMBOL), market.book(SYMBOL)
                self.assertEqual(Counter(self.calls), {(SYMBOL, MARK): 1, (SYMBOL, BBO): 1})
                self.assertEqual((mark.timestamp, book.timestamp, mark.price, book.mark),
                                 (99, 99, dec("100.5"), dec("100.5")))

    def test_concurrent_valuation_and_books_share_one_mark_request(self):
        entered, release = threading.Event(), threading.Event()
        original = self.api.call

        def blocked(method, path, *args, **kwargs):
            if path == MARK:
                entered.set()
                if not release.wait(2):
                    raise AssertionError("mark read was not released")
            return original(method, path, *args, **kwargs)

        with patch.object(self.api, "call", side_effect=blocked), ThreadPoolExecutor(max_workers=8) as pool:
            first = pool.submit(self.market.mark_price, SYMBOL)
            try:
                self.assertTrue(entered.wait(1))
                readers = [pool.submit(self.market.book if i % 2 else self.market.mark_price, SYMBOL)
                           for i in range(7)]
            finally:
                release.set()
            results = [first.result(2), *(reader.result(2) for reader in readers)]
        self.assertEqual(Counter(self.calls), {(SYMBOL, MARK): 1, (SYMBOL, BBO): 1})
        self.assertTrue(all((row.price if isinstance(row, MarkPrice) else row.mark) == dec("100.5")
                            for row in results))
        self.assertEqual(self.api.budget.weight, 2)

    def test_another_symbol_and_recovered_ws_do_not_wait_for_shared_rest(self):
        entered, release = threading.Event(), threading.Event()
        original = self.api.call

        def blocked(method, path, params=None, **kwargs):
            if path == MARK and params == {"symbol": SYMBOL}:
                entered.set()
                if not release.wait(2):
                    raise AssertionError("mark read was not released")
            return original(method, path, params, **kwargs)

        with patch.object(self.api, "call", side_effect=blocked), ThreadPoolExecutor(max_workers=3) as pool:
            pending = pool.submit(self.market.book, SYMBOL)
            try:
                self.assertTrue(entered.wait(1))
                other = pool.submit(self.market.book, OTHER).result(1)
                self.assertEqual(other.mark, dec("100.5"))
                self.assertFalse(pending.done())
                live_mark = MarkPrice(SYMBOL, dec(150), 100)
                live_book = Book(dec(149), dec(151), dec(1), dec(1), dec(150), 100)
                self.stream.mark_price.return_value, self.stream.book.return_value = live_mark, live_book
                self.assertIs(pool.submit(self.market.mark_price, SYMBOL).result(1), live_mark)
                self.assertEqual(pool.submit(self.market.book, SYMBOL).result(1), live_book)
                self.assertFalse(pending.done())
            finally:
                release.set()
            pending.result(2)
        self.assertEqual(Counter(self.calls), {(SYMBOL, MARK): 1, (SYMBOL, BBO): 1,
                                             (OTHER, MARK): 1, (OTHER, BBO): 1})

    def test_consumers_cannot_mutate_the_shared_mark_or_book(self):
        mark = self.market.mark_price(SYMBOL)
        with self.assertRaises(FrozenInstanceError):
            mark.price = dec(999)
        first = self.market.book(SYMBOL)
        first.mark, first.bid, first.timestamp = dec(999), dec(998), 999
        second = self.market.book(SYMBOL)
        self.assertEqual((second.mark, second.bid, second.timestamp), (dec("100.5"), dec(100), 100))
        second.mark = dec(998)
        self.assertEqual(self.market.book(SYMBOL).mark, dec("100.5"))
        self.assertEqual(self.api.budget.weight, 2)

    def test_failed_expired_mark_never_renews_or_falls_back_to_old_data(self):
        self.market.mark_price(SYMBOL)
        self.ticks += 1
        self.responses[SYMBOL, MARK] = httpx.ConnectError("offline failure")
        with self.assertRaises(ExchangeError):
            self.market.book(SYMBOL)
        self.assertNotIn(SYMBOL, self.market.marks)
        self.assertNotIn(SYMBOL, self.market.books)
        self.responses[SYMBOL, MARK] = {"symbol": SYMBOL, "markPrice": "101", "time": 100000}
        self.assertEqual(self.market.book(SYMBOL).mark, dec(101))
        self.assertEqual(Counter(self.calls), {(SYMBOL, MARK): 3, (SYMBOL, BBO): 1})

    def test_bbo_failure_does_not_change_the_mark_sample_time(self):
        self.market.mark_price(SYMBOL)
        original_stamp = self.market.marks[SYMBOL][0]
        self.ticks += .9
        self.responses[SYMBOL, BBO] = httpx.ConnectError("offline failure")
        with self.assertRaises(ExchangeError):
            self.market.book(SYMBOL)
        self.assertEqual(self.market.marks[SYMBOL][0], original_stamp)
        self.assertNotIn(SYMBOL, self.market.books)
        self.ticks += .1
        self.market.mark_price(SYMBOL)
        self.assertEqual(Counter(self.calls), {(SYMBOL, MARK): 2, (SYMBOL, BBO): 1})

    def test_bbo_read_cannot_extend_either_mark_freshness_clock(self):
        for wall, ticks, error in ((103.01, 10.1, "标记价落后"), (100, 13.01, "有效期")):
            with self.subTest(wall=wall, ticks=ticks):
                self.wall, self.ticks = 100, 10
                market = MarketData(self.api, stream=self.stream)
                market.mark_price(SYMBOL)
                original = self.api.call

                def delayed(method, path, *args, **kwargs):
                    value = original(method, path, *args, **kwargs)
                    if path == BBO:
                        self.wall, self.ticks = wall, ticks
                    return value

                with patch.object(self.api, "call", side_effect=delayed), self.assertRaisesRegex(TradingError, error):
                    market.book(SYMBOL)
                self.assertNotIn(SYMBOL, market.books)

    def test_shared_mark_does_not_mask_an_expired_bbo(self):
        self.market.mark_price(SYMBOL)
        self.responses[SYMBOL, BBO]["time"] = 96000
        with self.assertRaisesRegex(TradingError, "盘口（BBO）落后程序时间 4.000 秒"):
            self.market.book(SYMBOL)
        self.assertNotIn(SYMBOL, self.market.books)
        self.assertEqual(Counter(self.calls), {(SYMBOL, MARK): 1, (SYMBOL, BBO): 1})

    def test_book_cache_retains_the_marks_monotonic_expiry_after_wall_rollback(self):
        self.responses[SYMBOL, MARK]["time"] = 97050
        self.market.book(SYMBOL)
        self.wall, self.ticks = 99.9, 10.1
        self.responses[SYMBOL, MARK] = httpx.ConnectError("fresh source unavailable")
        with self.assertRaises(ExchangeError):
            self.market.book(SYMBOL)
        self.assertNotIn(SYMBOL, self.market.books)
        self.assertEqual(Counter(self.calls), {(SYMBOL, MARK): 2, (SYMBOL, BBO): 1})

    def test_partial_ws_mark_does_not_contaminate_complete_rest_fallback(self):
        self.stream.mark_price.return_value = MarkPrice(SYMBOL, dec(999), 100)
        self.assertEqual(self.market.mark_price(SYMBOL).price, dec(999))
        self.assertEqual(self.market.book(SYMBOL).mark, dec("100.5"))
        self.assertEqual(Counter(self.calls), {(SYMBOL, MARK): 1, (SYMBOL, BBO): 1})
