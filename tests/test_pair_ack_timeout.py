"""Request-scoped acknowledgement timeouts, using offline HTTP transports."""
import unittest
from unittest.mock import patch

import httpx

from tests import test_exchange as fixtures
from tests import test_pair_warm_mode_recovery as pair_fixtures
from trading.exchange import API, AmbiguousOrder, ExchangeError, LiveBroker, RateBudget
from trading.paper import DemoMarket
from trading.models import dec


class PairAcknowledgementTimeoutTests(unittest.TestCase):
    setUp = fixtures.ExchangeTests.setUp

    def api(self, handler):
        api = API(self.credentials, transport=httpx.MockTransport(handler), budget=RateBudget())
        self.addCleanup(api.close)
        return api

    def test_explicit_timeout_is_request_scoped_and_other_calls_keep_defaults(self):
        requests = []
        api = self.api(lambda request: requests.append(request) or httpx.Response(200, json={}))
        defaults = api.http.timeout.as_dict()
        with patch.object(api.http, "request", wraps=api.http.request) as send:
            api.call("POST", "/fapi/v3/order", {"symbol": "XAUUSD1"}, signed=True, timeout=3)
            api.call("GET", "/fapi/v3/order", {"symbol": "XAUUSD1"}, signed=True)
            api.call("POST", "/fapi/v3/leverage", {"symbol": "XAUUSD1", "leverage": 5}, signed=True)
        self.assertEqual(requests[0].extensions["timeout"], dict.fromkeys(defaults, 3))
        self.assertEqual(requests[1].extensions["timeout"], defaults)
        self.assertEqual(requests[2].extensions["timeout"], defaults)
        self.assertEqual(api.http.timeout.as_dict(), defaults)
        self.assertEqual(send.call_args_list[0].kwargs["timeout"], 3)
        self.assertTrue(all("timeout" not in call.kwargs for call in send.call_args_list[1:]))

    def test_write_timeout_is_ambiguous_without_retry_and_revokes_prior_reads(self):
        requests = []

        def timeout(request):
            requests.append(request)
            raise httpx.ReadTimeout("offline timeout", request=request)

        api = self.api(timeout)
        broker = LiveBroker(self.credentials, DemoMarket(), api=api)
        generation = broker._snapshot_generation
        with self.assertRaises(AmbiguousOrder):
            broker.submit([{"symbol": "XAUUSD1", "newClientOrderId": "original-client-id"}], timeout=3)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].extensions["timeout"]["read"], 3)
        self.assertEqual(broker._snapshot_generation, generation + 2)

    def test_get_timeout_remains_a_read_failure(self):
        def timeout(request):
            raise httpx.ReadTimeout("offline timeout", request=request)

        api = self.api(timeout)
        with self.assertRaises(ExchangeError) as caught:
            api.call("GET", "/fapi/v3/order", signed=True)
        self.assertNotIsInstance(caught.exception, AmbiguousOrder)

    def test_single_and_batch_submissions_forward_timeout_without_changing_following_calls(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json=[] if request.url.path.endswith("batchOrders") else {})

        api = self.api(handle)
        broker = LiveBroker(self.credentials, DemoMarket(), api=api)
        order = {"symbol": "XAUUSD1", "newClientOrderId": "first"}
        broker.submit([order], timeout=3)
        broker.submit([order, {**order, "newClientOrderId": "second"}], timeout=3)
        broker.submit([order])
        broker.query("XAUUSD1", "first")
        broker.cancel("XAUUSD1", "first")
        self.assertEqual([request.url.path for request in requests],
                         ["/fapi/v3/order", "/fapi/v3/batchOrders", "/fapi/v3/order",
                          "/fapi/v3/order", "/fapi/v3/order"])
        self.assertEqual([request.extensions["timeout"]["read"] for request in requests], [3, 3, 8, 8, 8])

    def test_default_submissions_keep_existing_api_call_signature(self):
        calls = []

        class ExistingAPI:
            def call(self, method, path, params=None, signed=False, weight=1):
                calls.append((method, path, params, signed, weight))
                return [] if path.endswith("batchOrders") else {}

        broker = LiveBroker(self.credentials, DemoMarket(), api=ExistingAPI())
        orders = [{"symbol": "XAUUSD1", "newClientOrderId": "first"},
                  {"symbol": "XAUUSD1", "newClientOrderId": "second"}]
        broker.submit(orders[:1])
        broker.submit(orders)
        broker.query("XAUUSD1", "first")
        broker.cancel("XAUUSD1", "first")
        self.assertEqual(len(calls), 4)
        self.assertEqual([call[4] for call in calls], [1, 5, 1, 1])


class PairAcknowledgementFlowTests(unittest.TestCase):
    setUp = pair_fixtures.PairWarmModeRecoveryTests.setUp
    live_brokers = pair_fixtures.PairWarmModeRecoveryTests.live_brokers
    warm = pair_fixtures.PairWarmModeRecoveryTests.warm
    clear_calls = pair_fixtures.PairWarmModeRecoveryTests.clear_calls
    ordinary_plan = pair_fixtures.PairWarmModeRecoveryTests.ordinary_plan

    def run_flow(self, reject_short=False):
        state, snapshots, guards, plan = self.ordinary_plan()
        events = []
        long_api = self.brokers["long"].api
        call = long_api.call

        def lose_response(method, path, params=None, **options):
            events.append((method, path, dict(params or {}), options.get("timeout")))
            result = call(method, path, params, **options)
            if method == "POST" and path == "/fapi/v3/order":
                raise AmbiguousOrder("read timeout after exchange accepted")
            return result

        with patch.object(long_api, "call", side_effect=lose_response):
            if reject_short:
                with patch.object(self.brokers["short"], "submit", side_effect=ExchangeError("notional cap", code=-2029)):
                    self.trader._start(self.pair, state, self.brokers, snapshots, guards, plan, kind="ordinary")
                self.assertIsNotNone(state["pending"])
                self.assertEqual(len(state["pending"]["repairs"]), 1)
                self.trader._recover(self.pair, state, self.brokers)
            else:
                self.trader._start(self.pair, state, self.brokers, snapshots, guards, plan, kind="ordinary")
        self.assertIsNone(state["pending"])
        posts = [event for event in events if event[0] == "POST"]
        self.assertEqual(len(posts), 2 if reject_short else 1)
        self.assertTrue(all(event[3] == 3 for event in posts))
        queries = [event for event in events if event[0] == "GET" and event[1] == "/fapi/v3/order"]
        self.assertEqual([event[2]["origClientOrderId"] for event in queries],
                         [event[2]["newClientOrderId"] for event in posts])
        self.assertTrue(all(event[3] is None for event in queries))
        self.assertTrue(any(event[1].endswith("accountWithJoinMargin") for event in events))
        self.assertTrue(all(dec(qty) == (0 if reject_short else plan.qty) for qty in state["owned"].values()))
        if reject_short:
            self.assertEqual([event[2]["side"] for event in posts], ["BUY", "SELL"])
            self.assertEqual(posts[0][2]["quantity"], posts[1][2]["quantity"])
        return state

    def test_timeout_then_both_fills_queries_original_ids_without_resubmission(self):
        self.assertTrue(self.run_flow()["last_batch"]["completed"])

    def test_timeout_then_single_fill_only_reduces_that_original_increment_once(self):
        self.assertFalse(self.run_flow(reject_short=True)["last_batch"]["completed"])
