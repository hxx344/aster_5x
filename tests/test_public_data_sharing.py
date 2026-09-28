"""Unsigned listing and strategy reads share responses without renewing their age."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import unittest
from unittest.mock import patch

import httpx

import monitor
from trading.exchange import API, BudgetWait, ExchangeError, MarketData, RateBudget
from trading.models import dec


SYMBOL = "XAUUSD1"


class PublicDataSharingTests(unittest.TestCase):
    def setUp(self):
        self.now, self.wall = 100.0, 1000.0
        self.requests, self.failure = [], None
        self.block_method = None
        self.entered, self.release = threading.Event(), threading.Event()
        self.remaining = "2000"
        self.api = API(transport=httpx.MockTransport(self.request), budget=RateBudget())
        self.addCleanup(self.api.close)
        self.market = MarketData(self.api)
        for name, value in (("monotonic", lambda: self.now), ("time", lambda: self.wall)):
            clock = patch("trading.exchange.time." + name, side_effect=value)
            clock.start()
            self.addCleanup(clock.stop)

    def request(self, request):
        self.requests.append(request)
        if request.method == self.block_method:
            self.entered.set()
            if not self.release.wait(3):
                raise AssertionError("Concurrent public read did not finish")
        if self.failure is not None:
            return self.failure
        symbol = (json.loads(request.content)["symbol"] if request.method == "POST"
                  else request.url.params["symbol"])
        data = ({"brackets": [{"symbol": symbol, "riskBrackets": [{
            "minOpenPosLeverage": 1, "maxOpenPosLeverage": 20, "bracketNotionalCap": "1000"}]}]}
            if request.method == "POST" else
            {"symbol": symbol, "leverageOiRemainingMap": {"5": self.remaining, "20": self.remaining}})
        return httpx.Response(200, json={"success": True, "code": "000000", "data": data})

    def advance(self, seconds):
        self.now += seconds
        self.wall += seconds

    def join_read(self, pool, function, key):
        """Prove the second consumer has joined before releasing the transport."""
        pending = self.market.public_sample_reads[key]
        joined = threading.Event()
        original = pending.result
        def wait():
            joined.set()
            return original()
        with patch.object(pending, "result", side_effect=wait):
            follower = pool.submit(function)
            self.assertTrue(joined.wait(3))
            self.release.set()
            return follower.result(timeout=3)

    def listing_owner_with_monitor_followers(self):
        """Hold ordinary admission until both monitor consumers join its read."""
        entered, release, joined = threading.Event(), threading.Event(), threading.Event()
        joined_lock, joined_count = threading.Lock(), 0
        original_read = self.market._public_json
        def controlled(path, *args, **kwargs):
            if path == monitor.OI_PATH and kwargs.get("priority") is False:
                entered.set()
                self.assertTrue(release.wait(3))
            return original_read(path, *args, **kwargs)
        with patch.object(self.market, "_public_json", side_effect=controlled), ThreadPoolExecutor(max_workers=3) as pool:
            listing = pool.submit(self.market.listing_detail, SYMBOL)
            self.assertTrue(entered.wait(3))
            pending = self.market.public_sample_reads[(monitor.OI_PATH, SYMBOL)]
            original_result = pending.result
            def wait():
                nonlocal joined_count
                with joined_lock:
                    joined_count += 1
                    if joined_count == 2:
                        joined.set()
                return original_result()
            with patch.object(pending, "result", side_effect=wait):
                monitors = [pool.submit(self.market.capacities, SYMBOL, [5]) for _ in range(2)]
                self.assertTrue(joined.wait(3))
                release.set()
                results = []
                for follower in monitors:
                    try:
                        results.append(follower.result(timeout=3))
                    except ExchangeError as exc:
                        results.append(exc)
            return listing.result(timeout=3), results

    def test_monitor_followers_rejoin_using_reserved_budget_after_listing_admission_fails(self):
        self.market.refresh_public_brackets(SYMBOL)
        self.api.budget.configure_capacity_reserve(363)
        self.api.budget.reserve(self.api.budget.snapshot()["execution_limit"] - self.api.budget.weight)
        listing, monitors = self.listing_owner_with_monitor_followers()
        self.assertIsNone(listing["capacity"])
        self.assertTrue(listing["error"])
        self.assertEqual(monitors, [{5: dec(1000)}, {5: dec(1000)}])
        self.assertEqual([sample.checked_at for sample in monitors], [1000, 1000])
        self.assertEqual([request.method for request in self.requests], ["POST", "GET"])
        self.assertEqual(self.api.budget.weight, 1138)
        self.assertEqual(self.market.public_sample_reads, {})

    def test_monitor_followers_cannot_retry_into_order_recovery_reserve(self):
        self.market.refresh_public_brackets(SYMBOL)
        self.api.budget.configure_capacity_reserve(363)
        with self.api.budget.capacity_monitoring():
            self.api.budget.reserve(self.api.budget.snapshot()["ordinary_limit"] - self.api.budget.weight)
        listing, monitors = self.listing_owner_with_monitor_followers()
        self.assertIsNone(listing["capacity"])
        self.assertTrue(all(isinstance(result, BudgetWait) for result in monitors))
        self.assertEqual([request.method for request in self.requests], ["POST"])
        self.assertEqual(self.api.budget.weight, 1500)
        self.assertEqual(self.market.public_sample_reads, {})

    def test_monitor_followers_never_retry_shared_exchange_cooldown(self):
        self.market.refresh_public_brackets(SYMBOL)
        self.failure = httpx.Response(429, headers={"Retry-After": "240"})
        listing, monitors = self.listing_owner_with_monitor_followers()
        self.assertIsNone(listing["capacity"])
        self.assertTrue(all(isinstance(result, ExchangeError) and result.http_status == 429 for result in monitors))
        self.assertEqual([result.retry_after for result in monitors], [240, 240])
        self.assertEqual([request.method for request in self.requests], ["POST", "GET"])
        self.assertEqual(self.market.public_sample_reads, {})
        with self.api.budget.capacity_monitoring(), self.assertRaises(ExchangeError):
            self.api.budget.require_available(1)

    def test_concurrent_strategy_and_listing_share_oi_without_changing_source_time(self):
        self.market.refresh_public_brackets(SYMBOL)
        self.block_method = "GET"
        with ThreadPoolExecutor(max_workers=2) as pool:
            strategy = pool.submit(self.market.capacities, SYMBOL, [5, 20])
            self.assertTrue(self.entered.wait(3))
            listing = self.join_read(pool, lambda: self.market.listing_detail(SYMBOL), (monitor.OI_PATH, SYMBOL))
            capacities = strategy.result(timeout=3)
        self.assertEqual(capacities, {5: dec(1000), 20: dec(1000)})
        self.assertEqual((capacities.checked_at, listing["checked_at"], listing["brackets_checked_at"]),
                         (1000, 1000, 1000))
        self.assertEqual([request.method for request in self.requests], ["POST", "GET"])
        self.assertEqual(self.api.budget.weight, 2)
        self.assertTrue(all(request.url.host == "www.asterdex.com" and "signature" not in str(request.url)
                            for request in self.requests))

    def test_listing_brackets_are_shared_with_background_without_extending_tier_age(self):
        self.block_method = "POST"
        with ThreadPoolExecutor(max_workers=2) as pool:
            listing = pool.submit(self.market.listing_detail, SYMBOL)
            self.assertTrue(self.entered.wait(3))
            self.join_read(pool, lambda: self.market.refresh_public_brackets(SYMBOL),
                           (monitor.BRACKETS_PATH, SYMBOL))
            self.assertEqual(listing.result(timeout=3)["brackets_checked_at"], 1000)
        self.assertEqual([request.method for request in self.requests], ["POST", "GET"])
        self.advance(59)
        self.market.refresh_public_brackets(SYMBOL)
        self.assertEqual(self.market.public_brackets[SYMBOL][0], 100)
        self.assertEqual(len(self.requests), 2)
        self.block_method = None
        self.advance(1)
        self.market.refresh_public_brackets(SYMBOL)
        self.assertEqual(self.market.public_brackets[SYMBOL][0], 160)
        self.assertEqual(len(self.requests), 3)

    def test_short_cache_keeps_original_timestamp_and_refreshes_at_fast_sampling_period(self):
        self.market.refresh_public_brackets(SYMBOL)
        first = self.market.capacities(SYMBOL, [5])
        self.advance(.05)
        listing = self.market.listing_detail(SYMBOL)
        self.assertEqual(listing["checked_at"], first.checked_at)
        self.assertEqual(len(self.requests), 2)
        # The next scheduled 200 ms poll can enter a little earlier than the
        # prior HTTP start plus 200 ms; it must still fetch the next sample.
        self.advance(.149)
        self.remaining = "42"
        second = self.market.capacities(SYMBOL, [5])
        self.assertEqual(second, {5: dec(42)})
        self.assertEqual(second.checked_at, self.wall)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.market.public_brackets[SYMBOL][0], 100)

    def test_expired_shared_read_failure_is_not_cached_or_retried_by_waiters(self):
        self.market.refresh_public_brackets(SYMBOL)
        self.market.capacities(SYMBOL, [5])
        original = self.market.public_samples[(monitor.OI_PATH, SYMBOL)]
        self.advance(.3)
        self.block_method = "GET"
        self.failure = httpx.Response(503)
        with ThreadPoolExecutor(max_workers=2) as pool:
            strategy = pool.submit(self.market.capacities, SYMBOL, [5])
            self.assertTrue(self.entered.wait(3))
            listing = self.join_read(pool, lambda: self.market.listing_detail(SYMBOL), (monitor.OI_PATH, SYMBOL))
            with self.assertRaises(ExchangeError):
                strategy.result(timeout=3)
        self.assertIsNone(listing["checked_at"])
        self.assertTrue(listing["error"])
        self.assertIs(self.market.public_samples[(monitor.OI_PATH, SYMBOL)], original)
        self.assertEqual(self.market.public_sample_reads, {})
        self.assertEqual(len(self.requests), 3)
        self.block_method, self.failure = None, None
        self.assertEqual(self.market.capacities(SYMBOL, [5]).checked_at, self.wall)
        self.assertEqual(len(self.requests), 4)

    def test_invalid_brackets_do_not_replace_prior_public_sample_or_tier_age(self):
        self.market.refresh_public_brackets(SYMBOL)
        original = self.market.public_samples[(monitor.BRACKETS_PATH, SYMBOL)]
        self.advance(60)
        self.failure = httpx.Response(200, json={"success": True, "code": "000000", "data": {"brackets": []}})
        with self.assertRaises(ExchangeError):
            self.market.refresh_public_brackets(SYMBOL)
        self.assertIs(self.market.public_samples[(monitor.BRACKETS_PATH, SYMBOL)], original)
        self.assertEqual(self.market.public_brackets[SYMBOL][0], 100)
        self.failure = None
        self.advance(240)
        with self.assertRaises(ExchangeError):
            self.market.capacities(SYMBOL, [5])
        self.assertEqual(len(self.requests), 2)

    def test_symbols_remain_separate_and_slow_requests_keep_start_time(self):
        first = self.market.listing_detail(SYMBOL)
        self.advance(.1)
        other = self.market.listing_detail("CLUSD1")
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(first["checked_at"], 1000)
        self.assertEqual(other["checked_at"], self.wall)
        self.advance(1)
        self.market.refresh_public_brackets(SYMBOL)
        original = self.market._public_json
        def slow(*args, **kwargs):
            result = original(*args, **kwargs)
            self.advance(2)
            return result
        started = self.wall
        with patch.object(self.market, "_public_json", side_effect=slow):
            sample = self.market.capacities(SYMBOL, [5])
        self.assertEqual(sample.checked_at, started)
        self.assertLess(sample.checked_at, self.wall)


if __name__ == "__main__":
    unittest.main()
