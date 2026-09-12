"""WS-first quote and scheduler regressions, using only local fake transports."""
import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from queue import Empty, Queue
import threading
import time
import unittest
from unittest.mock import Mock, patch

import httpx

from trading.engine import Engine
from trading.exchange import API, ExchangeError, MarketData, RateBudget
from trading.models import Book, TradingError, dec
from .helpers import Fixture


SYMBOL = "XAUUSD1"
QUOTE_PATHS = ["/fapi/v3/premiumIndex", "/fapi/v3/ticker/bookTicker"]


def streamed_book(timestamp=100.0):
    return Book(dec("200"), dec("200.01"), dec("7"), dec("8"), dec("200.005"), timestamp)


class BufferedSocket:
    """Signal only after the consumer has handled all initial messages."""

    def __init__(self, messages):
        self.messages = Queue()
        for message in messages:
            self.messages.put(json.dumps(message))
        self.drained = threading.Event()

    def recv(self, timeout=None):
        if self.messages.empty():
            self.drained.set()
        try:
            message = self.messages.get(timeout=timeout)
        except Empty:
            raise TimeoutError from None
        if message is None:
            raise EOFError("fake socket closed")
        return message

    def close(self):
        self.messages.put(None)


class WSQuoteIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.now, self.wall = 100.0, 100.0
        for target, value in (("trading.exchange.time.monotonic", lambda: self.now),
                              ("trading.exchange.time.time", lambda: self.wall)):
            clock = patch(target, side_effect=value)
            clock.start()
            self.addCleanup(clock.stop)
        self.calls = []
        self.responses = {
            QUOTE_PATHS[0]: {"symbol": SYMBOL, "markPrice": "100.005", "time": 100000},
            QUOTE_PATHS[1]: {"symbol": SYMBOL, "bidPrice": "100", "askPrice": "100.01",
                             "bidQty": "10", "askQty": "11", "time": 100000},
        }

        def handle(request):
            self.calls.append(request.url.path)
            response = self.responses[request.url.path]
            if isinstance(response, Exception):
                raise response
            return httpx.Response(200, json=copy.deepcopy(response))

        self.api = API(transport=httpx.MockTransport(handle), budget=RateBudget())
        self.addCleanup(self.api.close)
        self.stream = Mock(spec=["book", "start", "close"])
        self.stream.book.return_value = None
        self.depth_stream = Mock(spec=["snapshot", "seed_token", "seed", "start", "close"])
        self.depth_stream.snapshot.return_value = None
        self.depth_stream.seed_token.return_value = None
        self.market = MarketData(self.api, stream=self.stream, depth_stream=self.depth_stream)

    def test_constructor_does_not_start_network_and_lifecycle_is_explicit(self):
        self.stream.start.assert_not_called()
        self.stream.close.assert_not_called()
        self.assertEqual(self.calls, [])
        self.market.start_stream()
        self.stream.start.assert_called_once_with()
        self.market.close_stream()
        self.stream.close.assert_called_once_with()

    def test_healthy_ws_quotes_use_no_http_or_request_weight(self):
        quote = streamed_book()
        self.stream.book.return_value = quote
        for _ in range(8):
            self.assertEqual(self.market.book(SYMBOL), quote)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.api.budget.weight, 0)

    def test_healthy_ws_remains_available_when_rest_budget_is_exhausted(self):
        quote = streamed_book()
        self.stream.book.return_value = quote
        self.api.budget.reserve(1500)
        self.assertEqual(self.market.book(SYMBOL), quote)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.api.budget.weight, 1500)

    def test_ws_takes_over_a_recent_rest_cache_immediately(self):
        rest = self.market.book(SYMBOL)
        self.assertEqual(rest.bid, 100)
        quote = streamed_book()
        self.stream.book.return_value = quote
        self.now += .1
        self.assertEqual(self.market.book(SYMBOL), quote)
        self.assertEqual(self.calls, QUOTE_PATHS)

    def test_expired_or_invalid_stream_quote_falls_back_to_complete_rest_quote(self):
        for quote in (streamed_book(96.999), streamed_book(101.001),
                      replace(streamed_book(), bid_qty=dec(0)),
                      replace(streamed_book(), ask=dec(199))):
            with self.subTest(quote=quote):
                self.stream.book.return_value = quote
                market = MarketData(self.api, stream=self.stream)
                before = len(self.calls)
                actual = market.book(SYMBOL)
                self.assertEqual(actual, Book(dec("100"), dec("100.01"), dec("10"),
                                              dec("11"), dec("100.005"), 100.0))
                self.assertEqual(self.calls[before:], QUOTE_PATHS)

    def test_disconnect_within_one_second_cannot_reuse_the_previous_ws_quote(self):
        self.stream.book.return_value = streamed_book()
        self.assertEqual(self.market.book(SYMBOL).bid, 200)
        self.now += .1
        self.stream.book.return_value = None
        self.assertEqual(self.market.book(SYMBOL).bid, 100)
        self.assertEqual(self.calls, QUOTE_PATHS)

    def test_rejected_stream_data_falls_back_to_rest(self):
        self.stream.book.side_effect = TradingError("simulated invalid stream quote")
        self.assertEqual(self.market.book(SYMBOL).bid, 100)
        self.assertEqual(self.calls, QUOTE_PATHS)

    def test_rest_failure_after_disconnect_never_returns_the_previous_ws_quote(self):
        self.stream.book.return_value = streamed_book()
        self.market.book(SYMBOL)
        self.now += .1
        self.stream.book.return_value = None
        self.responses[QUOTE_PATHS[0]] = httpx.ConnectError("simulated REST failure")
        with self.assertRaises(ExchangeError):
            self.market.book(SYMBOL)
        self.assertEqual(self.calls, QUOTE_PATHS[:1])
        self.responses[QUOTE_PATHS[0]] = {"symbol": SYMBOL, "markPrice": "101", "time": 100000}
        self.assertEqual(self.market.book(SYMBOL).mark, 101)

    def test_eight_concurrent_fallback_readers_share_one_complete_rest_fetch(self):
        barrier = threading.Barrier(8)
        entered, release = threading.Event(), threading.Event()
        call = self.api.call

        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError("fallback fetch was not released")
            return call(*args, **kwargs)

        def read():
            barrier.wait(2)
            return self.market.book(SYMBOL)

        with patch.object(self.api, "call", side_effect=blocked), ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(read) for _ in range(8)]
            try:
                self.assertTrue(entered.wait(2))
            finally:
                release.set()
            quotes = [future.result(2) for future in futures]
        self.assertEqual(self.calls, QUOTE_PATHS)
        self.assertEqual(self.api.budget.weight, 2)
        self.assertTrue(all(quote == quotes[0] for quote in quotes))

    def test_recovered_ws_quote_does_not_wait_for_an_inflight_rest_fallback(self):
        rest_entered, release_rest = threading.Event(), threading.Event()
        ws_reader_started, ws_reader_finished = threading.Event(), threading.Event()
        call = self.api.call
        quote = streamed_book()

        def blocked_rest(*args, **kwargs):
            rest_entered.set()
            if not release_rest.wait(4):
                raise AssertionError("fallback fetch was not released")
            return call(*args, **kwargs)

        def read_recovered_ws():
            ws_reader_started.set()
            result = self.market.book(SYMBOL)
            ws_reader_finished.set()
            return result

        with patch.object(self.api, "call", side_effect=blocked_rest), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.market.book, SYMBOL)
            try:
                self.assertTrue(rest_entered.wait(1), "first reader did not begin REST fallback")
                self.stream.book.return_value = quote
                second = pool.submit(read_recovered_ws)
                self.assertTrue(ws_reader_started.wait(1), "second reader did not start")
                returned_before_rest = ws_reader_finished.wait(1)
            finally:
                release_rest.set()
            first.result(2)
            recovered = second.result(2)
        self.assertTrue(returned_before_rest, "healthy WS quote waited for an unrelated REST response")
        self.assertEqual(recovered, quote)
        self.assertEqual(self.calls, QUOTE_PATHS)

    def test_missing_ws_side_is_not_combined_with_a_rest_side(self):
        from trading.market_stream import PublicQuoteStream

        messages = (
            {"stream": "xauusd1@bookTicker", "data": {"e": "bookTicker", "s": SYMBOL,
                "E": 100000, "T": 100000, "u": 1, "b": "200", "a": "200.01", "B": "7", "A": "8"}},
            {"stream": "xauusd1@markPrice@1s", "data": {"e": "markPriceUpdate", "s": SYMBOL,
                "E": 100000, "p": "200.005"}},
        )
        for message in messages:
            with self.subTest(side=message["stream"]):
                socket = BufferedSocket([message])
                stream = PublicQuoteStream(symbols=(SYMBOL,), connect=lambda *a, **kw: socket,
                                           clock=lambda: self.wall, monotonic=lambda: self.now)
                market = MarketData(self.api, stream=stream)
                before = len(self.calls)
                stream.start()
                try:
                    self.assertTrue(socket.drained.wait(2), "fake WS message was not consumed")
                    actual = market.book(SYMBOL)
                    self.assertEqual((actual.bid, actual.ask, actual.bid_qty, actual.ask_qty, actual.mark),
                                     (dec("100"), dec("100.01"), dec("10"), dec("11"), dec("100.005")))
                    self.assertEqual(self.calls[before:], QUOTE_PATHS)
                finally:
                    stream.close()


class WSStreamLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.stream = Mock(spec=["book", "start", "close"])
        self.stream.book.return_value = None
        self.api = Mock()
        self.api.budget = RateBudget()
        self.depth_stream = Mock(spec=["snapshot", "seed_token", "seed", "start", "close"])
        self.depth_stream.snapshot.return_value = None
        self.depth_stream.seed_token.return_value = None
        self.market = MarketData(self.api, stream=self.stream, depth_stream=self.depth_stream)
        self.market.rules = self.f.market.rules
        self.engine = Engine(self.f.store, market=self.market)

    def test_scheduler_starts_stream_before_loading_rules_and_closes_all_resources_on_failure(self):
        events = []
        self.stream.start.side_effect = lambda: events.append("stream.start")
        self.stream.close.side_effect = lambda: events.append("stream.close")
        self.api.close.side_effect = lambda: events.append("api.close")

        def load_rules():
            events.append("rules")
            raise RuntimeError("simulated scheduler failure")

        with patch.object(self.market, "load_rules", side_effect=load_rules), \
             self.assertLogs("aster.trading", level="ERROR"):
            self.engine.run()
        self.assertEqual(events, ["stream.start", "rules", "stream.close", "api.close"])
        self.assertFalse(self.engine.ready)
        self.assertTrue(self.engine.shutdown.is_set())

    def test_failing_broker_and_stream_close_do_not_prevent_other_resource_cleanup(self):
        first, second = Mock(), Mock()
        first.close.side_effect = RuntimeError("private broker diagnostic")
        self.stream.close.side_effect = RuntimeError("private websocket diagnostic")
        self.api.close.side_effect = RuntimeError("private HTTP diagnostic")
        self.engine.brokers = {"first": first, "second": second}
        with patch.object(self.market, "load_rules", side_effect=RuntimeError("private scheduler diagnostic")), \
             self.assertLogs("aster.trading", level="ERROR") as log:
            self.engine.run()
        for client in (first, second, self.stream, self.api):
            client.close.assert_called_once_with()
        self.assertNotIn("private", self.engine.error + " ".join(log.output))

    def test_stream_start_failure_keeps_rest_scheduler_running_and_still_closes_resources(self):
        self.stream.start.side_effect = RuntimeError("private websocket start diagnostic")

        def notify():
            self.engine.shutdown.set()
            return 60

        with patch.object(self.market, "load_rules") as load_rules, \
             patch.object(self.engine, "tick_account", return_value=60) as tick, \
             patch.object(self.engine, "poll_market", return_value=60) as poll, \
             patch.object(self.engine, "notify", side_effect=notify), \
             self.assertLogs("aster.trading", level="WARNING") as log:
            self.engine.run()
        load_rules.assert_called_once_with()
        tick.assert_called_once_with("test")
        self.assertEqual(poll.call_count, len(self.market.rules))
        self.stream.start.assert_called_once_with()
        self.stream.close.assert_called_once_with()
        self.api.close.assert_called_once_with()
        self.assertNotIn("private", " ".join(log.output))

    def test_stream_and_http_remain_open_until_the_last_account_worker_exits(self):
        entered, release = threading.Event(), threading.Event()
        events = []
        self.stream.start.side_effect = lambda: events.append("stream.start")
        self.stream.close.side_effect = lambda: events.append("stream.close")
        self.api.close.side_effect = lambda: events.append("api.close")

        def blocked_tick(account_id):
            entered.set()
            if not release.wait(3):
                raise AssertionError("account worker was not released")
            events.append("worker.finished")
            return 60

        with patch.object(self.market, "load_rules"), \
             patch.object(self.engine, "tick_account", side_effect=blocked_tick), \
             patch.object(self.engine, "poll_market", return_value=60), \
             patch.object(self.engine, "notify", return_value=60):
            self.engine.start()
            try:
                self.assertTrue(entered.wait(2))
                self.engine.shutdown.set()
                deadline = time.monotonic() + 1
                while self.engine.ready and time.monotonic() < deadline:
                    time.sleep(.005)
                self.assertFalse(self.engine.ready)
                self.stream.close.assert_not_called()
                self.api.close.assert_not_called()
            finally:
                release.set()
                self.engine.stop()
        self.assertFalse(self.engine.thread.is_alive())
        self.assertEqual(events, ["stream.start", "worker.finished", "stream.close", "api.close"])


if __name__ == "__main__":
    unittest.main()
