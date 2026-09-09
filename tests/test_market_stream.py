import json
import queue
import threading
import time
import unittest
from decimal import Decimal
from unittest.mock import Mock, patch

from trading.market_stream import PublicQuoteStream


SYMBOL = "XAUUSD1"
NOW = 1_800_000_000
MS = NOW * 1000


def event(kind="bbo", symbol=SYMBOL, **changes):
    if kind == "bbo":
        data = dict(e="bookTicker", E=MS, T=MS, s=symbol, u=1,
                    b="100", a="101", B="3", A="4")
        suffix = "bookTicker"
    else:
        data = dict(e="markPriceUpdate", E=MS, T=MS + 8 * 60 * 60 * 1000,
                    s=symbol, p="100.5")
        suffix = "markPrice@1s"
    data.update(changes)
    return json.dumps({"stream": f"{symbol.lower()}@{suffix}", "data": data})


def connected_cache(now=NOW, ticks=100):
    wall, monotonic = Mock(return_value=now), Mock(return_value=ticks)
    stream = PublicQuoteStream(clock=wall, monotonic=monotonic)
    stream._connected = True
    return stream, wall, monotonic


def fill(stream, **changes):
    stream._handle_message(event(**changes))
    stream._handle_message(event("mark", E=changes.get("E", MS)))


class FakeSocket:
    def __init__(self):
        self.messages = queue.Queue()
        self.closed = threading.Event()
        self.receiving = threading.Event()
        self.received = 0

    def recv(self, timeout):
        self.receiving.set()
        try:
            item = self.messages.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError from None
        self.received += 1
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed.set()


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.002)
    raise AssertionError("Timed out waiting for test receiver")


