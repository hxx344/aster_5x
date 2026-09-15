"""Observe actual transport boundaries without credentials or external requests."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx

from trading.cycle_execution import _GuardedCycleBroker
from trading.cycle_quality import ObservedBroker
from trading.exchange import API, BudgetWait, LiveBroker, RequestNotSent
from trading.models import TradingError


class RequestTimingTests(unittest.TestCase):
    def setUp(self):
        self.clock = 100.0
        def reserve(*args, **kwargs):
            self.clock += .003
            return 1
        self.budget = SimpleNamespace(reserve=Mock(side_effect=reserve), observe=Mock(), finish=Mock())
        self.requests = []
        def respond(request):
            self.requests.append(request)
            self.clock += .050
            return httpx.Response(200, json=[{"orderId": 1}, {"orderId": 2}])
        self.api = API(transport=httpx.MockTransport(respond), budget=self.budget)
        self.addCleanup(self.api.close)
        def sign(params):
            self.clock += .004
            return params
        self.api.signed_parameters = Mock(side_effect=sign)
        broker = LiveBroker({}, None, self.api)
        def guard():
            self.clock += .002
        self.guard = Mock(side_effect=guard)
        self.quality = {"timing": {}}
        self.observed = ObservedBroker(_GuardedCycleBroker(broker, self.guard), self.quality)
        self.orders = [{"symbol": "XAUUSD1"}, {"symbol": "XAUUSD1"}]

    def test_guard_budget_signing_and_http_are_separate_local_call_observations(self):
        with patch("trading.request_timing.perf_counter", side_effect=lambda: self.clock):
            self.assertEqual(self.observed.submit(self.orders), [{"orderId": 1}, {"orderId": 2}])
        data = self.quality["timing"]["transport"]
        for key, expected in {"guard_ms": 2, "budget_ms": 3, "signing_ms": 4,
                              "before_http_ms": 9, "http_ms": 50, "response_decode_ms": 0}.items():
            self.assertAlmostEqual(data[key], expected)
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(self.observed.send_attempted)
        self.assertGreaterEqual(data["http_finished_at"], data["http_started_at"])

    def test_failed_guard_and_budget_keep_http_unknown_and_remain_not_sent(self):
        self.guard.side_effect = TradingError("revoked")
        with self.assertRaises(RequestNotSent):
            self.observed.submit(self.orders)
        self.assertFalse(self.observed.send_attempted)
        self.assertEqual(set(self.quality["timing"]["transport"]), {"guard_ms"})
        self.budget.reserve.assert_not_called()
        self.guard.side_effect = None
        self.budget.reserve.side_effect = BudgetWait("quota")
        with self.assertRaises(BudgetWait):
            self.observed.submit(self.orders)
        self.assertFalse(self.observed.send_attempted)
        self.assertNotIn("http_ms", self.quality["timing"]["transport"])
        self.assertEqual(self.requests, [])

    def test_clock_failure_cannot_change_send_or_response(self):
        with patch("trading.request_timing.perf_counter", side_effect=RuntimeError("clock")):
            self.assertEqual(self.observed.submit(self.orders), [{"orderId": 1}, {"orderId": 2}])
        self.assertTrue(self.observed.send_attempted)
        self.assertIsNone(self.quality["timing"]["transport"]["http_ms"])
