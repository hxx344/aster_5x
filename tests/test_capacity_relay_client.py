"""Offline relay boundaries: original age, authenticated TLS and shared reads."""
from concurrent.futures import ThreadPoolExecutor
import json
import queue
import ssl
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx

from trading.capacity_relay_client import PublicCapacityRelayClient
from trading.models import TradingError


SYMBOL = "XAUUSD1"
TOKEN = "secret-relay-token-must-not-leak"


class RelayClientTests(unittest.TestCase):
    def setUp(self):
        self.wall, self.ticks = 1000.0, 100.0
        self.epoch = str(uuid4())
        self.requests = []
        self.response = None
        self.client = PublicCapacityRelayClient(
            "https://relay.invalid:9443", TOKEN, clock=lambda: self.wall,
            monotonic=lambda: self.ticks, transport=httpx.MockTransport(self.request))
        self.addCleanup(self.client.close)

    def request(self, request):
        self.requests.append(request)
        return self.response or httpx.Response(503)

    def envelope(self, age=0.1, sequence=1, kind="oi", symbol=SYMBOL, epoch=None, **changes):
        result = {"version": 1, "epoch": epoch or self.epoch, "sequence": sequence,
                  "kind": kind, "symbol": symbol, "sampled_at": self.wall - age,
                  "published_at": self.wall, "age_ms": age * 1000,
                  "payload": {"success": True, "data": {"symbol": symbol, "remaining": "1000"}}}
        result.update(changes)
        return result

    def advance(self, seconds):
        self.wall += seconds
        self.ticks += seconds

    def test_constructor_and_unconfigured_environment_do_not_connect(self):
        self.assertEqual(self.requests, [])
        self.assertFalse(self.client.status()["running"])
        self.assertIsNone(PublicCapacityRelayClient.from_env({}))
        for env in ({"ASTER_CAPACITY_RELAY_TOKEN": TOKEN},
                    {"ASTER_CAPACITY_RELAY_URL": "https://relay.invalid"},
                    {"ASTER_CAPACITY_RELAY_CA_FILE": "private.pem"}):
            with self.assertRaises(ValueError) as raised:
                PublicCapacityRelayClient.from_env(env)
            self.assertNotIn(TOKEN, str(raised.exception))

    def test_rejects_unsafe_urls_and_header_tokens_without_echoing(self):
        for url in ("http://relay.invalid", "wss://relay.invalid", "https://user:secret@relay.invalid",
                    "https://relay.invalid?token=" + TOKEN, "https://relay.invalid#secret",
                    "https://relay.invalid?", "https://relay.invalid#", "https://relay.invalid:bad",
                    "https://relay.invalid\\secret", "https://relay.invalid\n/secret", "https://", None):
            with self.subTest(url=url), self.assertRaises(ValueError) as raised:
                PublicCapacityRelayClient(url, TOKEN)
            self.assertNotIn(TOKEN, str(raised.exception))
        for token in ("", "token\r\nInjected: yes", "token space", "中文"):
            with self.subTest(token=token), self.assertRaises(ValueError):
                PublicCapacityRelayClient("https://relay.invalid", token)

    def test_private_ca_is_loaded_without_disabling_hostname_verification(self):
        context = ssl.create_default_context()
        with patch("trading.capacity_relay_client.ssl.create_default_context", return_value=context) as create:
            client = PublicCapacityRelayClient("https://127.0.0.1:9443", TOKEN, ca_file="relay-ca.pem",
                                               transport=httpx.MockTransport(self.request))
        self.addCleanup(client.close)
        create.assert_called_once_with(cafile="relay-ca.pem")
        self.assertTrue(client._tls.check_hostname)
        self.assertEqual(client._tls.verify_mode, ssl.CERT_REQUIRED)

    def test_http_uses_bearer_header_and_never_follows_redirects(self):
        self.response = httpx.Response(302, headers={"Location": "https://steal.invalid/" + TOKEN})
        with self.assertRaises(TradingError) as raised:
            self.client.sample("oi", SYMBOL, 8)
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(request.headers["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(request.url.path, "/v1/snapshot")
        self.assertEqual(dict(request.url.params), {"kind": "oi", "symbol": SYMBOL})
        self.assertNotIn(TOKEN, str(request.url))
        self.assertNotIn(TOKEN, str(raised.exception) + repr(self.client.status()))

    def test_oversized_chunked_http_stops_reading_before_json_parse(self):
        consumed = []
        closed = threading.Event()
        class OversizedStream(httpx.SyncByteStream):
            def __iter__(self):
                for index in range(80):
                    consumed.append(index)
                    yield b"x" * (64 * 1024)
            def close(self):
                closed.set()
        self.response = httpx.Response(200, stream=OversizedStream(),
                                      headers={"Transfer-Encoding": "chunked"})
        with patch("trading.capacity_relay_client.json.loads") as parse:
            with self.assertRaises(TradingError) as raised:
                self.client.sample("oi", SYMBOL, 8)
            parse.assert_not_called()
        self.assertEqual(len(consumed), 33)
        self.assertTrue(closed.is_set())
        self.assertEqual(self.client.status()["cached_samples"], 0)
        self.assertNotIn(TOKEN, str(raised.exception) + repr(self.client.status()))

    def test_original_sample_age_and_returned_payload_are_preserved(self):
        envelope = self.envelope(age=3)
        self.response = httpx.Response(200, json=envelope)
        started, checked, payload = self.client.sample("oi", SYMBOL, 8)
        self.assertEqual((started, checked), (97, 997))
        self.assertEqual(payload, envelope["payload"])
        payload["data"]["remaining"] = "mutated"
        self.advance(2)
        self.assertEqual(self.client.sample("oi", SYMBOL, 8), (97, 997, envelope["payload"]))
        self.assertEqual(len(self.requests), 2)

    def test_duplicate_and_out_of_order_samples_never_renew_age(self):
        self.assertTrue(self.client._accept(self.envelope(age=0.5, sequence=20)))
        original = self.client.sample("oi", SYMBOL, 8)
        self.advance(2)
        for sequence in (19, 20):
            self.assertFalse(self.client._accept(self.envelope(age=0, sequence=sequence)))
        self.assertEqual(self.client.sample("oi", SYMBOL, 8), original)
        self.response = httpx.Response(200, json=self.envelope(age=0, sequence=20))
        with self.assertRaises(TradingError):
            self.client.sample("oi", SYMBOL, 1)
        self.assertEqual(self.client.sample("oi", SYMBOL, 8), original)

    def test_sequence_order_is_per_key_and_retired_epoch_cannot_return(self):
        self.assertTrue(self.client._accept(self.envelope(sequence=20)))
        self.assertTrue(self.client._accept(self.envelope(sequence=10, kind="brackets")))
        second_epoch = str(uuid4())
        self.assertTrue(self.client._accept(self.envelope(sequence=1, epoch=second_epoch)))
        self.assertFalse(self.client._accept(self.envelope(sequence=100)))
        self.assertFalse(self.client._accept(self.envelope(sequence=101, kind="brackets")))
        self.assertEqual(self.client.status()["cached_samples"], 1)

    def test_old_connection_and_inflight_http_cannot_overwrite_new_session(self):
        self.client._connection_generation = 3
        self.assertFalse(self.client._accept(self.envelope(), generation=2))
        self.assertTrue(self.client._accept(self.envelope(), generation=3))
        serial = self.client._epoch_serial
        self.assertTrue(self.client._accept(self.envelope(epoch=str(uuid4()))))
        self.assertFalse(self.client._accept(self.envelope(epoch=str(uuid4())), epoch_serial=serial))

    def test_disconnection_keeps_original_cache_but_http_old_data_is_not_fresh(self):
        self.assertTrue(self.client._accept(self.envelope(age=2)))
        self.client._connected = False
        self.assertEqual(self.client.sample("oi", SYMBOL, 8)[:2], (98, 998))
        self.advance(6.5)
        self.response = httpx.Response(200, json=self.envelope(age=8.5))
        with self.assertRaises(TradingError):
            self.client.sample("oi", SYMBOL, 8)
        self.assertEqual(self.client._cache[("oi", SYMBOL)].started, 98)

    def test_disconnected_cache_refreshes_before_normal_max_age_for_cycle_recovery(self):
        self.assertTrue(self.client._accept(self.envelope(age=1.1)))
        self.client._connected = False
        self.response = httpx.Response(200, json=self.envelope(age=0.05, sequence=2))
        started, checked, _ = self.client.sample("oi", SYMBOL, 8)
        self.assertAlmostEqual(self.ticks - started, 0.05)
        self.assertAlmostEqual(self.wall - checked, 0.05)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.client.sample("oi", SYMBOL, 1)[:2], (started, checked))
        self.assertEqual(len(self.requests), 1)

    def test_failed_refresh_and_rate_limit_keep_acceptable_age_without_renewing(self):
        self.assertTrue(self.client._accept(self.envelope(age=2)))
        original = (98, 998)
        self.assertEqual(self.client.sample("oi", SYMBOL, 8)[:2], original)
        self.advance(0.1)
        self.assertEqual(self.client.sample("oi", SYMBOL, 8)[:2], original)
        self.assertEqual(len(self.requests), 1)
        with self.assertRaises(TradingError):
            self.client.sample("oi", SYMBOL, 1)

    def test_http_round_trip_is_charged_to_age(self):
        envelope = self.envelope(age=0.1)
        def delayed(request):
            self.advance(0.7)
            return httpx.Response(200, json=envelope)
        self.client._http = httpx.Client(transport=httpx.MockTransport(delayed))
        started, checked, _ = self.client.sample("oi", SYMBOL, 1)
        self.assertAlmostEqual(self.ticks - started, 0.8)
        self.assertAlmostEqual(self.wall - checked, 0.8)

    def test_future_clock_allowance_can_only_increase_age(self):
        envelope = self.envelope(age=0.1, sampled_at=self.wall + 0.4, published_at=self.wall + 0.5)
        self.assertTrue(self.client._accept(envelope))
        started, checked, _ = self.client.sample("oi", SYMBOL, 8)
        self.assertAlmostEqual(self.ticks - started, 0.6)
        self.assertAlmostEqual(self.wall - checked, 0.6)
        self.assertFalse(self.client._accept(self.envelope(sequence=2, published_at=self.wall + 2)))

    def test_malformed_or_inconsistent_envelopes_cannot_update_cache(self):
        for changes in ({"version": True}, {"epoch": "bad"}, {"sequence": True},
                        {"sequence": 0}, {"sequence": -1}, {"age_ms": float("nan")},
                        {"age_ms": -1}, {"sampled_at": float("inf")}, {"payload": None},
                        {"sampled_at": self.wall - 10}, {"kind": "orders"}, {"symbol": "XAU/USDT"}):
            with self.subTest(changes=changes):
                self.assertFalse(self.client._accept(self.envelope(**changes)))
        self.assertEqual(self.client.status()["cached_samples"], 0)

    def test_maximum_ages_and_local_clock_changes_remain_fail_closed(self):
        self.assertTrue(self.client._accept(self.envelope(age=7)))
        self.advance(2)
        with self.assertRaises(TradingError):
            self.client.sample("oi", SYMBOL, 100)
        self.assertTrue(self.client._accept(self.envelope(age=299, kind="brackets")))
        self.advance(2)
        with self.assertRaises(TradingError):
            self.client.sample("brackets", SYMBOL, 1000)
        self.assertTrue(self.client._accept(self.envelope(sequence=2)))
        self.wall -= 100
        self.ticks += 9
        with self.assertRaises(TradingError):
            self.client.sample("oi", SYMBOL, 8)

    def test_http_response_for_wrong_key_and_clock_jump_are_rejected(self):
        self.response = httpx.Response(200, json=self.envelope(symbol="CLUSD1"))
        with self.assertRaises(TradingError):
            self.client.sample("oi", SYMBOL, 8)
        self.assertEqual(self.client.status()["cached_samples"], 0)
        def jump(request):
            self.wall -= 2
            return httpx.Response(200, json=self.envelope())
        self.advance(1)
        self.client._http = httpx.Client(transport=httpx.MockTransport(jump))
        with self.assertRaises(TradingError):
            self.client.sample("oi", SYMBOL, 8)

    def test_http_fallback_is_rate_limited_after_failures(self):
        for _ in range(5):
            with self.assertRaises(TradingError):
                self.client.sample("oi", SYMBOL, 8)
        self.assertEqual(len(self.requests), 1)
        self.advance(0.21)
        with self.assertRaises(TradingError):
            self.client.sample("oi", SYMBOL, 8)
        self.assertEqual(len(self.requests), 2)

    def test_concurrent_readers_share_one_fallback_and_keep_individual_age_limits(self):
        entered, release, joined = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def blocked(request):
            calls.append(request)
            entered.set()
            self.assertTrue(release.wait(2))
            return httpx.Response(200, json=self.envelope(age=2))
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
                follower = pool.submit(self.client.sample, "oi", SYMBOL, 1)
                self.assertTrue(joined.wait(2))
                release.set()
                self.assertEqual(owner.result(timeout=2)[:2], (98, 998))
                with self.assertRaises(TradingError):
                    follower.result(timeout=2)
        self.assertEqual(len(calls), 1)

    def test_update_listener_runs_without_lock_and_only_for_new_samples(self):
        updates = []
        def listener(symbol, kind):
            updates.append((symbol, kind, self.client.status()["cached_samples"]))
        self.client.set_update_listener(listener)
        self.assertTrue(self.client._accept(self.envelope()))
        self.assertFalse(self.client._accept(self.envelope()))
        self.assertEqual(updates, [(SYMBOL, "oi", 1)])
        self.client.set_update_listener(lambda *args: (_ for _ in ()).throw(RuntimeError(TOKEN)))
        self.assertTrue(self.client._accept(self.envelope(sequence=2)))
        self.assertNotIn(TOKEN, repr(self.client.status()))

    def test_close_is_idempotent_and_prevents_reads_or_restart(self):
        self.client._accept(self.envelope())
        self.client.close()
        self.client.close()
        self.client.start()
        self.assertTrue(self.client.status()["closed"])
        self.assertFalse(self.client.status()["running"])
        self.assertTrue(self.client._http.is_closed)
        self.assertFalse(self.client._accept(self.envelope(sequence=2)))
        with self.assertRaises(TradingError):
            self.client.sample("oi", SYMBOL, 8)

    def test_close_releases_waiting_readers_and_late_http_cannot_repopulate(self):
        entered, release = threading.Event(), threading.Event()
        def blocked(request):
            entered.set()
            self.assertTrue(release.wait(2))
            return httpx.Response(200, json=self.envelope())
        self.client._http = httpx.Client(transport=httpx.MockTransport(blocked))
        with ThreadPoolExecutor(max_workers=1) as pool:
            owner = pool.submit(self.client.sample, "oi", SYMBOL, 8)
            self.assertTrue(entered.wait(2))
            pending = self.client._inflight[("oi", SYMBOL)]
            self.client.close()
            with self.assertRaises(TradingError):
                pending.result(timeout=0.1)
            release.set()
            with self.assertRaises(TradingError):
                owner.result(timeout=2)
        self.assertEqual(self.client.status()["cached_samples"], 0)

    def test_websocket_uses_verified_tls_auth_headers_and_receiver_stops(self):
        messages = queue.Queue()
        entered, notified = threading.Event(), threading.Event()
        options = []
        class Connection:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def recv(self, timeout):
                try:
                    return messages.get(timeout=timeout)
                except queue.Empty:
                    raise TimeoutError from None
        def connect(url, **kwargs):
            options.append((url, kwargs))
            entered.set()
            return Connection()
        self.client._connect = connect
        self.client.set_update_listener(lambda symbol, kind: notified.set())
        messages.put(json.dumps(self.envelope()))
        self.client.start()
        self.client.start()
        self.assertTrue(entered.wait(2))
        self.assertTrue(notified.wait(2))
        self.assertEqual(len(options), 1)
        url, kwargs = options[0]
        self.assertEqual(url, "wss://relay.invalid:9443/v1/stream")
        self.assertEqual(kwargs["additional_headers"], {"Authorization": "Bearer " + TOKEN})
        self.assertIs(kwargs["ssl"], self.client._tls)
        self.assertTrue(kwargs["ssl"].check_hostname)
        self.assertEqual(kwargs["ssl"].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(kwargs["logger"].disabled)
        self.assertIsNone(kwargs["proxy"])
        self.client.close()
        self.assertFalse(self.client._thread.is_alive())
        self.assertFalse(self.client.status()["connected"])


if __name__ == "__main__":
    unittest.main()
