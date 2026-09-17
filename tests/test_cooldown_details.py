"""Cooldown diagnostics through mocked exchange responses; no live requests."""
import unittest
from unittest.mock import patch

import httpx

from trading.exchange import API, AmbiguousOrder, ExchangeError, MarketData, RateBudget, RequestNotSent


class CooldownDetailsTests(unittest.TestCase):
    def api(self, response):
        seen = []
        api = API(budget=RateBudget(), transport=httpx.MockTransport(
            lambda request: seen.append(request) or response))
        self.addCleanup(api.close)
        api.signed_parameters = lambda params: {**params, "signature": "private-signature"}
        return api, seen

    @patch("trading.exchange.time.monotonic", return_value=1000)
    def test_gateway_reason_survives_later_local_rejections(self, clock):
        for status, delay, reason in ((403, 30, "访问被拒绝"), (429, 180, "请求频率超限"),
                                      (418, 86400, "IP 被临时封禁")):
            with self.subTest(status=status):
                clock.return_value = 1000
                api, seen = self.api(httpx.Response(status, text="private-upstream-response"))
                with self.assertRaises(ExchangeError) as first:
                    api.call("GET", "/fapi/v3/account", {"user": "private-account"}, signed=True)
                for fragment in (f"HTTP {status}", reason, "GET /fapi/v3/account", f"剩余 {delay} 秒"):
                    self.assertIn(fragment, str(first.exception))
                clock.return_value = 1005.2
                with self.assertRaises(RequestNotSent) as later:
                    api.call("GET", "/fapi/v3/time")
                self.assertIn("GET /fapi/v3/account", str(later.exception))
                self.assertIn(f"剩余 {delay - 5} 秒", str(later.exception))
                self.assertIn(reason, str(later.exception))
                self.assertNotIn("private-", str(first.exception) + str(later.exception))
                self.assertEqual(len(seen), 1)
                self.assertAlmostEqual(later.exception.retry_after, delay - 5.2)

    @patch("trading.exchange.time.monotonic", return_value=1000)
    def test_exchange_retry_after_is_reflected_in_wait(self, _clock):
        api, _ = self.api(httpx.Response(429, headers={"Retry-After": "600"}))
        with self.assertRaises(ExchangeError) as caught:
            api.call("GET", "/fapi/v3/time")
        self.assertIn("剩余 600 秒", str(caught.exception))
        self.assertEqual(caught.exception.retry_after, 600)

    @patch("trading.exchange.time.monotonic", return_value=1000)
    def test_exchange_codes_distinguish_request_and_order_limits(self, _clock):
        for code, reason in ((-1003, "请求频率或权重超限"), (-1015, "下单频率超限")):
            with self.subTest(code=code):
                api, _ = self.api(httpx.Response(400, json={"code": code, "msg": "private-upstream"}))
                with self.assertRaises(ExchangeError) as caught:
                    api.call("POST", "/fapi/v3/order", signed=True)
                self.assertEqual(caught.exception.code, code)
                for fragment in (f"错误码 {code}", reason, "POST /fapi/v3/order", "剩余 180 秒"):
                    self.assertIn(fragment, str(caught.exception))
                self.assertNotIn("private-upstream", str(caught.exception))
                with self.assertRaisesRegex(RequestNotSent, reason):
                    api.call("GET", "/fapi/v3/time")

    def test_batch_preserves_fills_and_both_limit_causes(self):
        rows = [{"status": "FILLED"}, {"code": -1003}, {"code": -1015}]
        api, seen = self.api(httpx.Response(200, json=rows))
        self.assertEqual(api.call("POST", "/fapi/v3/batchOrders", signed=True), rows)
        with self.assertRaises(RequestNotSent) as caught:
            api.call("GET", "/fapi/v3/order")
        for fragment in ("-1003", "-1015", "POST /fapi/v3/batchOrders"):
            self.assertIn(fragment, str(caught.exception))
        self.assertEqual(len(seen), 1)

    @patch("trading.exchange.time.monotonic", return_value=1000)
    def test_shorter_blocks_cannot_replace_longer_cause_and_expiry_allows_requests(self, clock):
        budget = RateBudget()
        budget.block(600, reason="长冷却原因")
        budget.block(30, reason="短冷却原因")
        with self.assertRaisesRegex(RequestNotSent, "长冷却原因"):
            budget.reserve(1)
        clock.return_value = 1600
        budget.reserve(1)
        budget.block(30, reason="新冷却原因")
        with self.assertRaisesRegex(RequestNotSent, "新冷却原因"):
            budget.reserve(1)

    def test_public_capacity_endpoint_shares_its_cause_with_account_requests(self):
        for brackets in (False, True):
            with self.subTest(brackets=brackets):
                api, seen = self.api(httpx.Response(403, text="blocked"))
                market = MarketData(api)
                with self.assertRaises(ExchangeError) as first:
                    market._public_json("/public/capacity", "XAUUSD1", brackets=brackets)
                with self.assertRaises(RequestNotSent) as later:
                    api.call("GET", "/fapi/v3/account")
                for error in (first.exception, later.exception):
                    self.assertIn("公共额度接口", str(error))
                    self.assertIn("HTTP 403", str(error))
                    self.assertIn(f"{'POST' if brackets else 'GET'} /public/capacity", str(error))
                self.assertEqual(len(seen), 1)

    def test_403_diagnostics_preserve_ambiguous_write_classification(self):
        for body in ({"code": -1007}, None):
            with self.subTest(body=body):
                response = httpx.Response(403, json=body) if body else httpx.Response(403, text="blocked")
                api, _ = self.api(response)
                with self.assertRaises(AmbiguousOrder) as caught:
                    api.call("POST", "/fapi/v3/order", signed=True)
                self.assertIn("HTTP 403", str(caught.exception))
                self.assertIn("POST /fapi/v3/order", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
