import unittest
from fractions import Fraction
from unittest.mock import Mock

from trading.exchange import MarketData
from trading.market_stream import PublicQuoteStream
from tests import test_depth_stream as depths
from tests import test_market_stream as quotes


class QuoteUpdateListenerTests(unittest.TestCase):
    def test_accepted_bbo_signals_original_receive_times_after_cache_write(self):
        stream, wall, ticks = quotes.connected_cache()
        stream._handle_message(quotes.event("mark"))
        observed = []

        def listener(*signal):
            unlocked = stream._lock.acquire(blocking=False)
            if unlocked:
                stream._lock.release()
            observed.append((signal, unlocked, stream.book(quotes.SYMBOL).ask if unlocked else None))

        stream.set_update_listener(listener)
        wall.return_value += 0.25
        ticks.return_value += 0.25
        stream._handle_message(quotes.event(E=quotes.MS + 100, T=quotes.MS + 100))
        self.assertEqual(observed, [((quotes.SYMBOL, "bbo", quotes.NOW + 0.25, 100.25), True, 101)])

    def test_bbo_signal_does_not_require_mark_but_mark_never_signals(self):
        stream, _, _ = quotes.connected_cache()
        listener = Mock()
        stream.set_update_listener(listener)
        stream._handle_message(quotes.event())
        listener.assert_called_once_with(quotes.SYMBOL, "bbo", quotes.NOW, 100)
        self.assertIsNone(stream.book(quotes.SYMBOL))
        stream._handle_message(quotes.event("mark"))
        self.assertEqual(listener.call_count, 1)
        self.assertIsNotNone(stream.book(quotes.SYMBOL))

    def test_duplicates_old_invalid_unrelated_and_disconnected_events_do_not_signal(self):
        cases = [
            quotes.event(), quotes.event(u=2, E=quotes.MS - 1),
            quotes.event(u=2, T=quotes.MS - 1), quotes.event(u=2, B="0"),
            quotes.event(u=2, b="102"), quotes.event(u=2, E=quotes.MS + 1001),
            quotes.event(u=2, e="depthUpdate"), "{", "[]",
            '{"stream":"other@bookTicker","data":{}}',
        ]
        for message in cases:
            with self.subTest(message=message):
                stream, _, _ = quotes.connected_cache()
                quotes.fill(stream)
                listener = Mock()
                stream.set_update_listener(listener)
                stream._handle_message(message)
                listener.assert_not_called()
        stream, _, _ = quotes.connected_cache()
        listener = Mock()
        stream.set_update_listener(listener)
        stream._connected = False
        stream._handle_message(quotes.event())
        listener.assert_not_called()

    def test_expired_newer_event_does_not_signal(self):
        stream, wall, ticks = quotes.connected_cache()
        quotes.fill(stream)
        listener = Mock()
        stream.set_update_listener(listener)
        wall.return_value += 10
        ticks.return_value += 10
        stream._handle_message(quotes.event(u=2, E=quotes.MS + 1000, T=quotes.MS + 1000))
        listener.assert_not_called()
        self.assertIsNone(stream.book(quotes.SYMBOL))

    def test_listener_can_unsubscribe_itself_replace_and_close_without_deadlock(self):
        stream, _, _ = quotes.connected_cache()
        calls = []

        def listener(*signal):
            unlocked = stream._lock.acquire(blocking=False)
            if unlocked:
                stream._lock.release()
                stream.set_update_listener(None)
            calls.append(unlocked)

        stream.set_update_listener(listener)
        stream._handle_message(quotes.event())
        stream._handle_message(quotes.event(u=2))
        self.assertEqual(calls, [True])
        replacement = Mock()
        stream.set_update_listener(replacement)
        stream._handle_message(quotes.event(u=3))
        replacement.assert_called_once()
        stream.close()
        stream._handle_message(quotes.event(u=4))
        replacement.assert_called_once()

    def test_listener_failure_does_not_disconnect_receiver(self):
        socket = quotes.FakeSocket()
        connector = Mock(return_value=socket)
        stream = PublicQuoteStream(connect=connector, clock=lambda: quotes.NOW, monotonic=lambda: 100)
        listener = Mock(side_effect=[RuntimeError("test listener"), None])
        stream.set_update_listener(listener)
        try:
            stream.start()
            quotes.wait_for(socket.receiving.is_set)
            socket.messages.put(quotes.event())
            socket.messages.put(quotes.event("mark"))
            socket.messages.put(quotes.event(u=2, a="102"))
            quotes.wait_for(lambda: listener.call_count == 2)
            self.assertEqual(stream.book(quotes.SYMBOL).ask, 102)
            self.assertTrue(stream._connected)
            connector.assert_called_once()
        finally:
            stream.close()