class PublicQuoteCacheTests(unittest.TestCase):
    def test_complete_quote_is_copied_and_keeps_all_book_fields(self):
        stream, _, _ = connected_cache()
        self.assertIsNone(stream.book(SYMBOL))
        stream._handle_message(event())
        self.assertIsNone(stream.book(SYMBOL))
        stream._handle_message(event("mark"))
        first = stream.book(SYMBOL)
        self.assertEqual((first.bid, first.ask, first.bid_qty, first.ask_qty, first.mark),
                         tuple(map(Decimal, ("100", "101", "3", "4", "100.5"))))
        first.bid = Decimal("500")
        self.assertEqual(stream.book(SYMBOL).bid, Decimal("100"))
        self.assertIsNone(stream.book("BTCUSDT"))

    def test_each_event_timestamp_has_inclusive_three_second_boundary(self):
        for kind, field in (("bbo", "E"), ("bbo", "T"), ("mark", "E")):
            with self.subTest(kind=kind, field=field):
                stream, wall, ticks = connected_cache()
                stream._handle_message(event(**({field: MS - 3000} if kind == "bbo" else {})))
                stream._handle_message(event("mark", **({field: MS - 3000} if kind == "mark" else {})))
                self.assertEqual(stream.book(SYMBOL).timestamp, NOW - 3)
                wall.return_value = NOW + 0.001
                ticks.return_value += 0.001
                self.assertIsNone(stream.book(SYMBOL))

    def test_each_event_timestamp_has_inclusive_future_boundary(self):
        for kind, field in (("bbo", "E"), ("bbo", "T"), ("mark", "E")):
            for advance, valid in ((1000, True), (1001, False)):
                with self.subTest(kind=kind, field=field, advance=advance):
                    stream, _, _ = connected_cache()
                    stream._handle_message(event(**({field: MS + advance} if kind == "bbo" else {})))
                    stream._handle_message(event("mark", **({field: MS + advance} if kind == "mark" else {})))
                    self.assertEqual(stream.book(SYMBOL) is not None, valid)

    def test_mark_funding_time_cannot_replace_event_time(self):
        stream, _, _ = connected_cache()
        stream._handle_message(event())
        stream._handle_message(event("mark", E=MS - 3001, T=MS))
        self.assertIsNone(stream.book(SYMBOL))
        stream._handle_message(event("mark", E=MS, T=MS + 99_999_999))
        self.assertIsNotNone(stream.book(SYMBOL))

    def test_fresh_mark_does_not_extend_old_book_and_reverse(self):
        for refresh in ("mark", "bbo"):
            with self.subTest(refresh=refresh):
                stream, wall, ticks = connected_cache()
                fill(stream)
                wall.return_value += 3.001
                ticks.return_value += 3.001
                stream._handle_message(event(refresh, E=MS + 3001, T=MS + 3001, u=2))
                self.assertIsNone(stream.book(SYMBOL))

    def test_source_age_consumes_monotonic_lifetime_even_after_clock_rollback(self):
        stream, wall, ticks = connected_cache()
        fill(stream, E=MS - 2000, T=MS - 2000)
        self.assertIsNotNone(stream.book(SYMBOL))
        wall.return_value -= 1
        ticks.return_value += 1.001
        self.assertIsNone(stream.book(SYMBOL))

    def test_clock_forward_then_back_cannot_revive_expired_quote(self):
        stream, wall, _ = connected_cache()
        fill(stream)
        wall.return_value += 4
        self.assertIsNone(stream.book(SYMBOL))
        wall.return_value = NOW
        self.assertIsNone(stream.book(SYMBOL))

    def test_duplicate_and_out_of_order_books_never_replace_or_refresh(self):
        for changes in (dict(u=1, E=MS + 1000, T=MS + 1000),
                        dict(u=0), dict(u=2, E=MS - 1), dict(u=2, T=MS - 1)):
            with self.subTest(changes=changes):
                stream, wall, ticks = connected_cache()
                fill(stream)
                stream._handle_message(event(b="99", **changes))
                book = stream.book(SYMBOL)
                if book is not None:
                    self.assertEqual(book.bid, Decimal("100"))
                    self.assertEqual(book.timestamp, NOW)
                wall.return_value = NOW + 3.001
                ticks.return_value = 103.001
                stream._handle_message(event("mark", E=MS + 3001))
                self.assertIsNone(stream.book(SYMBOL))

    def test_higher_update_id_with_equal_milliseconds_is_accepted(self):
        stream, _, _ = connected_cache()
        fill(stream)
        stream._handle_message(event(u=2, b="99"))
        self.assertEqual(stream.book(SYMBOL).bid, Decimal("99"))
        self.assertEqual(stream.book(SYMBOL).timestamp, NOW)

    def test_delayed_old_events_cannot_clear_new_quotes_even_with_bad_prices(self):
        for kind in ("bbo", "mark"):
            for bad_price in (False, True):
                with self.subTest(kind=kind, bad_price=bad_price):
                    stream, _, _ = connected_cache()
                    fill(stream, u=2)
                    previous = stream.book(SYMBOL)
                    changes = {"E": MS - 4000, "T": MS - 4000, "u": 1}
                    if bad_price:
                        changes["b" if kind == "bbo" else "p"] = "NaN"
                    stream._handle_message(event(kind, **changes))
                    self.assertEqual(stream.book(SYMBOL), previous)

    def test_duplicate_events_with_bad_prices_cannot_invalidate_current_quotes(self):
        for kind in ("bbo", "mark"):
            with self.subTest(kind=kind):
                stream, _, _ = connected_cache()
                fill(stream)
                previous = stream.book(SYMBOL)
                stream._handle_message(event(kind, **({"b": "NaN"} if kind == "bbo" else {"p": "NaN"})))
                self.assertEqual(stream.book(SYMBOL), previous)

    def test_duplicate_and_out_of_order_marks_never_refresh(self):
        for event_ms in (MS, MS - 1):
            with self.subTest(event_ms=event_ms):
                stream, wall, ticks = connected_cache()
                fill(stream)
                stream._handle_message(event("mark", E=event_ms, p="50"))
                self.assertEqual(stream.book(SYMBOL).mark, Decimal("100.5"))
                wall.return_value = NOW + 3.001
                ticks.return_value = 103.001
                stream._handle_message(event(E=MS + 3001, T=MS + 3001, u=2))
                self.assertIsNone(stream.book(SYMBOL))

    def test_bad_numeric_values_invalidate_the_corresponding_side(self):
        for field in ("b", "a", "B", "A", "p"):
            kind = "mark" if field == "p" else "bbo"
            for value in (None, True, False, "NaN", "Infinity", "-1", "0", "x", "1e101", "1e-101", "9" * 129):
                with self.subTest(field=field, value=str(value)[:20]):
                    stream, _, _ = connected_cache()
                    fill(stream)
                    stream._handle_message(event(kind, **{"u": 2, "E": MS + 1, "T": MS + 1, field: value}))
                    self.assertIsNone(stream.book(SYMBOL))

    def test_bad_event_times_or_update_ids_are_rejected(self):
        for kind, field in (("bbo", "u"), ("bbo", "E"), ("bbo", "T"), ("mark", "E")):
            for value in (None, True, 0, -1, "1800000000000", 1.5, 2**63, float("nan")):
                with self.subTest(kind=kind, field=field, value=value):
                    stream, _, _ = connected_cache()
                    fill(stream)
                    stream._handle_message(event(kind, **{"u": 2, "E": MS + 1, "T": MS + 1, field: value}))
                    self.assertIsNone(stream.book(SYMBOL))

    def test_crossed_and_incomplete_books_invalidate_cache(self):
        for remove in (None, "a", "b", "A", "B", "E", "T", "u"):
            with self.subTest(remove=remove):
                stream, _, _ = connected_cache()
                fill(stream)
                payload = json.loads(event(b="102" if remove is None else "100", u=2, E=MS + 1, T=MS + 1))
                if remove:
                    del payload["data"][remove]
                stream._handle_message(json.dumps(payload))
                self.assertIsNone(stream.book(SYMBOL))

    def test_symbol_mismatch_cannot_pair_with_other_symbol(self):
        stream, _, _ = connected_cache()
        fill(stream)
        payload = json.loads(event())
        payload["data"]["s"] = "CLUSD1"
        stream._handle_message(json.dumps(payload))
        self.assertIsNone(stream.book(SYMBOL))
        self.assertIsNone(stream.book("CLUSD1"))
        stream._handle_message(event(symbol="CLUSD1"))
        stream._handle_message(event("mark"))
        self.assertIsNone(stream.book("CLUSD1"))

    def test_bad_envelopes_invalidate_but_control_messages_do_not_refresh(self):
        for payload in ("{", "null", "[]", json.dumps({"stream": "xauusd1@bookTicker", "data": []}),
                        json.dumps({"stream": "xauusd1@markPrice@1s", "data": {}})):
            with self.subTest(payload=payload):
                stream, _, _ = connected_cache()
                fill(stream)
                stream._handle_message(payload)
                self.assertIsNone(stream.book(SYMBOL))
        stream, wall, ticks = connected_cache()
        fill(stream)
        for payload in ('{"result":null,"id":1}', '{"ping":123}', '{"stream":"other","data":{}}'):
            stream._handle_message(payload)
            self.assertEqual(stream.book(SYMBOL).timestamp, NOW)
        wall.return_value += 3.001
        ticks.return_value += 3.001
        self.assertIsNone(stream.book(SYMBOL))

    def test_invalid_event_does_not_reset_accepted_ordering_watermark(self):
        for kind in ("bbo", "mark"):
            with self.subTest(kind=kind):
                stream, _, _ = connected_cache()
                fill(stream)
                stream._handle_message(event(kind, u=2, E=MS + 1, T=MS + 1,
                                             **({"b": "NaN"} if kind == "bbo" else {"p": "NaN"})))
                stream._handle_message(event(kind))
                self.assertIsNone(stream.book(SYMBOL))
                stream._handle_message(event(kind, u=2, E=MS + 1, T=MS + 1))
                self.assertIsNotNone(stream.book(SYMBOL))

    def test_default_clock_is_resolved_at_read_time(self):
        stream = PublicQuoteStream(monotonic=lambda: 100)
        stream._connected = True
        with patch("trading.market_stream.time.time", return_value=NOW):
            fill(stream)
            self.assertIsNotNone(stream.book(SYMBOL))
        with patch("trading.market_stream.time.time", return_value=NOW + 4):
            self.assertIsNone(stream.book(SYMBOL))


