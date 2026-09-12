import json
import queue
import threading
import time
import unittest
from dataclasses import FrozenInstanceError
from fractions import Fraction
from unittest.mock import Mock, patch

from trading.depth_stream import PublicDepthStream
from trading.models import TradingError


SYMBOL = "XAUUSD1"
NOW = 1_800_000_000
MS = NOW * 1000


def event(symbol=SYMBOL, **changes):
    data = dict(e="depthUpdate", E=MS, T=MS, s=symbol, U=99, u=101, pu=98,
                b=[["100", "3"]], a=[["101", "4"]])
    data.update(changes)
    return json.dumps({"stream": f"{symbol.lower()}@depth@100ms", "data": data})


def response(**changes):
    data = dict(lastUpdateId=100, E=MS, T=MS,
                bids=[["100", "2"], ["99", "5"]],
                asks=[["101", "2"], ["102", "5"]])
    data.update(changes)
    return data


def connected_cache(*, buffered=True):
    wall, ticks = Mock(return_value=NOW), Mock(return_value=100)
    stream = PublicDepthStream(clock=wall, monotonic=ticks)
    stream._connected = True
    if buffered:
        stream._handle_message(event(U=95, u=98, pu=94))
    return stream, wall, ticks


def synchronized(stream):
    if stream.seed_token(SYMBOL) is None:
        stream._handle_message(event(U=95, u=98, pu=94))
    token = stream.seed_token(SYMBOL)
    stream._handle_message(event())
    assert stream.seed(SYMBOL, response(), token=token, requested_at=NOW)
    return stream.snapshot(SYMBOL)


class FakeSocket:
    def __init__(self):
        self.messages = queue.Queue()
        self.closed = threading.Event()
        self.receiving = threading.Event()
        self.received = 0

    def recv(self, timeout):
        self.receiving.set()
        try:
            value = self.messages.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError from None
        self.received += 1
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        self.closed.set()


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.002)
    raise AssertionError("Timed out waiting for depth receiver")


