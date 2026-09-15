"""Capacity discovery retains headroom without borrowing order recovery quota."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import threading
import time
import httpx
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from trading.engine import CAPACITY_MONITOR_RESERVE
from trading.exchange import BudgetWait, ExchangeError, MarketData, RateBudget, RequestNotSent
from trading.models import dec


class CapacityMonitorBudgetTests(unittest.TestCase):
    def test_execution_leaves_monitor_headroom_and_reports_its_actual_limit(self):
        budget = RateBudget(capacity_reserve=CAPACITY_MONITOR_RESERVE)
        budget.require_available(1407)
        self.assertEqual(budget.weight, 0)
        budget.reserve(1407)
        for check in (budget.require_available, budget.reserve):
            with self.assertRaisesRegex(BudgetWait, "1407/1407"):
                check(1)
        state = budget.snapshot()
        self.assertEqual((state["ordinary_limit"], state["execution_limit"], state["capacity_reserve"]),
                         (1500, 1407, 93))
        self.assertEqual((state["remaining"], state["ordinary_remaining"]), (393, 0))
        self.assertGreater(state["retry_after"], 0)
        with budget.capacity_monitoring():
            budget.require_available(93)
            budget.reserve(93)
        self.assertEqual(budget.weight, 1500)

    def test_monitoring_cannot_spend_reconciliation_quota(self):
        budget = RateBudget(capacity_reserve=360)
        with budget.capacity_monitoring():
            budget.reserve(1500)
            for check in (budget.require_available, budget.reserve):
                with self.assertRaisesRegex(BudgetWait, "1500/1500"):
                    check(1)
        with budget.reconciliation():
            with budget.capacity_monitoring(), self.assertRaises(BudgetWait):
                budget.reserve(1)
            budget.require_available(300)
            budget.reserve(300)
            with self.assertRaisesRegex(BudgetWait, "1800/1800"):
                budget.reserve(1)
        self.assertEqual(budget.weight, 1800)

    def test_monitor_context_is_nested_exception_safe_and_thread_local(self):
        budget = RateBudget(capacity_reserve=360)
        budget.reserve(1140)
        entered, checked = threading.Event(), threading.Event()

        def monitor():
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                with budget.capacity_monitoring():
                    with budget.capacity_monitoring():
                        budget.reserve(1)
                    entered.set()
                    self.assertTrue(checked.wait(3))
                    budget.reserve(1)
                    raise RuntimeError("interrupted")
            with self.assertRaises(BudgetWait):
                budget.reserve(1)

        def execution():
            self.assertTrue(entered.wait(3))
            try:
                with self.assertRaises(BudgetWait):
                    budget.reserve(1)
            finally:
                checked.set()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(monitor), pool.submit(execution)]
            for future in futures:
                future.result()
        self.assertEqual(budget.weight, 1142)

    def test_low_exchange_limits_cap_monitor_reserve_at_one_third_of_ordinary(self):
        for total, ordinary, reserve in ((1800, 1500, 500), (600, 500, 166), (6, 5, 1), (1, 1, 0)):
            with self.subTest(total=total):
                budget = RateBudget(capacity_reserve=1000)
                budget.limit = total
                state = budget.snapshot()
                self.assertEqual((state["ordinary_limit"], state["capacity_reserve"], state["execution_limit"]),
                                 (ordinary, reserve, ordinary - reserve))
                budget.reserve(ordinary - reserve)
                with self.assertRaises(BudgetWait):
                    budget.reserve(1)
                if reserve:
                    with budget.capacity_monitoring():
                        budget.reserve(reserve)
                with budget.capacity_monitoring(), self.assertRaises(BudgetWait):
                    budget.reserve(1)
                if total > ordinary:
                    with budget.reconciliation():
                        budget.reserve(total - ordinary)
                self.assertEqual(budget.weight, total)

    def test_dynamic_configuration_preserves_existing_accounting(self):
        budget = RateBudget()
        budget.reserve(1200)
        budget.configure_capacity_reserve(360)
        with self.assertRaises(BudgetWait):
            budget.reserve(1)
        with budget.capacity_monitoring():
            budget.require_available(300)
        self.assertEqual(budget.weight, 1200)
        budget.configure_capacity_reserve(0)
        budget.reserve(300)
        self.assertEqual(budget.snapshot()["execution_limit"], 1500)

    def test_invalid_monitor_reserve_is_rejected_without_changing_configuration(self):
        budget = RateBudget(capacity_reserve=360)
        for value in (-1, True, 1.5, "360", None):
            with self.subTest(value=value):
                with self.assertRaises(ExchangeError):
                    RateBudget(capacity_reserve=value)
                with self.assertRaises(ExchangeError):
                    budget.configure_capacity_reserve(value)
                self.assertEqual(budget.snapshot()["capacity_reserve"], 360)

    def test_all_channels_obey_exchange_backoff_even_after_window_reset(self):
        with patch("trading.exchange.time.monotonic", return_value=100):
            budget = RateBudget(capacity_reserve=360)
            budget.block(180)
        with patch("trading.exchange.time.monotonic", return_value=160):
            for context in (nullcontext, budget.capacity_monitoring, budget.reconciliation):
                with self.subTest(channel=context.__name__), context():
                    for check in (budget.require_available, budget.reserve):
                        with self.assertRaisesRegex(RequestNotSent, "退避中") as caught:
                            check(1)
                        self.assertEqual(caught.exception.retry_after, 120)
            self.assertEqual(budget.weight, 0)
        with patch("trading.exchange.time.monotonic", return_value=280):
            for context in (nullcontext, budget.capacity_monitoring, budget.reconciliation):
                with context():
                    budget.reserve(1)
            self.assertEqual(budget.weight, 3)

    def test_parallel_monitors_reserve_atomically_without_spending_recovery_quota(self):
        budget = RateBudget(capacity_reserve=360)
        with budget.capacity_monitoring():
            budget.reserve(1490)
        gate = threading.Barrier(8)

        def reserve(_):
            gate.wait()
            try:
                with budget.capacity_monitoring():
                    budget.reserve(2)
                return 1
            except BudgetWait:
                return 0

        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(reserve, range(8))), 5)
        self.assertEqual(budget.weight, 1500)

    def test_capacity_sample_can_use_headroom_and_does_not_keep_priority_context(self):
        budget = RateBudget(capacity_reserve=360)
        budget.reserve(1140)

        def sample(request):
            self.assertEqual(dict(request.url.params), {"symbol": "XAUUSD1"})
            with self.assertRaises(BudgetWait):
                budget.reserve(1)
            return httpx.Response(200, json={"success": True, "code": "000000", "data": {
                "symbol": "XAUUSD1", "leverageOiRemainingMap": {"10": "20", "20": "30"}}})

        client = httpx.Client(transport=httpx.MockTransport(sample))
        self.addCleanup(client.close)
        market = MarketData(SimpleNamespace(budget=budget, http=client))
        market.public_brackets["XAUUSD1"] = (time.monotonic(), {10: dec(100), 20: dec(100)})
        self.assertEqual(market.capacities("XAUUSD1", [20, 10, 20]), {10: dec(20), 20: dec(30)})
        self.assertEqual(budget.weight, 1141)
        with budget.capacity_monitoring():
            budget.reserve(359)
        with patch.object(client, "request") as sampler:
            with self.assertRaises(BudgetWait):
                market.capacities("XAUUSD1", [10, 20])
            sampler.assert_not_called()


if __name__ == "__main__":
    unittest.main()
