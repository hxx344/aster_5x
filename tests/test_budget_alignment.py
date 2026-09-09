"""Exchange-minute accounting with delayed responses and no live HTTP calls."""
from email.utils import formatdate
import unittest
from unittest.mock import patch

import httpx

from trading.exchange import API, BudgetWait, ExchangeError, RateBudget


class AlignedBudgetTests(unittest.TestCase):
    def setUp(self):
        self.now, self.epoch = 100.0, 1800000000  # A UTC minute boundary.
        mono = patch("trading.exchange.time.monotonic", side_effect=lambda: self.now)
        wall = patch("trading.exchange.time.time", side_effect=lambda: self.epoch + self.now - 45)
        mono.start()
        wall.start()
        self.addCleanup(mono.stop)
        self.addCleanup(wall.stop)
        self.budget = RateBudget()

    def headers(self, second, used):
        return {"Date": formatdate(self.epoch + second, usegmt=True), "X-MBX-USED-WEIGHT-1M": str(used)}

    def first_report(self, used=1490):
        ticket = self.budget.reserve(5, track=True)
        self.budget.observe(self.headers(55, used), ticket=ticket)

    def test_high_counter_near_minute_end_waits_for_exchange_boundary_not_sixty_more_seconds(self):
        self.first_report()
        with self.assertRaises(BudgetWait) as blocked:
            self.budget.require_available(20)
        self.assertEqual(blocked.exception.retry_after, 6)
        self.assertIn("1490/1500", str(blocked.exception))
        self.assertIn("本进程计入 5", str(blocked.exception))
        self.now = 105.999
        with self.assertRaises(BudgetWait):
            self.budget.reserve(20)
        self.now = 106
        self.budget.reserve(20)
        self.assertEqual(self.budget.weight, 20)

    def test_observed_new_minute_resets_immediately_but_lower_same_minute_does_not(self):
        self.first_report()
        self.now = 101
        self.budget.observe(self.headers(56, 20))
        self.assertEqual(self.budget.weight, 1490)
        self.now = 105
        ticket = self.budget.reserve(1, track=True)
        self.budget.observe(self.headers(60, 1), ticket=ticket)
        self.assertEqual(self.budget.weight, 1)
        self.budget.require_available(1400)
        self.assertEqual(self.budget.snapshot()["aster_ip_used"], 1)

    def test_late_previous_minute_reply_cannot_restore_old_peak_or_drop_outstanding_requests(self):
        self.first_report(1450)
        late = self.budget.reserve(30, track=True)
        current = self.budget.reserve(1, track=True)
        self.now = 105
        self.budget.observe(self.headers(60, 1), ticket=current)
        self.assertEqual(self.budget.weight, 31)
        self.budget.observe(self.headers(55, 1480), ticket=late)
        self.assertEqual(self.budget.weight, 31)
        self.assertEqual(self.budget.snapshot()["aster_ip_used"], 1)
        self.assertEqual(self.budget.inflight, {})

    def test_pending_reservations_survive_an_automatic_boundary(self):
        self.first_report(100)
        ticket = self.budget.reserve(1000, track=True)
        self.now = 106
        self.assertEqual(self.budget.snapshot()["used"], 1000)
        with self.assertRaises(BudgetWait):
            self.budget.reserve(501)
        self.budget.observe(self.headers(61, 1000), ticket=ticket)
        self.assertEqual(self.budget.inflight, {})

    def test_unreported_public_requests_are_not_lost_on_an_early_server_rollover(self):
        self.first_report(100)
        self.now = 104.9
        self.budget.reserve(40)
        ticket = self.budget.reserve(1, track=True)
        self.now = 105
        self.budget.observe(self.headers(60, 1), ticket=ticket)
        self.assertGreaterEqual(self.budget.weight, 41)

    def test_unknown_result_is_retained_then_expires_without_leaking_inflight_tickets(self):
        self.first_report(100)
        ticket = self.budget.reserve(40, track=True)
        self.now = 104
        self.budget.finish(ticket)
        self.assertEqual(self.budget.inflight, {})
        self.now = 106
        self.assertEqual(self.budget.snapshot()["used"], 40)
        self.now = 166
        self.assertEqual(self.budget.snapshot()["used"], 0)

    def test_invalid_or_untrusted_date_keeps_the_fallback_window(self):
        for date in (None, "invalid", formatdate(self.epoch - 1000, usegmt=True)):
            with self.subTest(date=date):
                budget = RateBudget()
                ticket = budget.reserve(1, track=True)
                budget.observe({"Date": date, "X-MBX-USED-WEIGHT-1M": "1490"}, ticket=ticket)
                self.assertIsNone(budget.server_minute)
                self.assertEqual(budget.snapshot()["reset_after"], 60)
                with self.assertRaises(BudgetWait):
                    budget.reserve(11)

    def test_exchange_backoff_remains_active_across_aligned_reset(self):
        self.first_report()
        self.budget.block(180)
        self.now = 106
        self.assertEqual(self.budget.snapshot()["retry_after"], 174)
        with self.assertRaisesRegex(ExchangeError, "退避中"):
            self.budget.reserve(1)

    def test_api_releases_tracked_requests_on_all_failure_paths(self):
        for response in (httpx.Response(200, text="invalid"), httpx.Response(429),
                         httpx.ConnectError("simulated network failure")):
            with self.subTest(response=type(response).__name__):
                budget = RateBudget()
                def handle(request):
                    if isinstance(response, Exception):
                        raise response
                    return response
                api = API(transport=httpx.MockTransport(handle), budget=budget)
                self.addCleanup(api.close)
                with self.assertRaises(ExchangeError):
                    api.call("GET", "/fapi/v3/time")
                self.assertEqual(budget.inflight, {})
                self.assertGreaterEqual(budget.weight, 1)

    def test_api_missing_credentials_does_not_leave_an_inflight_request(self):
        api = API(budget=self.budget)
        self.addCleanup(api.close)
        with self.assertRaisesRegex(Exception, "凭据"):
            api.call("POST", "/fapi/v3/order", signed=True)
        self.assertEqual(self.budget.inflight, {})


if __name__ == "__main__":
    unittest.main()