class PublicQuoteLifecycleTests(unittest.TestCase):
    def test_failed_thread_start_can_be_closed_safely_and_close_remains_final(self):
        connect = Mock()
        stream = PublicQuoteStream(connect=connect)
        with patch("trading.market_stream.threading.Thread.start", side_effect=RuntimeError("thread unavailable")):
            with self.assertRaises(RuntimeError):
                stream.start()
        self.assertIsNone(stream._thread)
        stream.close()
        stream.close()
        stream.start()
        self.assertIsNone(stream._thread)
        self.assertIsNone(stream.book(SYMBOL))
        connect.assert_not_called()

    def test_failed_thread_start_can_be_retried_before_close(self):
        socket = FakeSocket()
        connect = Mock(return_value=socket)
        stream = PublicQuoteStream(connect=connect)
        self.addCleanup(stream.close)
        with patch("trading.market_stream.threading.Thread.start", side_effect=RuntimeError("thread unavailable")):
            with self.assertRaises(RuntimeError):
                stream.start()
        stream.start()
        self.assertTrue(socket.receiving.wait(1))
        connect.assert_called_once()
        stream.close()
        self.assertFalse(stream._thread.is_alive())

    def test_concurrent_start_uses_one_combined_connection_and_close_is_final(self):
        socket = FakeSocket()
        connect = Mock(return_value=socket)
        stream = PublicQuoteStream(connect=connect, clock=lambda: NOW)
        self.addCleanup(stream.close)
        connect.assert_not_called()
        self.assertIsNone(stream.book(SYMBOL))
        starters = [threading.Thread(target=stream.start) for _ in range(12)]
        for thread in starters:
            thread.start()
        for thread in starters:
            thread.join(1)
        self.assertTrue(socket.receiving.wait(1))
        connect.assert_called_once()
        url = connect.call_args.args[0]
        self.assertEqual(url.split("streams=")[1].split("/"), [
            "xauusd1@bookTicker", "xauusd1@markPrice@1s", "spcxusd1@bookTicker",
            "spcxusd1@markPrice@1s", "clusd1@bookTicker", "clusd1@markPrice@1s",
        ])
        self.assertEqual(connect.call_args.kwargs["open_timeout"], 3)
        self.assertEqual(connect.call_args.kwargs["close_timeout"], 1)
        socket.messages.put(event())
        socket.messages.put(event("mark"))
        wait_for(lambda: stream.book(SYMBOL) is not None)
        stream.close()
        stream.close()
        stream.start()
        self.assertIsNone(stream.book(SYMBOL))
        self.assertFalse(stream._thread.is_alive())
        self.assertTrue(socket.closed.is_set())
        connect.assert_called_once()

    def test_disconnect_clears_before_reconnect_and_new_connection_needs_both_sides(self):
        first, second = FakeSocket(), FakeSocket()
        reconnecting, allow_reconnect = threading.Event(), threading.Event()

        def connect(*args, **kwargs):
            if not first.closed.is_set():
                return first
            reconnecting.set()
            if not allow_reconnect.wait(2):
                raise TimeoutError
            return second

        stream = PublicQuoteStream(connect=connect, clock=lambda: NOW)
        stream._RETRY_INITIAL = 0.005
        self.addCleanup(stream.close)
        self.addCleanup(allow_reconnect.set)
        stream.start()
        first.messages.put(event(u=99))
        first.messages.put(event("mark"))
        wait_for(lambda: stream.book(SYMBOL) is not None)
        first.messages.put(OSError("disconnected"))
        self.assertTrue(reconnecting.wait(1))
        self.assertIsNone(stream.book(SYMBOL))
        allow_reconnect.set()
        self.assertTrue(second.receiving.wait(1))
        second.messages.put(event("mark"))
        wait_for(lambda: second.received == 1)
        self.assertIsNone(stream.book(SYMBOL))
        second.messages.put(event(u=1))
        wait_for(lambda: stream.book(SYMBOL) is not None)
        self.assertEqual(stream.book(SYMBOL).timestamp, NOW)

    def test_shutdown_interrupts_backoff(self):
        attempted = threading.Event()

        def connect(*args, **kwargs):
            attempted.set()
            raise OSError("offline")

        stream = PublicQuoteStream(connect=connect)
        stream._RETRY_INITIAL = 30
        stream.start()
        self.assertTrue(attempted.wait(1))
        started = time.monotonic()
        stream.close()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertFalse(stream._thread.is_alive())

    def test_reconnect_backoff_grows_and_is_capped(self):
        connect = Mock(side_effect=OSError("offline"))
        stream = PublicQuoteStream(connect=connect)
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.side_effect = [False] * 8 + [True]
        stream._stop = stop
        stream._run()
        self.assertEqual([call.args[0] for call in stop.wait.call_args_list],
                         [0.5, 1, 2, 4, 8, 16, 30, 30, 30])
        self.assertEqual(connect.call_count, 9)

    def test_late_connect_after_close_cannot_restore_cache(self):
        connecting, release = threading.Event(), threading.Event()
        socket = FakeSocket()

        def connect(*args, **kwargs):
            connecting.set()
            release.wait(2)
            return socket

        stream = PublicQuoteStream(connect=connect, clock=lambda: NOW)
        stream._OPEN_TIMEOUT = stream._CLOSE_TIMEOUT = stream._RECV_TIMEOUT = 0
        self.addCleanup(release.set)
        self.addCleanup(stream.close)
        stream.start()
        self.assertTrue(connecting.wait(1))
        stream.close()
        release.set()
        wait_for(socket.closed.is_set)
        self.assertIsNone(stream.book(SYMBOL))
        self.assertEqual(socket.received, 0)

    def test_concurrent_reads_never_see_half_updated_bbo(self):
        stream, _, _ = connected_cache()
        fill(stream)
        failures = []

        def writer():
            for update in range(2, 300):
                price = str(100 + update)
                stream._handle_message(event(u=update, b=price, a=price, B=price, A=price))

        def reader():
            for _ in range(500):
                quote = stream.book(SYMBOL)
                if quote and quote.bid != Decimal("100") and len({quote.bid, quote.ask, quote.bid_qty, quote.ask_qty}) != 1:
                    failures.append(quote)

        workers = [threading.Thread(target=writer)] + [threading.Thread(target=reader) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
