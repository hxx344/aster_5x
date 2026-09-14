import json
import queue
import threading
import time
import unittest
from unittest.mock import Mock, patch

from trading.user_stream import PrivateAccountStream


KEY = "test_listen_key_DO_NOT_LOG"


class FakeSocket:
    def __init__(self):
        self.messages = queue.Queue()
        self.closed = threading.Event()
        self.receiving = threading.Event()

    def recv(self, timeout):
        self.receiving.set()
        try:
            item = self.messages.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError from None
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
    raise AssertionError("Timed out waiting for private stream")


class PrivateAccountStreamTests(unittest.TestCase):
    def make_stream(self, api=None, connect=None, **kwargs):
        api = api or Mock()
        if api.call.side_effect is None:
            api.call.side_effect = lambda method, *a, **k: {"listenKey": KEY} if method == "POST" else {}
        socket = FakeSocket()
        connect = connect or Mock(return_value=socket)
        state, event = Mock(), Mock()
        stream = PrivateAccountStream(api, state, event, connect=connect, **kwargs)
        stream._RETRY_INITIAL = 0.005
        stream._RECV_TIMEOUT = 0.01
        self.addCleanup(stream.close)
        return stream, api, connect, socket, state, event

    def test_constructor_is_network_free_and_start_is_single_daemon(self):
        stream, api, connect, socket, state, event = self.make_stream()
        api.call.assert_not_called()
        connect.assert_not_called()
        state.assert_not_called()
        starters = [threading.Thread(target=stream.start) for _ in range(8)]
        for thread in starters:
            thread.start()
        for thread in starters:
            thread.join(1)
        self.assertTrue(socket.receiving.wait(1))
        self.assertTrue(stream._thread.daemon)
        api.call.assert_called_once_with("POST", "/fapi/v3/listenKey", signed=True, weight=1)
        connect.assert_called_once()
        self.assertEqual(connect.call_args.args, ("wss://fstream.asterdex.com/ws/" + KEY,))
        self.assertEqual(connect.call_args.kwargs["open_timeout"], 3)
        self.assertEqual(connect.call_args.kwargs["close_timeout"], 1)
        state.assert_called_once_with(True)
        event.assert_not_called()
        stream.close()
        stream.close()
        stream.start()
        self.assertFalse(stream._thread.is_alive())
        self.assertTrue(socket.closed.is_set())
        self.assertFalse(state.call_args.args[0])
        connect.assert_called_once()

    def test_account_order_config_margin_and_unknown_events_only_emit_kind(self):
        stream, api, _, socket, _, event = self.make_stream()
        stream.start()
        self.assertTrue(socket.receiving.wait(1))
        kinds = ["ACCOUNT_UPDATE", "ORDER_TRADE_UPDATE", "ACCOUNT_CONFIG_UPDATE", "MARGIN_CALL", "FUTURE_ACCOUNT_EVENT"]
        for kind in kinds:
            socket.messages.put(json.dumps({"e": kind, "E": 1, "a": {"B": [{"wb": "9999999"}]}}))
        wait_for(lambda: event.call_count == len(kinds))
        self.assertEqual([call.args for call in event.call_args_list], [(kind,) for kind in kinds])
        self.assertEqual(api.call.call_count, 1)

    def test_expiry_disconnects_before_reconnect_and_refreshes_listen_key(self):
        first, second = FakeSocket(), FakeSocket()
        entered, release = threading.Event(), threading.Event()
        calls = []

        def connect(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                return first
            entered.set()
            release.wait(2)
            return second

        stream, api, _, _, state, event = self.make_stream(connect=connect)
        self.addCleanup(release.set)
        stream.start()
        self.assertTrue(first.receiving.wait(1))
        first.messages.put('{"e":"listenKeyExpired"}')
        self.assertTrue(entered.wait(1))
        self.assertFalse(state.call_args.args[0])
        self.assertTrue(first.closed.is_set())
        event.assert_not_called()
        self.assertEqual(api.call.call_count, 2)
        release.set()
        self.assertTrue(second.receiving.wait(1))
        self.assertTrue(state.call_args.args[0])

    def test_malformed_event_fails_closed_instead_of_retaining_authority(self):
        for message in ("{", "null", "[]", "{}", '{"e":null}', '{"e":1}', '{"e":""}', '{"e":"BAD?EVENT"}'):
            with self.subTest(message=message):
                stream, _, _, socket, state, _ = self.make_stream()
                stream._RETRY_INITIAL = 30
                stream.start()
                self.assertTrue(socket.receiving.wait(1))
                socket.messages.put(message)
                wait_for(socket.closed.is_set)
                self.assertFalse(state.call_args.args[0])
                stream.close()

    def test_renewal_is_signed_and_uses_no_business_parameters(self):
        ticks = Mock(return_value=100)
        stream, api, _, socket, state, _ = self.make_stream(monotonic=ticks)
        stream.start()
        self.assertTrue(socket.receiving.wait(1))
        ticks.return_value = 100 + 30 * 60
        wait_for(lambda: api.call.call_count == 2)
        self.assertEqual(api.call.call_args.args, ("PUT", "/fapi/v3/listenKey"))
        self.assertEqual(api.call.call_args.kwargs, {"signed": True, "weight": 1})
        self.assertTrue(state.call_args.args[0])
        ticks.return_value += 1
        socket.messages.put('{"e":"ACCOUNT_UPDATE"}')
        time.sleep(0.025)
        self.assertEqual(api.call.call_count, 2)

    def test_renewal_failure_closes_socket_and_revokes_authority(self):
        ticks = Mock(return_value=100)
        api = Mock()
        api.call.side_effect = [{"listenKey": KEY}, OSError("renewal failed")]
        stream, _, _, socket, state, _ = self.make_stream(api=api, monotonic=ticks)
        stream._RETRY_INITIAL = 30
        stream.start()
        self.assertTrue(socket.receiving.wait(1))
        ticks.return_value += 30 * 60
        wait_for(socket.closed.is_set)
        self.assertFalse(state.call_args.args[0])

    def test_connection_rotates_before_exchange_twenty_four_hour_limit(self):
        ticks = Mock(return_value=100)
        first, second = FakeSocket(), FakeSocket()
        connect = Mock(side_effect=[first, second])
        stream, api, _, _, state, _ = self.make_stream(connect=connect, monotonic=ticks)
        stream.start()
        self.assertTrue(first.receiving.wait(1))
        ticks.return_value += 23 * 60 * 60 + 50 * 60
        self.assertTrue(second.receiving.wait(1))
        self.assertTrue(first.closed.is_set())
        self.assertTrue(state.call_args.args[0])
        self.assertEqual([call.args[0] for call in api.call.call_args_list], ["POST", "POST"])

    def test_bad_listen_key_never_reaches_connector(self):
        for reply in ({}, [], {"listenKey": None}, {"listenKey": ""}, {"listenKey": "x?SECRET=y"}, {"listenKey": "a/b"}):
            with self.subTest(reply=reply):
                api = Mock()
                api.call.side_effect = [reply]
                stream, _, connect, _, state, _ = self.make_stream(api=api)
                stream._RETRY_INITIAL = 30
                stream.start()
                wait_for(lambda: state.call_count > 0)
                connect.assert_not_called()
                self.assertFalse(state.call_args.args[0])
                stream.close()

    def test_private_urls_and_callback_exception_contents_never_enter_logs(self):
        connect = Mock(side_effect=OSError("wss://fstream.asterdex.com/ws/" + KEY))
        stream, _, _, _, state, _ = self.make_stream(connect=connect)
        stream._RETRY_INITIAL = 30
        with self.assertLogs("trading.user_stream", level="DEBUG") as logs:
            stream.start()
            wait_for(lambda: state.call_count > 0)
            stream.close()
        self.assertNotIn(KEY, "\n".join(logs.output))
        self.assertNotIn("wss://", "\n".join(logs.output))

    def test_close_revokes_before_waiting_for_blocked_rest_and_late_result_cannot_connect(self):
        entered, release = threading.Event(), threading.Event()
        api = Mock()

        def create(*args, **kwargs):
            entered.set()
            release.wait(2)
            return {"listenKey": KEY}

        api.call.side_effect = create
        stream, _, connect, _, state, _ = self.make_stream(api=api)
        self.addCleanup(release.set)
        stream._REST_TIMEOUT = stream._OPEN_TIMEOUT = stream._CLOSE_TIMEOUT = stream._RECV_TIMEOUT = 0
        stream.start()
        self.assertTrue(entered.wait(1))
        closer = threading.Thread(target=stream.close)
        closer.start()
        wait_for(lambda: state.call_count > 0)
        self.assertFalse(state.call_args.args[0])
        release.set()
        closer.join(1)
        self.assertFalse(closer.is_alive())
        self.assertFalse(stream._thread.is_alive())
        connect.assert_not_called()

    def test_late_socket_connect_after_close_cannot_restore_connected_state(self):
        entered, release = threading.Event(), threading.Event()
        socket = FakeSocket()

        def connect(*args, **kwargs):
            entered.set()
            release.wait(2)
            return socket

        stream, _, _, _, state, _ = self.make_stream(connect=connect)
        self.addCleanup(release.set)
        stream._REST_TIMEOUT = stream._OPEN_TIMEOUT = stream._CLOSE_TIMEOUT = stream._RECV_TIMEOUT = 0
        stream.start()
        self.assertTrue(entered.wait(1))
        stream.close()
        release.set()
        wait_for(socket.closed.is_set)
        self.assertFalse(any(call.args[0] for call in state.call_args_list))
        self.assertFalse(socket.receiving.is_set())

    def test_callback_failure_fails_closed(self):
        stream, _, _, socket, state, event = self.make_stream()
        stream._RETRY_INITIAL = 30
        event.side_effect = RuntimeError("private data")
        stream.start()
        self.assertTrue(socket.receiving.wait(1))
        socket.messages.put('{"e":"ACCOUNT_UPDATE"}')
        wait_for(socket.closed.is_set)
        self.assertFalse(state.call_args.args[0])

    def test_shutdown_interrupts_backoff_and_failed_thread_start_can_retry(self):
        stream, _, _, socket, state, _ = self.make_stream()
        with patch("trading.user_stream.threading.Thread.start", side_effect=RuntimeError("thread unavailable")):
            with self.assertRaises(RuntimeError):
                stream.start()
        self.assertIsNone(stream._thread)
        stream.start()
        self.assertTrue(socket.receiving.wait(1))
        stream._RETRY_INITIAL = 30
        socket.messages.put(OSError("offline"))
        wait_for(socket.closed.is_set)
        started = time.monotonic()
        stream.close()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertFalse(stream._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
