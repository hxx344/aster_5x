"""Local rate admission, recovery headroom and documented request costs; no live calls."""
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from unittest.mock import patch

import httpx

from trading.exchange import API, ExchangeError, LiveBroker, MarketData, RateBudget, RequestNotSent
from trading.paper import DemoMarket


class RequestBudgetTests(unittest.TestCase):
    def test_normal_traffic_leaves_headroom_and_recovery_cannot_exceed_total(self):
        budget = RateBudget()
        budget.reserve(1500)
        with self.assertRaises(RequestNotSent):
            budget.reserve(1)
        state = budget.snapshot()
        self.assertEqual((state["used"], state["limit"], state["ordinary_limit"]), (1500, 1800, 1500))
        self.assertEqual((state["remaining"], state["ordinary_remaining"]), (300, 0))
        with budget.reconciliation():
            budget.require_available(300)
            budget.reserve(300)
            with self.assertRaises(RequestNotSent):
                budget.reserve(1)
        self.assertEqual(budget.weight, 1800)

    def test_recovery_priority_is_nested_exception_safe_and_thread_local(self):
        budget = RateBudget()
        budget.reserve(1500)
        entered, checked = threading.Event(), threading.Event()

        def reconcile():
            with self.assertRaisesRegex(RuntimeError, "test interruption"):
                with budget.reconciliation():
                    with budget.reconciliation():
                        budget.reserve(1)
                    entered.set()
                    self.assertTrue(checked.wait(3))
                    budget.reserve(1)
                    raise RuntimeError("test interruption")
            with self.assertRaises(RequestNotSent):
                budget.reserve(1)

        def ordinary():
            self.assertTrue(entered.wait(3))
            try:
                with self.assertRaises(RequestNotSent):
                    budget.reserve(1)
            finally:
                checked.set()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(reconcile), pool.submit(ordinary)]
            for future in futures:
                future.result()
        self.assertEqual(budget.weight, 1502)

    def test_admission_check_does_not_charge_and_actual_requests_still_recheck(self):
        budget = RateBudget()
        budget.reserve(1300)
        budget.require_available()
        self.assertEqual(budget.weight, 1300)
        budget.reserve(100)
        with self.assertRaises(RequestNotSent) as caught:
            budget.require_available(200)
        self.assertGreater(caught.exception.retry_after, 0)
        self.assertEqual(budget.weight, 1400)

    def test_lower_exchange_limit_keeps_a_proportional_recovery_reserve(self):
        budget = RateBudget()
        budget.limit = 600
        self.assertEqual(budget.snapshot()["ordinary_limit"], 500)
        budget.reserve(500)
        with self.assertRaises(RequestNotSent):
            budget.reserve(1)
        with budget.reconciliation():
            budget.reserve(100)
        self.assertEqual(budget.weight, 600)

    def test_used_weight_header_never_refunds_local_or_out_of_order_requests(self):
        budget = RateBudget()
        budget.reserve(10)
        budget.observe(httpx.Headers({"x-mbx-used-weight-1m": "1400"}))
        budget.reserve(5)
        budget.observe(httpx.Headers({"X-MBX-USED-WEIGHT-1M": "1399"}))
        self.assertEqual(budget.weight, 1405)
        self.assertEqual(budget.snapshot()["ordinary_remaining"], 95)
        with self.assertRaises(RequestNotSent):
            budget.require_available(100)

    def test_invalid_or_unrelated_headers_do_not_poison_budget(self):
        budget = RateBudget()
        budget.reserve(5)
        for reported in ("-1", "NaN", "Infinity", "1.5", "9" * 100, "", None):
            budget.observe({"X-MBX-USED-WEIGHT-1M": reported})
        budget.observe({"X-MBX-ORDER-COUNT-1M": "99999"})
        self.assertEqual(budget.weight, 5)

    def test_window_reset_restores_normal_budget_but_keeps_exchange_backoff(self):
        with patch("trading.exchange.time.monotonic", return_value=100):
            budget = RateBudget()
            budget.observe({"X-MBX-USED-WEIGHT-1M": "1800"})
            budget.block(180)
        with patch("trading.exchange.time.monotonic", return_value=160):
            state = budget.snapshot()
            self.assertEqual(state["used"], 0)
            self.assertEqual(state["retry_after"], 120)
            with budget.reconciliation(), self.assertRaises(RequestNotSent):
                budget.reserve(1)
        with patch("trading.exchange.time.monotonic", return_value=280):
            budget.reserve(1500)
            self.assertEqual(budget.weight, 1500)

    def test_local_rejections_never_reach_http_transport(self):
        for failure in ("exhausted", "backoff", "invalid"):
            with self.subTest(failure=failure):
                seen, budget = [], RateBudget()
                api = API(transport=httpx.MockTransport(lambda req: seen.append(req) or httpx.Response(200, json={})), budget=budget)
                self.addCleanup(api.close)
                if failure == "exhausted":
                    budget.reserve(1500)
                elif failure == "backoff":
                    budget.block(10)
                with self.assertRaises(RequestNotSent):
                    api.call("GET", "/fapi/v3/time", weight=0 if failure == "invalid" else 1)
                self.assertEqual(seen, [])

    def test_all_responses_update_ip_usage_including_invalid_json(self):
        budget = RateBudget()
        api = API(transport=httpx.MockTransport(lambda req: httpx.Response(200, text="invalid", headers={"X-MBX-USED-WEIGHT-1M": "1480"})), budget=budget)
        self.addCleanup(api.close)
        with self.assertRaises(ExchangeError):
            api.call("GET", "/fapi/v3/time")
        self.assertEqual(budget.weight, 1480)

    def test_batch_rate_limit_preserves_other_fills_and_blocks_further_requests(self):
        for code in (-1003, -1015):
            with self.subTest(code=code):
                rows = [{"symbol": "XAUUSD1", "status": "FILLED", "executedQty": "1"}, {"code": code, "msg": "limited"}]
                budget, seen = RateBudget(), []
                api = API(transport=httpx.MockTransport(lambda req: seen.append(req) or httpx.Response(200, json=rows)), budget=budget)
                self.addCleanup(api.close)
                api.signed_parameters = lambda params: params
                self.assertEqual(api.call("POST", "/fapi/v3/batchOrders", signed=True, weight=5), rows)
                self.assertGreater(budget.snapshot()["retry_after"], 179)
                with budget.reconciliation(), self.assertRaises(RequestNotSent):
                    api.call("GET", "/fapi/v3/order")
                self.assertEqual(len(seen), 1)

    def test_query_and_cancel_can_use_reserve_while_new_order_cannot(self):
        budget, seen = RateBudget(), []
        api = API(transport=httpx.MockTransport(lambda req: seen.append(req) or httpx.Response(200, json={})), budget=budget)
        self.addCleanup(api.close)
        api.signed_parameters = lambda params: params
        broker = LiveBroker({}, DemoMarket(), api=api)
        budget.reserve(1500)
        with self.assertRaises(RequestNotSent):
            broker.submit([{"symbol": "XAUUSD1"}])
        broker.query("XAUUSD1", "test-order")
        broker.cancel("XAUUSD1", "test-order")
        self.assertEqual([request.method for request in seen], ["GET", "DELETE"])
        self.assertEqual(budget.weight, 1502)
        with self.assertRaises(RequestNotSent):
            broker.submit([{"symbol": "XAUUSD1"}])

    def test_broker_recovery_context_covers_other_clients_on_shared_budget(self):
        budget = RateBudget()
        api = API(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={})), budget=budget)
        market_api = API(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={})), budget=budget)
        self.addCleanup(api.close)
        self.addCleanup(market_api.close)
        broker = LiveBroker({}, DemoMarket(), api=api)
        budget.reserve(1500)
        with broker.reconciliation_budget():
            market_api.call("GET", "/fapi/v3/ticker/bookTicker")
        self.assertEqual(budget.weight, 1501)
        with self.assertRaises(RequestNotSent):
            market_api.call("GET", "/fapi/v3/ticker/bookTicker")

    def test_single_symbol_book_uses_two_total_weight_and_keeps_freshness_checks(self):
        budget = RateBudget()
        def handle(request):
            row = ({"markPrice": "100"} if request.url.path.endswith("premiumIndex")
                   else {"bidPrice": "100", "askPrice": "100.01", "bidQty": "10", "askQty": "10"})
            return httpx.Response(200, json={"symbol": "XAUUSD1", "time": 100000, **row})
        api = API(transport=httpx.MockTransport(handle), budget=budget)
        self.addCleanup(api.close)
        with patch("trading.exchange.time.time", return_value=100):
            MarketData(api).book("XAUUSD1")
        self.assertEqual(budget.weight, 2)


if __name__ == "__main__":
    unittest.main()
