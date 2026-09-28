"""Refund only provably unsent preparation, with no real keys or HTTP calls."""
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
import threading
import unittest
from unittest.mock import patch

import httpx

from trading.exchange import API, AmbiguousOrder, RateBudget
from trading.models import TradingError


class UnsentRequestBudgetTests(unittest.TestCase):
    def api(self, budget, handler=None):
        api = API(budget=budget, transport=httpx.MockTransport(
            handler or (lambda request: httpx.Response(200, json={}))))
        self.addCleanup(api.close)
        return api

    def test_missing_credentials_and_signing_failures_do_not_spend_budget(self):
        for failure in (None, ValueError("local signing failed"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                budget = RateBudget()
                requests = []
                api = self.api(budget, lambda request: requests.append(request))
                if failure is not None:
                    api.signed_parameters = lambda params: self.raise_error(failure)
                with self.assertRaises(type(failure) if failure is not None else TradingError):
                    api.call("POST", "/fapi/v3/order", signed=True, weight=5)
                self.assertEqual(requests, [])
                self.assertEqual((budget.weight, budget.local_weight), (0, 0))
                self.assertEqual(budget.inflight, {})
                self.assertEqual(list(budget.unreported), [])

    @staticmethod
    def raise_error(error):
        raise error

    def test_form_encoding_failure_does_not_spend_budget(self):
        class InvalidParameter:
            def __str__(self):
                raise ValueError("cannot encode local parameter")

        budget, requests = RateBudget(), []
        api = self.api(budget, lambda request: requests.append(request))
        with self.assertRaisesRegex(ValueError, "encode"):
            api.call("POST", "/fapi/v3/order", {"quantity": InvalidParameter()}, weight=5)
        self.assertEqual(requests, [])
        self.assertEqual((budget.weight, budget.local_weight), (0, 0))
        self.assertEqual(list(budget.unreported), [])

    def test_concurrent_ip_report_preserves_other_requests_and_cooldown(self):
        budget = RateBudget()
        waiting, release = threading.Event(), threading.Event()
        requests = []
        api = self.api(budget, lambda request: requests.append(request))

        def signing(params):
            waiting.set()
            self.assertTrue(release.wait(3))
            raise ValueError("local signing failed")

        api.signed_parameters = signing
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(api.call, "POST", "/fapi/v3/order", signed=True, weight=5)
            try:
                self.assertTrue(waiting.wait(3))
                outstanding = budget.reserve(7, track=True)
                unknown = budget.reserve(3, track=True)
                budget.finish(unknown)
                report = budget.reserve(1, track=True)
                budget.observe({"X-MBX-USED-WEIGHT-1M": "1000"}, ticket=report)
                budget.block(180)
                deadline = budget.until
            finally:
                release.set()
            with self.assertRaisesRegex(ValueError, "signing"):
                future.result()
        self.assertEqual(requests, [])
        self.assertEqual(budget.weight, 1010)
        self.assertEqual(budget.local_weight, 11)
        self.assertEqual(budget.reported_weight, 1000)
        self.assertEqual(budget.inflight, {outstanding: 7})
        self.assertEqual([weight for _, weight in budget.unreported], [3])
        self.assertEqual(budget.until, deadline)

    def test_cancel_preserves_unrelated_concurrent_high_water_estimate(self):
        budget = RateBudget()
        first = budget.reserve(5, track=True)
        second = budget.reserve(5, track=True)
        headers = {"X-MBX-USED-WEIGHT-1M": "10"}
        budget.observe(headers, ticket=first)
        budget.observe(headers, ticket=second)
        self.assertEqual(budget.weight, 15)
        unsent = budget.reserve(5, track=True)
        budget.cancel_unstarted(unsent)
        self.assertEqual((budget.weight, budget.local_weight), (15, 10))
        budget.cancel_unstarted(unsent)
        budget.finish(unsent)
        self.assertEqual((budget.weight, budget.local_weight), (15, 10))
        self.assertEqual(list(budget.unreported), [])

    def test_cancel_after_automatic_minute_change_keeps_other_pending_weight(self):
        with patch("trading.exchange.time.monotonic", return_value=100):
            budget = RateBudget()
            old = budget.reserve(1, track=True)
            budget.observe({"X-MBX-USED-WEIGHT-1M": "1000"}, ticket=old)
            unsent = budget.reserve(5, track=True)
            other = budget.reserve(7, track=True)
            budget.block(180)
        with patch("trading.exchange.time.monotonic", return_value=160):
            budget.cancel_unstarted(unsent)
            self.assertEqual((budget.weight, budget.local_weight), (7, 7))
            self.assertIsNone(budget.reported_weight)
            self.assertEqual(budget.inflight, {other: 7})
            self.assertEqual(budget.snapshot()["retry_after"], 120)

    def test_cancel_after_observed_minute_change_preserves_new_ip_floor(self):
        epoch = 1800000000
        with patch("trading.exchange.time.monotonic", return_value=100), \
                patch("trading.exchange.time.time", return_value=epoch + 55):
            budget = RateBudget()
            old = budget.reserve(1, track=True)
            budget.observe({"Date": formatdate(epoch + 55, usegmt=True),
                            "X-MBX-USED-WEIGHT-1M": "1000"}, ticket=old)
            unsent = budget.reserve(5, track=True)
            other = budget.reserve(7, track=True)
        with patch("trading.exchange.time.monotonic", return_value=105), \
                patch("trading.exchange.time.time", return_value=epoch + 60):
            report = budget.reserve(1, track=True)
            budget.observe({"Date": formatdate(epoch + 60, usegmt=True),
                            "X-MBX-USED-WEIGHT-1M": "200"}, ticket=report)
            budget.cancel_unstarted(unsent)
            self.assertEqual((budget.weight, budget.local_weight), (207, 8))
            self.assertEqual(budget.reported_weight, 200)
            self.assertEqual(budget.inflight, {other: 7})

    def test_http_failures_and_headerless_responses_keep_the_full_charge(self):
        for failure in (None, httpx.ReadTimeout("unknown delivery"),
                        httpx.ConnectError("connection failed"), RuntimeError("request failed")):
            with self.subTest(failure=type(failure).__name__):
                budget, requests = RateBudget(), []

                def request(req):
                    requests.append(req)
                    if failure is not None:
                        raise failure
                    return httpx.Response(200, json={})

                api = self.api(budget, request)
                api.signed_parameters = lambda params: params
                if failure is None:
                    api.call("POST", "/fapi/v3/order", signed=True, weight=5)
                else:
                    expected = AmbiguousOrder if isinstance(failure, httpx.HTTPError) else RuntimeError
                    with self.assertRaises(expected):
                        api.call("POST", "/fapi/v3/order", signed=True, weight=5)
                self.assertEqual(len(requests), 1)
                self.assertEqual((budget.weight, budget.local_weight), (5, 5))
                self.assertEqual(budget.inflight, {})
                self.assertEqual([weight for _, weight in budget.unreported], [5])


if __name__ == "__main__":
    unittest.main()