class PublicDepthCacheTests(unittest.TestCase):
    def test_connected_socket_waits_for_first_symbol_event_before_requesting_seed(self):
        stream, _, _ = connected_cache(buffered=False)
        self.assertIsNone(stream.seed_token(SYMBOL))
        stream._handle_message(event())
        self.assertIsNotNone(stream.seed_token(SYMBOL))
        self.assertIsNone(stream.seed_token("CLUSD1"))

    def test_seed_is_not_exposed_before_inclusive_bridge_arrives(self):
        for first, last in ((99, 100), (100, 100), (100, 102), (99, 102)):
            with self.subTest(first=first, last=last):
                stream, _, _ = connected_cache()
                token = stream.seed_token(SYMBOL)
                self.assertEqual(token, stream.seed_token(SYMBOL))
                self.assertTrue(stream.seed(SYMBOL, response(), token=token, requested_at=NOW))
                self.assertIsNone(stream.snapshot(SYMBOL))
                self.assertIsNone(stream.seed_token(SYMBOL))
                stream._handle_message(event(U=first, u=last))
                self.assertIsNotNone(stream.snapshot(SYMBOL))

    def test_buffer_drops_events_below_seed_and_applies_bridge_and_continuation(self):
        stream, _, _ = connected_cache()
        token = stream.seed_token(SYMBOL)
        stream._handle_message(event(U=97, u=98, pu=96, b=[["100", "20"]]))
        stream._handle_message(event(U=99, u=101, pu=98, b=[["100", "4"]]))
        stream._handle_message(event(U=102, u=104, pu=101, b=[["100", "7"]]))
        self.assertTrue(stream.seed(SYMBOL, response(), token=token, requested_at=NOW))
        snapshot = stream.snapshot(SYMBOL)
        self.assertEqual(snapshot.bids[0], (Fraction(100), Fraction(7)))
        self.assertEqual(stream._states[SYMBOL].last, 104)
        self.assertEqual(len(stream._states[SYMBOL].pending), 0)

    def test_initial_bridge_gap_rejects_seed_and_changes_token(self):
        stream, _, _ = connected_cache()
        token = stream.seed_token(SYMBOL)
        stream._handle_message(event(U=101, u=103, pu=100))
        self.assertFalse(stream.seed(SYMBOL, response(), token=token, requested_at=NOW))
        self.assertIsNone(stream.snapshot(SYMBOL))
        self.assertNotEqual(token, stream.seed_token(SYMBOL))

    def test_after_seed_old_events_are_skipped_but_next_event_must_bridge(self):
        stream, _, _ = connected_cache()
        token = stream.seed_token(SYMBOL)
        stream.seed(SYMBOL, response(), token=token, requested_at=NOW)
        stream._handle_message(event(U=90, u=99, pu=89))
        self.assertIsNone(stream.snapshot(SYMBOL))
        stream._handle_message(event(U=102, u=104, pu=101))
        self.assertIsNone(stream.snapshot(SYMBOL))
        self.assertNotEqual(token, stream.seed_token(SYMBOL))

    def test_quantities_are_absolute_deletes_are_safe_and_sides_apply_atomically(self):
        stream, _, _ = connected_cache()
        old = synchronized(stream)
        stream._handle_message(event(U=102, u=103, pu=101,
                                     b=[["100", "0"], ["99.5", "8"], ["98", "0"]],
                                     a=[["101", "0"], ["101.5", "9"]]))
        snapshot = stream.snapshot(SYMBOL)
        self.assertEqual(snapshot.bids, ((Fraction("99.5"), Fraction(8)), (Fraction(99), Fraction(5))))
        self.assertEqual(snapshot.asks, ((Fraction("101.5"), Fraction(9)), (Fraction(102), Fraction(5))))
        self.assertEqual(old.bids[0], (Fraction(100), Fraction(3)))
        with self.assertRaises(FrozenInstanceError):
            snapshot.timestamp = NOW + 1
        with self.assertRaises(TypeError):
            snapshot.bids[0] = (Fraction(0), Fraction(0))

    def test_sequence_gap_revokes_previously_returned_snapshots(self):
        stream, _, _ = connected_cache()
        old = synchronized(stream)
        self.assertTrue(old.validity())
        stream._handle_message(event(U=103, u=104, pu=102))
        self.assertIsNone(stream.snapshot(SYMBOL))
        self.assertFalse(old.validity())
        self.assertIsNone(stream.seed_token(SYMBOL))
        stream._handle_message(event(U=105, u=106, pu=104))
        self.assertIsNotNone(stream.seed_token(SYMBOL))
        with self.assertRaises(TradingError):
            old.require_fresh(NOW)

    def test_duplicate_and_delayed_events_do_not_mutate_or_refresh_book(self):
        stream, wall, ticks = connected_cache()
        first = synchronized(stream)
        wall.return_value += 2
        ticks.return_value += 2
        for changes in (dict(U=99, u=101, pu=98, E=MS + 2000, b=[["100", "99"]]),
                        dict(U=90, u=100, pu=89, E=MS - 90000, b=[["100", "NaN"]])):
            stream._handle_message(event(**changes))
            current = stream.snapshot(SYMBOL)
            self.assertEqual(current.bids, first.bids)
            self.assertEqual(current.timestamp, first.timestamp)
            self.assertEqual(current.monotonic_timestamp, first.monotonic_timestamp)
            self.assertTrue(first.validity())

    def test_quiet_synchronized_book_expires_without_requesting_another_seed(self):
        stream, wall, ticks = connected_cache()
        synchronized(stream)
        wall.return_value += 60
        ticks.return_value += 60
        snapshot = stream.snapshot(SYMBOL)
        self.assertIsNotNone(snapshot)
        self.assertTrue(snapshot.validity())
        self.assertIsNone(stream.seed_token(SYMBOL))
        with self.assertRaises(TradingError):
            snapshot.require_fresh(wall.return_value)

    def test_source_age_consumes_monotonic_lifetime_after_wall_clock_rollback(self):
        stream, wall, ticks = connected_cache(buffered=False)
        stream._handle_message(event(E=MS - 2000, T=MS - 2000))
        token = stream.seed_token(SYMBOL)
        self.assertTrue(stream.seed(SYMBOL, response(), token=token, requested_at=NOW))
        snapshot = stream.snapshot(SYMBOL)
        self.assertEqual(snapshot.timestamp, NOW - 2)
        self.assertEqual(snapshot.monotonic_timestamp, 98)
        wall.return_value -= 1
        ticks.return_value += 14
        with patch("trading.depth.time.monotonic", return_value=114):
            self.assertEqual(snapshot.age(NOW - 1), 16)
            with self.assertRaises(TradingError):
                snapshot.require_fresh(NOW - 1)

    def test_old_seed_response_cannot_overwrite_new_generation_or_other_stream(self):
        stream, _, _ = connected_cache()
        old = stream.seed_token(SYMBOL)
        stream._handle_message("{")
        stream._handle_message(event())
        current = stream.seed_token(SYMBOL)
        self.assertNotEqual(old, current)
        self.assertFalse(stream.seed(SYMBOL, response(), token=old, requested_at=NOW))
        other, _, _ = connected_cache()
        self.assertFalse(other.seed(SYMBOL, response(), token=current, requested_at=NOW))
        self.assertFalse(stream.seed("CLUSD1", response(), token=current, requested_at=NOW))
        self.assertEqual(current, stream.seed_token(SYMBOL))

    def test_waiting_bridge_timeout_is_monotonic_and_rejects_late_seed(self):
        stream, wall, ticks = connected_cache()
        token = stream.seed_token(SYMBOL)
        self.assertTrue(stream.seed(SYMBOL, response(), token=token, requested_at=NOW))
        wall.return_value -= 1000
        ticks.return_value += stream._BRIDGE_TIMEOUT
        self.assertIsNone(stream.seed_token(SYMBOL))
        ticks.return_value += 0.001
        self.assertIsNone(stream.seed_token(SYMBOL))
        wall.return_value = NOW
        stream._handle_message(event())
        new = stream.seed_token(SYMBOL)
        self.assertIsNotNone(new)
        self.assertNotEqual(new, token)
        self.assertFalse(stream.seed(SYMBOL, response(), token=token, requested_at=NOW))

    def test_invalid_rest_responses_never_publish_a_snapshot(self):
        cases = [dict(lastUpdateId=value) for value in (None, True, 0, -1, 1.1, "100", 2**63)]
        cases += [dict(E=MS - 15001), dict(E=MS + 1001), dict(symbol="CLUSD1"),
                  dict(bids=[["99", "1"], ["100", "1"]]), dict(asks=[["98", "1"]]),
                  dict(bids=[["100", "NaN"]]), dict(asks=[["101", "-1"]])]
        for changes in cases:
            with self.subTest(changes=changes):
                stream, _, _ = connected_cache()
                token = stream.seed_token(SYMBOL)
                stream._handle_message(event())
                self.assertFalse(stream.seed(SYMBOL, response(**changes), token=token, requested_at=NOW))
                self.assertIsNone(stream.snapshot(SYMBOL))
                self.assertNotEqual(token, stream.seed_token(SYMBOL))

    def test_invalid_live_events_revoke_cached_and_previously_returned_snapshots(self):
        cases = [dict(E=value) for value in (None, True, 0, -1, "1", 2**63)]
        cases += [dict(U=105), dict(pu=104), dict(pu="101"), dict(T=MS + 1001),
                  dict(E=MS - 15001), dict(E=MS + 1001), dict(s="CLUSD1"), dict(e="bookTicker"),
                  dict(b=[["100", "NaN"]]), dict(b=[["100", "-1"]]), dict(a=[] , b=[["105", "1"]]),
                  dict(b=[["100", "1"], ["100", "2"]]), dict(a={}), dict(b=[["100"]])]
        for changes in cases:
            with self.subTest(changes=changes):
                stream, _, _ = connected_cache()
                old = synchronized(stream)
                update = dict(U=102, u=104, pu=101)
                update.update(changes)
                stream._handle_message(event(**update))
                self.assertIsNone(stream.snapshot(SYMBOL))
                self.assertFalse(old.validity())

    def test_new_update_with_backwards_event_or_transaction_time_invalidates(self):
        for field in ("E", "T"):
            with self.subTest(field=field):
                stream, _, _ = connected_cache()
                old = synchronized(stream)
                stream._handle_message(event(U=102, u=103, pu=101, **{field: MS - 1}))
                self.assertIsNone(stream.snapshot(SYMBOL))
                self.assertFalse(old.validity())

    def test_unknown_levels_beyond_original_snapshot_never_fill_liquidity_gaps(self):
        stream, _, _ = connected_cache()
        old = synchronized(stream)
        stream._handle_message(event(U=102, u=103, pu=101,
                                     b=[["98", "999999"]], a=[["103", "999999"]]))
        snapshot = stream.snapshot(SYMBOL)
        self.assertNotIn(Fraction(98), dict(snapshot.bids))
        self.assertNotIn(Fraction(103), dict(snapshot.asks))
        self.assertEqual(snapshot.display()["spreads"]["10000"]["status"], "insufficient")

        # Moving completely out of coverage revokes the book, then a new REST
        # seed establishes the new range instead of staying permanently empty.
        stream._handle_message(event(U=104, u=105, pu=103,
                                     b=[["100", "0"], ["99", "0"], ["98", "999999"]],
                                     a=[["101", "0"], ["102", "0"], ["103", "999999"]]))
        self.assertIsNone(stream.snapshot(SYMBOL))
        self.assertFalse(old.validity())
        stream._handle_message(event(U=106, u=107, pu=105, b=[["98", "5"]], a=[["103", "5"]]))
        token = stream.seed_token(SYMBOL)
        self.assertIsNotNone(token)
        self.assertTrue(stream.seed(SYMBOL, response(lastUpdateId=106,
                                                   bids=[["98", "2"], ["97", "5"]],
                                                   asks=[["103", "2"], ["104", "5"]]),
                                    token=token, requested_at=NOW))
        self.assertEqual(stream.snapshot(SYMBOL).bids[0], (Fraction(98), Fraction(5)))
        self.assertFalse(old.validity())

    def test_initial_empty_side_requests_rebuild_when_liquidity_first_appears(self):
        stream, _, _ = connected_cache()
        token = stream.seed_token(SYMBOL)
        self.assertTrue(stream.seed(SYMBOL, response(bids=[]), token=token, requested_at=NOW))
        stream._handle_message(event(b=[]))
        old = stream.snapshot(SYMBOL)
        self.assertEqual(old.bids, ())
        stream._handle_message(event(U=102, u=103, pu=101))
        self.assertIsNone(stream.snapshot(SYMBOL))
        self.assertFalse(old.validity())
        stream._handle_message(event(U=104, u=105, pu=103))
        token = stream.seed_token(SYMBOL)
        self.assertTrue(stream.seed(SYMBOL, response(lastUpdateId=104), token=token, requested_at=NOW))
        self.assertEqual(stream.snapshot(SYMBOL).bids[0][0], Fraction(100))

    def test_price_cache_is_bounded_and_trimmed_range_cannot_grow_back(self):
        stream, _, _ = connected_cache()
        stream._MAX_LEVELS = 2
        synchronized(stream)
        stream._handle_message(event(U=102, u=103, pu=101,
                                     b=[["99.5", "8"]], a=[["101.5", "9"]]))
        snapshot = stream.snapshot(SYMBOL)
        self.assertEqual([price for price, _ in snapshot.bids], list(map(Fraction, ("100", "99.5"))))
        self.assertEqual([price for price, _ in snapshot.asks], list(map(Fraction, ("101", "101.5"))))
        stream._handle_message(event(U=104, u=105, pu=103,
                                     b=[["100", "0"], ["99", "999"]], a=[["101", "0"], ["102", "999"]]))
        self.assertEqual(len(stream.snapshot(SYMBOL).bids), 1)
        self.assertEqual(len(stream.snapshot(SYMBOL).asks), 1)

    def test_buffer_limits_invalidate_inflight_seed_without_unbounded_growth(self):
        for by_levels in (False, True):
            with self.subTest(by_levels=by_levels):
                stream, _, _ = connected_cache()
                if by_levels:
                    stream._MAX_BUFFER_LEVELS = 3
                else:
                    stream._MAX_BUFFER_EVENTS = 2
                token = stream.seed_token(SYMBOL)
                for last in range(101, 141):
                    stream._handle_message(event(U=last, u=last, pu=last - 1))
                    state = stream._states[SYMBOL]
                    self.assertLessEqual(len(state.pending), stream._MAX_BUFFER_EVENTS)
                    self.assertLessEqual(state.pending_levels, stream._MAX_BUFFER_LEVELS)
                self.assertFalse(stream.seed(SYMBOL, response(), token=token, requested_at=NOW))
                self.assertNotEqual(token, stream.seed_token(SYMBOL))

    def test_gap_in_buffer_revokes_inflight_seed(self):
        stream, _, _ = connected_cache()
        token = stream.seed_token(SYMBOL)
        stream._handle_message(event())
        stream._handle_message(event(U=104, u=105, pu=103))
        self.assertNotEqual(token, stream.seed_token(SYMBOL))
        self.assertFalse(stream.seed(SYMBOL, response(), token=token, requested_at=NOW))
        current = stream.seed_token(SYMBOL)
        self.assertTrue(stream.seed(SYMBOL, response(lastUpdateId=104), token=current, requested_at=NOW))
        self.assertIsNotNone(stream.snapshot(SYMBOL))

    def test_malformed_and_oversized_envelopes_revoke_but_controls_do_not(self):
        for payload in ("{", "[]", "null", "x" * (PublicDepthStream._MAX_MESSAGE_BYTES + 1)):
            with self.subTest(payload=payload[:20]):
                stream, _, _ = connected_cache()
                old = synchronized(stream)
                stream._handle_message(payload)
                self.assertFalse(old.validity())
                self.assertIsNone(stream.snapshot(SYMBOL))
        stream, _, _ = connected_cache()
        old = synchronized(stream)
        for payload in ('{"result":null,"id":1}', '{"ping":1}', '{"stream":"unknown","data":{}}'):
            stream._handle_message(payload)
            self.assertEqual(stream.snapshot(SYMBOL).timestamp, old.timestamp)
            self.assertTrue(old.validity())

    def test_oversized_level_array_is_rejected_before_it_can_enter_cache(self):
        stream, _, _ = connected_cache()
        stream._MAX_UPDATE_LEVELS = 2
        old = synchronized(stream)
        stream._handle_message(event(U=102, u=103, pu=101,
                                     b=[["100", "1"], ["99.5", "2"], ["99", "3"]]))
        self.assertFalse(old.validity())
        self.assertIsNone(stream.snapshot(SYMBOL))

    def test_concurrent_readers_never_observe_half_an_update(self):
        stream, _, _ = connected_cache()
        synchronized(stream)
        failures = []

        def writer():
            for update in range(102, 302):
                quantity = str(update)
                stream._handle_message(event(U=update, u=update, pu=update - 1,
                                             b=[["100", quantity]], a=[["101", quantity]]))

        def reader():
            for _ in range(300):
                snapshot = stream.snapshot(SYMBOL)
                if snapshot and snapshot.bids[0][1] != 3 and snapshot.bids[0][1] != snapshot.asks[0][1]:
                    failures.append(snapshot)

        workers = [threading.Thread(target=writer)] + [threading.Thread(target=reader) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])