class DepthUpdateListenerTests(unittest.TestCase):
    def test_buffer_and_unbridged_seed_are_silent_then_live_bridge_signals(self):
        stream, _, _ = depths.connected_cache(buffered=False)
        listener = Mock()
        stream.set_update_listener(listener)
        stream._handle_message(depths.event(U=95, u=98, pu=94))
        token = stream.seed_token(depths.SYMBOL)
        self.assertTrue(stream.seed(depths.SYMBOL, depths.response(), token=token, requested_at=depths.NOW))
        stream._handle_message(depths.event(U=90, u=99, pu=89))
        listener.assert_not_called()
        self.assertIsNone(stream.snapshot(depths.SYMBOL))
        stream._handle_message(depths.event())
        listener.assert_called_once_with(depths.SYMBOL, "depth", depths.NOW, 100)
        self.assertIsNotNone(stream.snapshot(depths.SYMBOL))

    def test_seed_replay_signals_once_after_final_state_and_keeps_receive_times(self):
        stream, wall, ticks = depths.connected_cache()
        token = stream.seed_token(depths.SYMBOL)
        stream._handle_message(depths.event())
        wall.return_value += 0.5
        ticks.return_value += 0.5
        stream._handle_message(depths.event(U=102, u=104, pu=101, E=depths.MS + 500,
                                            T=depths.MS + 500, b=[["100", "7"]]))
        observed = []

        def listener(*signal):
            unlocked = stream._lock.acquire(blocking=False)
            if unlocked:
                stream._lock.release()
            snapshot = stream.snapshot(depths.SYMBOL) if unlocked else None
            observed.append((signal, unlocked, snapshot.bids[0] if snapshot else None))

        stream.set_update_listener(listener)
        wall.return_value += 0.5
        ticks.return_value += 0.5
        self.assertTrue(stream.seed(depths.SYMBOL, depths.response(), token=token, requested_at=depths.NOW))
        self.assertEqual(observed, [((depths.SYMBOL, "depth", depths.NOW + 0.5, 100.5),
                                    True, (Fraction(100), Fraction(7)))])
        self.assertEqual(stream.snapshot(depths.SYMBOL).timestamp, depths.NOW + 0.5)
        self.assertFalse(stream.seed(depths.SYMBOL, depths.response(), token=token, requested_at=depths.NOW))
        self.assertEqual(len(observed), 1)

    def test_failed_replay_does_not_publish_an_intermediate_valid_state(self):
        stream, _, _ = depths.connected_cache()
        token = stream.seed_token(depths.SYMBOL)
        listener = Mock()
        stream.set_update_listener(listener)
        stream._handle_message(depths.event())
        stream._handle_message(depths.event(U=102, u=104, pu=101, b=[["102", "7"]]))
        self.assertFalse(stream.seed(depths.SYMBOL, depths.response(), token=token, requested_at=depths.NOW))
        listener.assert_not_called()
        self.assertIsNone(stream.snapshot(depths.SYMBOL))

    def test_live_apply_is_visible_and_listener_can_remove_itself(self):
        stream, _, _ = depths.connected_cache()
        depths.synchronized(stream)
        observed = []

        def listener(*signal):
            unlocked = stream._lock.acquire(blocking=False)
            if unlocked:
                stream._lock.release()
                stream.set_update_listener(None)
            snapshot = stream.snapshot(depths.SYMBOL) if unlocked else None
            observed.append((signal, unlocked, snapshot.asks[0] if snapshot else None))

        stream.set_update_listener(listener)
        stream._handle_message(depths.event(U=102, u=104, pu=101, a=[["101", "9"]]))
        stream._handle_message(depths.event(U=105, u=106, pu=104))
        self.assertEqual(observed, [((depths.SYMBOL, "depth", depths.NOW, 100),
                                    True, (Fraction(101), Fraction(9)))])

    def test_duplicate_old_invalid_gap_and_stale_updates_do_not_signal(self):
        cases = [
            depths.event(), depths.event(U=99, u=100, pu=98),
            depths.event(U=102, u=104, pu=101, E=depths.MS - 1),
            depths.event(U=102, u=104, pu=101, T=depths.MS - 1),
            depths.event(U=102, u=104, pu=101, b=[["100", "-1"]]),
            depths.event(U=102, u=104, pu=101, b=[["102", "2"]]),
            depths.event(U=103, u=104, pu=102),
            depths.event(U=102, u=104, pu=101, E=depths.MS - 16000),
            depths.event(U=102, u=104, pu=101, E=depths.MS + 1001),
            "{", "[]", '{"stream":"other@depth@100ms","data":{}}',
        ]
        for message in cases:
            with self.subTest(message=message):
                stream, _, _ = depths.connected_cache()
                depths.synchronized(stream)
                listener = Mock()
                stream.set_update_listener(listener)
                stream._handle_message(message)
                listener.assert_not_called()

    def test_listener_failure_preserves_seed_and_following_updates(self):
        stream, _, _ = depths.connected_cache()
        listener = Mock(side_effect=RuntimeError("test listener"))
        stream.set_update_listener(listener)
        self.assertIsNotNone(depths.synchronized(stream))
        stream._handle_message(depths.event(U=102, u=104, pu=101, b=[["100", "8"]]))
        self.assertEqual(listener.call_count, 2)
        self.assertEqual(stream.snapshot(depths.SYMBOL).bids[0], (Fraction(100), Fraction(8)))
        self.assertTrue(stream._connected)
        replacement = Mock()
        stream.set_update_listener(replacement)
        stream._handle_message(depths.event(U=105, u=106, pu=104))
        replacement.assert_called_once()

    def test_disconnected_and_closed_streams_do_not_signal(self):
        for close in (False, True):
            with self.subTest(close=close):
                stream, _, _ = depths.connected_cache()
                depths.synchronized(stream)
                listener = Mock()
                stream.set_update_listener(listener)
                if close:
                    stream.close()
                else:
                    stream._connected = False
                stream._handle_message(depths.event(U=102, u=104, pu=101))
                listener.assert_not_called()


