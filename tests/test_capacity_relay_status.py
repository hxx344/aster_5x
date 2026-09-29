"""Read-only relay telemetry must retain transport and source-age boundaries."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx

from trading.capacity_relay_client import PublicCapacityRelayClient
from trading.models import TradingError


SYMBOL = "XAUUSD1"
TOKEN = "secret-relay-token-must-not-leak"
PRIVATE_URL = "https://relay.invalid/private-relay-prefix"


class RelayStatusTests(unittest.TestCase):
    def setUp(self):
        self.wall, self.ticks = 1000.0, 100.0
        self.epoch = str(uuid4())
        self.requests = []
        self.response = httpx.Response(503)
        self.client = PublicCapacityRelayClient(
            PRIVATE_URL, TOKEN, clock=lambda: self.wall, monotonic=lambda: self.ticks,
            transport=httpx.MockTransport(self.request))
        self.addCleanup(self.client.close)

    def request(self, request):
        self.requests.append(request)
        return self.response

    def envelope(self, age=0.1, sequence=1, kind="oi", **changes):
        value = {"version": 1, "epoch": self.epoch, "sequence": sequence,
                 "kind": kind, "symbol": SYMBOL, "sampled_at": self.wall - age,
                 "published_at": self.wall, "age_ms": age * 1000,
                 "payload": {"private_payload": TOKEN}}
        value.update(changes)
        return value

    def advance(self, seconds):
        self.wall += seconds
        self.ticks += seconds

    def test_status_is_detached_read_only_and_does_not_expose_configuration_or_payload(self):
        with patch.object(self.client, "_connect") as connect, \
                patch.object(self.client._http, "stream") as stream:
            status = self.client.status()
            self.assertEqual(status["observed_at"], 1000)
            self.assertEqual(status["ws"], {
                "connected_at": None, "disconnected_at": None, "last_message_at": None,
                "last_sample_at": None, "connection_attempts": 0,
                "last_oi_sample_at": None, "failure_count": 0,
                "connected_age_seconds": None, "disconnected_age_seconds": 0,
                "oi_idle_seconds": None, "has_oi_sample": False,
                "retry_in_seconds": None, "last_error": None})
            self.assertEqual(status["http"], {
                "inflight": 0, "requests": 0, "failures": 0, "last_attempt_at": None,
                "last_success_at": None, "last_error": None})
            self.assertEqual(status["samples"], [])
            self.client._accept(self.envelope())
            status = self.client.status()
            status["samples"][0]["source"] = "changed"
            status["ws"]["connection_attempts"] = 999
            status["http"]["requests"] = 999
            current = self.client.status()
            self.assertEqual(current["samples"][0]["source"], "ws")
            self.assertEqual(current["ws"]["connection_attempts"], 0)
            self.assertEqual(current["http"]["requests"], 0)
            connect.assert_not_called()
            stream.assert_not_called()
        encoded = json.dumps(current, allow_nan=False)
        for private in (TOKEN, PRIVATE_URL, "private_payload", "Authorization"):
            self.assertNotIn(private, encoded)

    def test_status_age_matches_cache_read_for_wall_and_monotonic_clock_changes(self):
        self.client._accept(self.envelope(age=1))
        self.client._accept(self.envelope(age=100, kind="brackets"))
        for wall, ticks in ((1002, 102), (900, 105), (1010, 105), (900, 90)):
            self.wall, self.ticks = wall, ticks
            with self.subTest(wall=wall, ticks=ticks):
                samples = self.client.status()["samples"]
                self.assertEqual([row["kind"] for row in samples], ["brackets", "oi"])
                for row in samples:
                    key = row["kind"], SYMBOL
                    value = self.client._cache[key]
                    expected = max(ticks - value.started, wall - value.checked_at)
                    self.assertEqual(row["age_seconds"], expected)
                    self.assertEqual(row["received_at"], 1000)
                    with self.client._lock:
                        cached = self.client._read_locked(key, row["max_age_seconds"])
                    self.assertEqual(cached is not None, 0 <= expected <= row["max_age_seconds"])
        self.ticks = float("inf")
        self.assertTrue(all(row["age_seconds"] is None for row in self.client.status()["samples"]))

    def test_duplicate_http_success_does_not_renew_ws_sample_or_source(self):
        self.client._accept(self.envelope(age=0.2, sequence=2))
        self.advance(1)
        self.response = httpx.Response(200, json=self.envelope(age=0, sequence=2))
        self.client.sample("oi", SYMBOL, 8)
        status = self.client.status()
        row = status["samples"][0]
        self.assertEqual(row["source"], "ws")
        self.assertEqual(row["received_at"], 1000)
        self.assertAlmostEqual(row["age_seconds"], 1.2)
        self.assertEqual(status["ws"]["last_sample_at"], 1000)
        self.assertEqual(status["http"]["requests"], 1)
        self.assertEqual(status["http"]["failures"], 0)
        self.assertEqual(status["http"]["last_success_at"], 1001)
        self.advance(1)
        self.response = httpx.Response(200, json=self.envelope(sequence=3))
        self.client.sample("oi", SYMBOL, 8)
        status = self.client.status()
        self.assertEqual(status["samples"][0]["source"], "http")
        self.assertEqual(status["samples"][0]["received_at"], 1002)
        self.assertAlmostEqual(status["samples"][0]["age_seconds"], 0.1)
        self.assertEqual(status["ws"]["last_sample_at"], 1000)

    def test_invalid_http_envelopes_are_failures_without_success_or_sample_refresh(self):
        self.client._accept(self.envelope(age=1))
        for changes in ({"age_ms": float("nan")}, {"payload": None},
                        {"symbol": "OTHER"}, {"published_at": self.wall + 100}):
            with self.subTest(changes=changes):
                self.advance(1)
                # json.dumps deliberately permits NaN to exercise protocol validation.
                self.response = httpx.Response(200, content=json.dumps(self.envelope(
                    sequence=2, **changes)))
                self.client.sample("oi", SYMBOL, 8)
                status = self.client.status()
                self.assertIsNone(status["http"]["last_success_at"])
                self.assertEqual(status["http"]["last_error"], "snapshot_invalid")
                self.assertEqual(status["http"]["failures"], len(self.requests))
                self.assertEqual(status["samples"][0]["source"], "ws")
                self.assertEqual(status["samples"][0]["received_at"], 1000)
                self.assertAlmostEqual(status["samples"][0]["age_seconds"], self.wall - 999)
        self.advance(4)
        self.response = httpx.Response(200, json=self.envelope(age=9, sequence=2))
        with self.assertRaises(TradingError):
            self.client.sample("oi", SYMBOL, 8)
        self.assertEqual(self.client.status()["http"]["failures"], 5)

    def test_http_failures_are_sanitized_rate_limited_and_not_cleared_by_ws_samples(self):
        with patch.object(self.client._http, "stream", side_effect=RuntimeError(
                TOKEN + " " + PRIVATE_URL + " C:/private/relay-ca.pem")):
            for _ in range(3):
                with self.assertRaises(TradingError):
                    self.client.sample("oi", SYMBOL, 8)
        status = self.client.status()
        self.assertEqual(status["http"]["requests"], 1)
        self.assertEqual(status["http"]["failures"], 1)
        self.assertEqual(status["http"]["last_attempt_at"], 1000)
        self.assertEqual(status["http"]["last_error"], "snapshot_unavailable")
        self.client._accept(self.envelope())
        status = self.client.status()
        self.assertIsNone(status["last_error"])
        self.assertEqual(status["http"]["last_error"], "snapshot_unavailable")
        for private in (TOKEN, PRIVATE_URL, "relay-ca.pem"):
            self.assertNotIn(private, json.dumps(status))

    def test_concurrent_readers_count_one_inflight_http_request(self):
        entered, release, joined = threading.Event(), threading.Event(), threading.Event()

        def blocked(request):
            entered.set()
            if not release.wait(2):
                raise TimeoutError
            return httpx.Response(200, json=self.envelope())

        self.client._http.close()
        self.client._http = httpx.Client(transport=httpx.MockTransport(blocked))
        with ThreadPoolExecutor(max_workers=2) as pool:
            owner = pool.submit(self.client.sample, "oi", SYMBOL, 8)
            self.assertTrue(entered.wait(2))
            pending = self.client._inflight[("oi", SYMBOL)]
            original = pending.result

            def wait(**kwargs):
                joined.set()
                return original(**kwargs)

            with patch.object(pending, "result", side_effect=wait):
                follower = pool.submit(self.client.sample, "oi", SYMBOL, 8)
                self.assertTrue(joined.wait(2))
                status = self.client.status()
                self.assertEqual(status["http"]["inflight"], 1)
                self.assertEqual(status["http"]["requests"], 1)
                release.set()
                owner.result(timeout=2)
                follower.result(timeout=2)
        self.assertEqual(self.client.status()["http"]["inflight"], 0)

    def test_close_freezes_telemetry_against_late_http_success_or_failure(self):
        for fails in (False, True):
            with self.subTest(fails=fails):
                entered, release = threading.Event(), threading.Event()

                def blocked(request):
                    entered.set()
                    if not release.wait(2):
                        raise TimeoutError
                    if fails:
                        raise RuntimeError(TOKEN)
                    return httpx.Response(200, json=self.envelope())

                client = PublicCapacityRelayClient(
                    PRIVATE_URL, TOKEN, clock=lambda: self.wall, monotonic=lambda: self.ticks,
                    transport=httpx.MockTransport(blocked))
                self.addCleanup(client.close)
                with ThreadPoolExecutor(max_workers=1) as pool:
                    owner = pool.submit(client.sample, "oi", SYMBOL, 8)
                    self.assertTrue(entered.wait(2))
                    client.close()
                    closed = client.status()
                    release.set()
                    with self.assertRaises(TradingError):
                        owner.result(timeout=2)
                self.assertEqual(client.status(), closed)
                self.assertTrue(closed["closed"])
                self.assertEqual(closed["http"]["inflight"], 0)
                self.assertIsNone(closed["ws"]["retry_in_seconds"])
                self.assertEqual(closed["samples"], [])

    def test_ws_frames_disconnect_retry_and_http_recovery_have_distinct_timestamps(self):
        snapshots, attempts = {}, []
        outer = self

        class Connection:
            def __init__(self, number):
                self.number, self.step = number, 0

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def recv(self, timeout):
                if self.number > 1:
                    snapshots["reconnected"] = outer.client.status()
                    outer.client.close()
                    return "closing"
                labels = ["connected", "invalid_json", "invalid_envelope", "valid", "duplicate"]
                snapshots[labels[self.step]] = outer.client.status()
                outer.advance(0.1)
                self.step += 1
                if self.step == 1:
                    return "not-json " + TOKEN
                if self.step == 2:
                    return json.dumps(outer.envelope(payload=None))
                if self.step in (3, 4):
                    return json.dumps(outer.envelope())
                raise RuntimeError(TOKEN + " " + PRIVATE_URL)

        def connect(*args, **kwargs):
            attempts.append(self.client.status())
            return Connection(len(attempts))

        def retry(delay):
            if self.client._stop.is_set():
                return True
            snapshots["disconnected"] = self.client.status()
            self.response = httpx.Response(200, json=self.envelope(sequence=2))
            self.client.sample("oi", SYMBOL, 0.2)
            snapshots["http_recovery"] = self.client.status()
            self.advance(0.2)
            snapshots["countdown"] = self.client.status()
            self.advance(delay - 0.2)
            return False

        self.client._connect = connect
        with patch.object(self.client._stop, "wait", side_effect=retry):
            self.client._run()
        self.assertEqual(len(attempts), 2)
        self.assertEqual(snapshots["connected"]["ws"]["connected_at"], 1000)
        self.assertTrue(snapshots["connected"]["connected"])
        self.assertIsNone(snapshots["connected"]["ws"]["last_message_at"])
        self.assertAlmostEqual(snapshots["invalid_json"]["ws"]["last_message_at"], 1000.1)
        self.assertIsNone(snapshots["invalid_json"]["ws"]["last_sample_at"])
        self.assertEqual(snapshots["invalid_envelope"]["samples"], [])
        self.assertAlmostEqual(snapshots["valid"]["ws"]["last_sample_at"], 1000.3)
        self.assertAlmostEqual(snapshots["duplicate"]["ws"]["last_message_at"], 1000.4)
        self.assertEqual(snapshots["valid"]["ws"]["last_sample_at"],
                         snapshots["duplicate"]["ws"]["last_sample_at"])
        disconnected = snapshots["disconnected"]
        self.assertFalse(disconnected["connected"])
        self.assertAlmostEqual(disconnected["ws"]["disconnected_at"], 1000.5)
        self.assertEqual(disconnected["ws"]["last_error"], "stream_unavailable")
        self.assertAlmostEqual(disconnected["ws"]["retry_in_seconds"], 0.5)
        recovered = snapshots["http_recovery"]
        self.assertEqual(recovered["ws"]["last_error"], "stream_unavailable")
        self.assertIsNone(recovered["http"]["last_error"])
        self.assertIsNone(recovered["last_error"])
        self.assertEqual(recovered["samples"][0]["source"], "http")
        self.assertAlmostEqual(snapshots["countdown"]["ws"]["retry_in_seconds"], 0.3)
        reconnected = snapshots["reconnected"]
        self.assertTrue(reconnected["connected"])
        self.assertEqual(reconnected["ws"]["connection_attempts"], 2)
        self.assertAlmostEqual(reconnected["ws"]["connected_at"], 1001)
        self.assertIsNone(reconnected["ws"]["retry_in_seconds"])
        self.assertIsNone(reconnected["ws"]["last_error"])
        self.assertTrue(self.client.status()["closed"])
        self.assertNotIn(TOKEN, json.dumps(snapshots))
        self.assertNotIn(PRIVATE_URL, json.dumps(snapshots))

    def test_handshake_failures_keep_original_retry_schedule_and_hide_exception_details(self):
        delays, snapshots = [], []

        def retry(delay):
            delays.append(delay)
            snapshots.append(self.client.status())
            if len(delays) == 9:
                self.client.close()
                return True
            self.advance(delay)
            return False

        with patch.object(self.client, "_connect", side_effect=RuntimeError(TOKEN + PRIVATE_URL)), \
                patch.object(self.client._stop, "wait", side_effect=retry):
            self.client._run()
        self.assertEqual(delays, [0.5, 1, 2, 4, 8, 16, 30, 30, 30])
        for attempt, (delay, status) in enumerate(zip(delays, snapshots), 1):
            self.assertEqual(status["ws"]["connection_attempts"], attempt)
            self.assertEqual(status["ws"]["retry_in_seconds"], delay)
            self.assertEqual(status["ws"]["last_error"], "stream_unavailable")
            self.assertIsNone(status["ws"]["connected_at"])
            self.assertIsNone(status["ws"]["last_sample_at"])
            self.assertFalse(status["connected"])
        self.assertNotIn(TOKEN, json.dumps(snapshots))
        self.assertNotIn(PRIVATE_URL, json.dumps(snapshots))


if __name__ == "__main__":
    unittest.main()