class PublicDepthLifecycleTests(unittest.TestCase):
    def test_start_is_idempotent_subscribes_once_and_close_revokes_snapshots(self):
        socket = FakeSocket()
        connect = Mock(return_value=socket)
        stream = PublicDepthStream(connect=connect, clock=lambda: NOW, monotonic=lambda: 100)
        self.addCleanup(stream.close)
        self.assertIsNone(stream.seed_token(SYMBOL))
        connect.assert_not_called()
        starters = [threading.Thread(target=stream.start) for _ in range(8)]
        for worker in starters:
            worker.start()
        for worker in starters:
            worker.join(1)
        self.assertTrue(socket.receiving.wait(1))
        connect.assert_called_once()
        self.assertEqual(connect.call_args.args[0].split("streams=")[1].split("/"),
                         ["xauusd1@depth@100ms", "spcxusd1@depth@100ms", "clusd1@depth@100ms"])
        self.assertEqual(connect.call_args.kwargs["max_size"], stream._MAX_MESSAGE_BYTES)
        self.assertEqual(connect.call_args.kwargs["max_queue"], 16)
        old = synchronized(stream)
        stream.close()
        stream.close()
        stream.start()
        self.assertFalse(old.validity())
        self.assertIsNone(stream.snapshot(SYMBOL))
        self.assertIsNone(stream.seed_token(SYMBOL))
        self.assertTrue(socket.closed.is_set())
        self.assertFalse(stream._thread.is_alive())
        connect.assert_called_once()

    def test_disconnect_and_reconnect_require_new_seed_and_reject_old_token(self):
        first, second = FakeSocket(), FakeSocket()
        reconnecting, allowed = threading.Event(), threading.Event()

        def connect(*args, **kwargs):
            if not first.closed.is_set():
                return first
            reconnecting.set()
            if not allowed.wait(2):
                raise TimeoutError
            return second

        stream = PublicDepthStream(connect=connect, clock=lambda: NOW, monotonic=lambda: 100)
        stream._RETRY_INITIAL = 0.005
        self.addCleanup(stream.close)
        self.addCleanup(allowed.set)
        stream.start()
        self.assertTrue(first.receiving.wait(1))
        first.messages.put(event(U=95, u=98, pu=94))
        wait_for(lambda: stream.seed_token(SYMBOL) is not None)
        token = stream.seed_token(SYMBOL)
        old = synchronized(stream)
        first.messages.put(OSError("disconnected"))
        self.assertTrue(reconnecting.wait(1))
        self.assertFalse(old.validity())
        self.assertIsNone(stream.snapshot(SYMBOL))
        self.assertIsNone(stream.seed_token(SYMBOL))
        allowed.set()
        self.assertTrue(second.receiving.wait(1))
        self.assertFalse(stream.seed(SYMBOL, response(), token=token, requested_at=NOW))
        second.messages.put(event(U=1, u=2, pu=0))
        wait_for(lambda: stream.seed_token(SYMBOL) is not None)
        current = stream.seed_token(SYMBOL)
        self.assertNotEqual(current, token)
        self.assertTrue(stream.seed(SYMBOL, response(lastUpdateId=1), token=current, requested_at=NOW))
        wait_for(lambda: stream.snapshot(SYMBOL) is not None)
        self.assertFalse(old.validity())

    def test_failed_start_can_retry_but_close_is_permanent(self):
        stream = PublicDepthStream(connect=Mock())
        with patch("trading.depth_stream.threading.Thread.start", side_effect=RuntimeError("unavailable")):
            with self.assertRaises(RuntimeError):
                stream.start()
        self.assertIsNone(stream._thread)
        stream.close()
        stream.start()
        self.assertIsNone(stream._thread)
        stream._connect.assert_not_called()

    def test_shutdown_interrupts_backoff(self):
        attempted = threading.Event()

        def connect(*args, **kwargs):
            attempted.set()
            raise OSError("offline")

        stream = PublicDepthStream(connect=connect)
        stream._RETRY_INITIAL = 30
        stream.start()
        self.assertTrue(attempted.wait(1))
        started = time.monotonic()
        stream.close()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertFalse(stream._thread.is_alive())

    def test_late_connect_after_close_cannot_publish_or_seed(self):
        connecting, release = threading.Event(), threading.Event()
        socket = FakeSocket()

        def connect(*args, **kwargs):
            connecting.set()
            release.wait(2)
            return socket

        stream = PublicDepthStream(connect=connect)
        stream._OPEN_TIMEOUT = stream._CLOSE_TIMEOUT = stream._RECV_TIMEOUT = 0
        self.addCleanup(release.set)
        self.addCleanup(stream.close)
        stream.start()
        self.assertTrue(connecting.wait(1))
        stream.close()
        release.set()
        wait_for(socket.closed.is_set)
        self.assertIsNone(stream.seed_token(SYMBOL))
        self.assertIsNone(stream.snapshot(SYMBOL))
        self.assertEqual(socket.received, 0)


if __name__ == "__main__":
    unittest.main()