class MarketDataUpdateListenerTests(unittest.TestCase):
    def test_listener_forwards_to_both_streams_and_can_be_removed_without_io(self):
        api = Mock()
        quote, _, _ = quotes.connected_cache()
        depth, _, _ = depths.connected_cache()
        market = MarketData(api=api, stream=quote, depth_stream=depth)
        listener = Mock()
        market.set_update_listener(listener)
        quote._handle_message(quotes.event())
        depths.synchronized(depth)
        self.assertEqual([call.args[1] for call in listener.call_args_list], ["bbo", "depth"])
        market.set_update_listener(None)
        quote._handle_message(quotes.event(u=2))
        depth._handle_message(depths.event(U=102, u=104, pu=101))
        self.assertEqual(listener.call_count, 2)
        api.call.assert_not_called()

    def test_older_injected_streams_without_listener_support_remain_usable(self):
        legacy = Mock(spec=["start", "close", "book"])
        depth = Mock(spec=["start", "close", "snapshot", "set_update_listener"])
        listener = Mock()
        market = MarketData(api=Mock(), stream=legacy, depth_stream=depth)
        market.set_update_listener(listener)
        depth.set_update_listener.assert_called_once_with(listener)
        market.set_update_listener(None)
        depth.set_update_listener.assert_called_with(None)
        legacy.start.assert_not_called()
        legacy.close.assert_not_called()

    def test_invalid_listener_is_rejected_without_replacing_existing_listener(self):
        quote, _, _ = quotes.connected_cache()
        depth, _, _ = depths.connected_cache()
        market = MarketData(api=Mock(), stream=quote, depth_stream=depth)
        listener = Mock()
        market.set_update_listener(listener)
        for target in (market, quote, depth):
            with self.subTest(target=type(target).__name__):
                with self.assertRaises(ValueError):
                    target.set_update_listener(False)
        quote._handle_message(quotes.event())
        depths.synchronized(depth)
        self.assertEqual(listener.call_count, 2)


if __name__ == "__main__":
    unittest.main()
