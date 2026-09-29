import asyncio
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import format_datetime
from io import StringIO
import json
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
import httpx
from starlette.websockets import WebSocketDisconnect

import monitor
from trading.capacity_relay_server import (MAX_CONNECTIONS, MAX_REQUESTS_PER_MINUTE, RelayCollector,
    RelayConfig, RelayConfigError, Sample, Subscriber, create_app, main, validate_payload)


TOKEN = "relay_test_token_" + "a" * 32
SYMBOL = "XAUUSD1"
HEADERS = {"Authorization": "Bearer " + TOKEN}


def payload(kind, symbol=SYMBOL):
    data = ({"symbol": symbol, "leverageOiRemainingMap": {"5": "50000.00000000000000000001", "20": "2000"}}
            if kind == "oi" else {"brackets": [{"symbol": symbol, "riskBrackets": [
                {"minOpenPosLeverage": 1, "maxOpenPosLeverage": 125, "bracketNotionalCap": "100000"}]}]})
    return {"success": True, "code": "000000", "data": data, "extra": {"untouched": [1, "ok"]}}


class Clock:
    def __init__(self):
        self.mono = 1000.0
        self.utc = 1750000000.0

    def advance(self, seconds):
        self.mono += seconds
        self.utc += seconds


class RelayConfigTests(unittest.TestCase):
    def test_defaults_extensions_and_numeric_settings_are_validated_without_secrets(self):
        config = RelayConfig.from_env({"ASTER_RELAY_TOKEN": TOKEN})
        self.assertEqual((config.host, config.port, config.symbols), ("0.0.0.0", 8766, (SYMBOL,)))
        self.assertNotIn(TOKEN, repr(config))
        extended = RelayConfig.from_env({"ASTER_RELAY_TOKEN": TOKEN, "ASTER_RELAY_SYMBOLS": "XAUUSD1, BTCUSD1"})
        self.assertEqual(extended.symbols, (SYMBOL, "BTCUSD1"))
        for kwargs in ({"token": "short"}, {"token": "a" * 32 + " "}, {"host": "https://elsewhere"},
                       {"port": True}, {"port": 0}, {"symbols": ()}, {"symbols": ("BTCUSDT",)},
                       {"symbols": (SYMBOL, SYMBOL)}, {"symbols": tuple(f"A{i}USD1" for i in range(17))},
                       {"interval": 0.1}, {"interval": float("nan")}, {"brackets_interval": 0},
                       {"cert_file": "cert-only"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(RelayConfigError):
                RelayConfig(**{**{"token": TOKEN}, **kwargs})

    def test_multiple_symbols_reserve_bracket_requests_in_global_rate(self):
        config = RelayConfig(TOKEN, symbols=tuple(f"A{i}USD1" for i in range(16)))
        total_rate = len(config.symbols) / config.effective_interval + len(config.symbols) / config.brackets_interval
        self.assertLessEqual(total_rate, 15)
        self.assertGreater(config.effective_interval, 1)

    def test_check_config_reads_tls_locally_without_network_or_starting_server(self):
        with tempfile.TemporaryDirectory() as directory:
            certificate, key = Path(directory) / "cert.pem", Path(directory) / "key.pem"
            certificate.write_text("test certificate")
            key.write_text("test key")
            env = {"ASTER_RELAY_TOKEN": TOKEN, "ASTER_RELAY_CERT_FILE": str(certificate), "ASTER_RELAY_KEY_FILE": str(key)}
            context = Mock()
            output = StringIO()
            with patch.dict("os.environ", env, clear=True), patch("ssl.SSLContext", return_value=context), \
                    patch("httpx.AsyncClient") as http, patch("uvicorn.run") as serve, redirect_stdout(output):
                main(["--check-config"])
            context.load_cert_chain.assert_called_once_with(str(certificate), str(key))
            http.assert_not_called()
            serve.assert_not_called()
            self.assertEqual(json.loads(output.getvalue())["status"], "ok")
            self.assertNotIn(TOKEN, output.getvalue())
        with self.assertRaises(RelayConfigError):
            RelayConfig(TOKEN).require_tls()

    def test_risk_payload_rejects_ambiguous_symbol_overlap_and_invalid_numbers(self):
        variants = []
        wrong = payload("brackets", "OTHERUSD1")
        variants.append(wrong)
        duplicated = payload("brackets")
        duplicated["data"]["brackets"].append(deepcopy(duplicated["data"]["brackets"][0]))
        variants.append(duplicated)
        for field, value in (("minOpenPosLeverage", "1.5"), ("maxOpenPosLeverage", 126),
                             ("bracketNotionalCap", "NaN"), ("bracketNotionalCap", "0")):
            wrong = payload("brackets")
            wrong["data"]["brackets"][0]["riskBrackets"][0][field] = value
            variants.append(wrong)
        overlap = payload("brackets")
        overlap["data"]["brackets"][0]["riskBrackets"].append(
            {"minOpenPosLeverage": 5, "maxOpenPosLeverage": 10, "bracketNotionalCap": "100"})
        variants.append(overlap)
        for wrong in variants:
            with self.subTest(wrong=wrong), self.assertRaises(monitor.MonitorError):
                validate_payload("brackets", SYMBOL, wrong)


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = Clock()
        self.calls = []
        self.reply = None

        async def respond(request):
            self.calls.append((self.clock.mono, request))
            if callable(self.reply):
                result = self.reply(request)
                return await result if asyncio.iscoroutine(result) else result
            if self.reply is not None:
                return self.reply
            kind = "oi" if request.method == "GET" else "brackets"
            symbol = request.url.params.get("symbol") if kind == "oi" else json.loads(request.content)["symbol"]
            return httpx.Response(200, json=payload(kind, symbol))

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        self.collector = RelayCollector(RelayConfig(TOKEN), http=self.http,
            clock=lambda: self.clock.utc, monotonic=lambda: self.clock.mono)

    async def asyncTearDown(self):
        await self.collector.stop()
        await self.http.aclose()

    async def test_sampling_uses_only_fixed_unsigned_paths_and_preserves_payload(self):
        self.assertTrue(await self.collector.sample_once("oi", SYMBOL))
        self.clock.advance(0.1)
        self.assertTrue(await self.collector.sample_once("brackets", SYMBOL))
        requests = [request for _, request in self.calls]
        self.assertEqual(requests[0].method, "GET")
        self.assertEqual(requests[0].url.path, monitor.OI_PATH)
        self.assertEqual(dict(requests[0].url.params), {"symbol": SYMBOL})
        self.assertEqual(requests[1].method, "POST")
        self.assertEqual(requests[1].url.path, monitor.BRACKETS_PATH)
        self.assertEqual(json.loads(requests[1].content), {"symbol": SYMBOL})
        self.assertTrue(all(request.url.host == "www.asterdex.com" for request in requests))
        self.assertTrue(all("authorization" not in request.headers for request in requests))
        self.assertEqual(self.collector.snapshot("oi", SYMBOL)["payload"], payload("oi"))
        self.assertEqual(self.collector.snapshot("brackets", SYMBOL)["sequence"], 2)

    async def test_decimal_json_literals_never_round_through_float(self):
        raw = json.dumps(payload("oi")).replace('"50000.00000000000000000001"', '50000.00000000000000000001')
        self.reply = httpx.Response(200, content=raw)
        self.assertTrue(await self.collector.sample_once("oi", SYMBOL))
        value = self.collector.snapshot("oi", SYMBOL)["payload"]["data"]["leverageOiRemainingMap"]["5"]
        self.assertEqual(value, "50000.00000000000000000001")

    async def test_sample_time_includes_network_age_and_reads_never_renew_it(self):
        def response(request):
            self.clock.advance(0.5)
            return httpx.Response(200, json=payload("oi"))
        self.reply = response
        start = self.clock.utc
        await self.collector.sample_once("oi", SYMBOL)
        first = self.collector.snapshot("oi", SYMBOL)
        self.assertEqual(first["sampled_at"], start)
        self.assertEqual(first["age_ms"], 500)
        self.clock.advance(1)
        second = self.collector.snapshot("oi", SYMBOL)
        self.assertEqual(second["sequence"], first["sequence"])
        self.assertEqual(second["sampled_at"], first["sampled_at"])
        self.assertEqual(second["published_at"] - first["published_at"], 1)
        self.assertEqual(second["age_ms"], 1500)
        second["payload"]["data"] = {}
        self.assertEqual(self.collector.snapshot("oi", SYMBOL)["payload"], payload("oi"))
        self.assertEqual(len(self.calls), 1)
        self.clock.advance(2)
        self.assertIsNone(self.collector.snapshot("oi", SYMBOL))

    async def test_invalid_and_failed_samples_do_not_replace_or_refresh_good_sample(self):
        await self.collector.sample_once("oi", SYMBOL)
        original = self.collector.samples[("oi", SYMBOL)]
        invalid = [payload("oi", "WRONGUSD1"), {"success": False, "code": "000000", "data": {}},
                   {"success": True, "code": "000000", "data": {"symbol": SYMBOL, "leverageOiRemainingMap": {"5": "NaN"}}},
                   {"success": True, "code": "000000", "data": {"symbol": SYMBOL, "leverageOiRemainingMap": {"0": "1"}}}]
        responses = [httpx.Response(200, json=item) for item in invalid]
        responses += [httpx.Response(500), httpx.Response(200, content="{"),
                      httpx.Response(200, content="NaN"), httpx.Response(200, content=b"a" * 2_000_001)]
        for response in responses:
            self.clock.advance(0.1)
            self.reply = response
            self.assertFalse(await self.collector.sample_once("oi", SYMBOL))
            self.assertIs(self.collector.samples[("oi", SYMBOL)], original)
            self.assertEqual(self.collector.sequence, 1)

    async def test_throttling_is_global_and_retry_after_is_respected(self):
        for status, seconds in ((403, 60), (418, 86400), (429, 600)):
            self.collector.backoff_until = 0
            self.clock.advance(1)
            self.reply = httpx.Response(status, headers={"Retry-After": str(seconds)})
            count = len(self.calls)
            self.assertFalse(await self.collector.sample_once("oi", SYMBOL))
            self.assertGreaterEqual(self.collector.backoff_until - self.clock.mono, seconds)
            self.clock.advance(1)
            self.assertFalse(await self.collector.sample_once("brackets", SYMBOL))
            self.assertEqual(len(self.calls), count + 1)
        self.collector.backoff_until = 0
        self.clock.advance(1)
        retry_at = format_datetime(datetime.fromtimestamp(self.clock.utc + 600, timezone.utc), usegmt=True)
        self.reply = httpx.Response(429, headers={"Retry-After": retry_at})
        await self.collector.sample_once("oi", SYMBOL)
        self.assertAlmostEqual(self.collector.backoff_until - self.clock.mono, 600)

    async def test_global_rolling_budget_includes_brackets_and_minimum_spacing(self):
        self.assertTrue(await self.collector.sample_once("oi", SYMBOL))
        self.assertFalse(await self.collector.sample_once("brackets", SYMBOL))
        self.clock.advance(0.1)
        self.assertTrue(await self.collector.sample_once("brackets", SYMBOL))
        self.clock.advance(61)
        with patch("trading.capacity_relay_server.MIN_REQUEST_SPACING", 0):
            for index in range(MAX_REQUESTS_PER_MINUTE):
                self.assertTrue(await self.collector.sample_once("oi" if index % 2 else "brackets", SYMBOL))
            self.assertFalse(await self.collector.sample_once("oi", SYMBOL))
            self.clock.advance(59.99)
            self.assertFalse(await self.collector.sample_once("brackets", SYMBOL))
            self.clock.advance(0.02)
            self.assertTrue(await self.collector.sample_once("oi", SYMBOL))

    async def test_same_key_requests_never_overlap(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def response(request):
            entered.set()
            await release.wait()
            return httpx.Response(200, json=payload("oi"))
        self.reply = response
        first = asyncio.create_task(self.collector.sample_once("oi", SYMBOL))
        await entered.wait()
        self.clock.advance(1)
        self.assertFalse(await self.collector.sample_once("oi", SYMBOL))
        release.set()
        self.assertTrue(await first)
        self.assertEqual(len(self.calls), 1)

    async def test_slow_requests_skip_missed_ticks_without_catchup_burst(self):
        self.collector._due[("brackets", SYMBOL)] = self.clock.mono + 60
        def response(request):
            self.clock.advance(1)
            return httpx.Response(200, json=payload("oi"))
        self.reply = response
        await self.collector.tick()
        self.assertAlmostEqual(await self.collector.tick(), 0.2)
        self.assertEqual(len(self.calls), 1)

    async def test_start_stop_is_single_background_task_and_cancels_promptly(self):
        await self.collector.start()
        first = self.collector._task
        await self.collector.start()
        self.assertIs(self.collector._task, first)
        subscriber = self.collector.subscribe()
        await self.collector.stop()
        self.assertTrue(first.done())
        self.assertTrue(subscriber.closed)
        self.assertFalse(self.collector.subscribers)


class RelayAPITests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.http = Mock()
        self.collector = RelayCollector(RelayConfig(TOKEN), http=self.http,
            clock=lambda: self.clock.utc, monotonic=lambda: self.clock.mono)
        self.app = create_app(collector=self.collector, start_collector=False)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def seed(self, kind="oi", sequence=1, age=0):
        sample = Sample(kind, SYMBOL, self.clock.utc - age, self.clock.mono - age, sequence, payload(kind))
        self.collector.samples[(kind, SYMBOL)] = sample
        return sample

    def test_health_is_authenticated_and_independent_of_upstream_readiness(self):
        self.assertEqual(self.client.get("/health").status_code, 401)
        self.assertEqual(self.client.get("/health", headers=HEADERS).json(), {"status": "ok"})
        self.assertEqual(self.client.get("/health?token=" + TOKEN, headers=HEADERS).status_code, 401)
        self.assertEqual(self.client.get("/health", headers={"Authorization": "Bearer wrong"}).status_code, 401)
        self.http.stream.assert_not_called()

    def test_snapshot_only_reads_cache_rejects_unconfigured_symbols_and_expires(self):
        url = "/v1/snapshot?symbol=" + SYMBOL + "&kind=oi"
        self.assertEqual(self.client.get(url, headers=HEADERS).status_code, 503)
        self.assertEqual(self.client.get("/v1/snapshot?symbol=BTCUSD1&kind=oi", headers=HEADERS).status_code, 400)
        self.assertEqual(self.client.get("/v1/snapshot?symbol=" + SYMBOL + "&kind=orders", headers=HEADERS).status_code, 400)
        self.seed()
        first = self.client.get(url, headers=HEADERS)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.headers["cache-control"], "no-store")
        self.clock.advance(0.5)
        second = self.client.get(url, headers=HEADERS).json()
        self.assertEqual(second["sequence"], first.json()["sequence"])
        self.assertEqual(second["sampled_at"], first.json()["sampled_at"])
        self.assertEqual(second["age_ms"], 500)
        self.clock.advance(3)
        self.assertEqual(self.client.get(url, headers=HEADERS).status_code, 503)
        self.http.stream.assert_not_called()

    def test_status_exposes_readiness_age_backoff_and_connections_without_token(self):
        self.seed()
        self.collector.backoff_until = self.clock.mono + 10
        response = self.client.get("/v1/status", headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(TOKEN, response.text)
        self.assertEqual(response.json()["configured_symbols"], [SYMBOL])
        self.assertEqual(response.json()["backoff_seconds"], 10)
        self.assertEqual(response.json()["connections"], 0)
        ready = {row["kind"]: row["ready"] for row in response.json()["samples"]}
        self.assertEqual(ready, {"oi": True, "brackets": False})

    def test_websocket_authentication_no_query_tokens_and_no_subscribe_messages(self):
        for url, headers in (("/v1/stream", {}), ("/v1/stream?token=" + TOKEN, HEADERS),
                             ("/v1/stream?symbol=" + SYMBOL, HEADERS)):
            with self.subTest(url=url), self.assertRaises(WebSocketDisconnect) as caught:
                with self.client.websocket_connect(url, headers=headers):
                    pass
            self.assertEqual(caught.exception.code, 1008)
        with self.client.websocket_connect("/v1/stream", headers=HEADERS) as websocket:
            websocket.send_json({"subscribe": [SYMBOL]})
            with self.assertRaises(WebSocketDisconnect) as caught:
                websocket.receive_json()
            self.assertEqual(caught.exception.code, 1008)

    def test_websocket_sends_valid_initial_cache_and_success_updates_without_sampling(self):
        self.seed("oi", sequence=2)
        self.seed("brackets", sequence=1)
        with self.client.websocket_connect("/v1/stream", headers=HEADERS) as websocket:
            first, second = websocket.receive_json(), websocket.receive_json()
            self.assertEqual((first["kind"], second["kind"]), ("brackets", "oi"))
            self.assertEqual(first["epoch"], second["epoch"])
            self.assertEqual(self.collector.status()["connections"], 1)
            updated = self.seed("oi", sequence=3)
            async def publish():
                for subscriber in self.collector.subscribers:
                    subscriber.publish(updated)
            self.client.portal.call(publish)
            self.assertEqual(websocket.receive_json()["sequence"], 3)
        self.assertEqual(self.collector.status()["connections"], 0)
        self.http.stream.assert_not_called()

    def test_websocket_connection_limit_does_not_create_unbounded_subscribers(self):
        with patch("trading.capacity_relay_server.MAX_CONNECTIONS", 1):
            with self.client.websocket_connect("/v1/stream", headers=HEADERS):
                with self.assertRaises(WebSocketDisconnect) as caught:
                    with self.client.websocket_connect("/v1/stream", headers=HEADERS):
                        pass
                self.assertEqual(caught.exception.code, 1013)
                self.assertEqual(len(self.collector.subscribers), 1)


class SubscriberTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_consumer_coalesces_to_latest_per_key_and_preserves_sequence_order(self):
        subscriber = Subscriber()
        for index in range(1000):
            subscriber.publish(Sample("oi", SYMBOL, 1, 1, index, payload("oi")))
        subscriber.publish(Sample("brackets", SYMBOL, 1, 1, 1000, payload("brackets")))
        subscriber.publish(Sample("oi", SYMBOL, 1, 1, 1001, payload("oi")))
        self.assertEqual(len(subscriber.pending), 2)
        self.assertEqual((await subscriber.next()).sequence, 1000)
        self.assertEqual((await subscriber.next()).sequence, 1001)
        waiter = asyncio.create_task(subscriber.next())
        await asyncio.sleep(0)
        subscriber.close()
        self.assertIsNone(await waiter)


if __name__ == "__main__":
    unittest.main()
