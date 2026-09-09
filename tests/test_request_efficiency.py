"""Request-count and guard regressions for the optimized REST execution path."""
import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading
import time
import unittest
from unittest.mock import patch

import httpx

from trading.engine import Engine
from trading.exchange import API, BudgetWait, ExchangeError, LiveBroker, MarketData, RateBudget
from trading.models import TradingError, dec
from trading.paper import DemoMarket
from .helpers import Fixture
from .test_exchange_hardening import FixtureAPI, account_responses


SYMBOL = "XAUUSD1"


class LeverageReadEfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.responses, self.calls = account_responses(), []
        self.responses["/fapi/v3/leverage"] = {"symbol": SYMBOL, "leverage": 5}
        def handle(request):
            self.calls.append(request.url.path)
            return httpx.Response(200, json=copy.deepcopy(self.responses[request.url.path]))
        self.api = API(transport=httpx.MockTransport(handle), budget=RateBudget())
        self.api.signed_parameters = lambda params: params
        self.addCleanup(self.api.close)
        self.broker = LiveBroker({}, self.f.market, api=self.api)
        self.engine = Engine(self.f.store, market=self.f.market)
        self.engine.brokers["test"] = self.broker
        self.engine.poll_market(SYMBOL)

    def change_actual(self, leverage):
        for path in ("/fapi/v3/positionRisk", "/fapi/v3/accountWithJoinMargin"):
            rows = self.responses[path] if path.endswith("positionRisk") else self.responses[path]["positions"]
            for row in rows:
                row["leverage"] = str(leverage)

    def test_upgrade_uses_two_full_snapshots_and_242_weight_instead_of_352(self):
        self.engine.tick_account("test")
        for path in ("/fapi/v3/accountWithJoinMargin", "/fapi/v3/positionRisk", "/fapi/v3/openOrders"):
            self.assertEqual(self.calls.count(path), 2)
        self.assertEqual(self.calls.count("/fapi/v3/leverage"), 1)
        self.assertEqual(self.api.budget.weight, 242)
        self.assertEqual(self.f.store.intent("test")["target"], 5)
        # The previous third snapshot is still required for direct broker calls.
        self.broker.set_leverage(SYMBOL, 5)
        self.assertEqual(self.calls.count("/fapi/v3/accountWithJoinMargin"), 3)
        self.assertEqual(self.api.budget.weight, 353)  # Three reads and two POSTs; old path had one POST.

    def test_cached_cost_admission_allows_a_complete_upgrade_with_260_weight_remaining(self):
        self.api.budget.reserve(1240)
        self.engine.tick_account("test")
        self.assertEqual(self.calls.count("/fapi/v3/leverage"), 1)
        self.assertEqual(self.api.budget.weight, 1482)
        self.assertLessEqual(self.api.budget.weight, self.api.budget.snapshot()["ordinary_limit"])

    def test_low_budget_wait_is_reported_without_an_http_call_or_pausing_strategy(self):
        self.api.budget.reserve(1450)
        self.engine.tick_account("test")
        self.assertEqual(self.calls, [])
        self.assertTrue(self.f.store.account("test")["enabled"])
        view = self.engine.state()["accounts"][0]
        self.assertEqual(view["status"], "waiting")
        self.assertIn("1450/1500", view["reason"])
        self.assertEqual(self.f.store.events()[0]["kind"], "wait")

    def test_expired_validation_is_reread_and_cannot_lower_actual_leverage(self):
        snapshot = self.broker.snapshot([SYMBOL], fresh_modes=True)
        self.broker.leverage_snapshot = (snapshot, time.monotonic() - 2)
        self.change_actual(10)
        with self.assertRaisesRegex(TradingError, "禁止降杠杆"):
            self.broker.set_leverage(SYMBOL, 5, checked_snapshot=snapshot)
        self.assertEqual(self.calls.count("/fapi/v3/accountWithJoinMargin"), 2)
        self.assertNotIn("/fapi/v3/leverage", self.calls)

    def test_foreign_snapshot_and_snapshot_without_fresh_modes_cannot_bypass_the_read(self):
        for fresh_modes, foreign in ((True, True), (False, False)):
            self.change_actual(4)
            snapshot = self.broker.snapshot([SYMBOL], fresh_modes=fresh_modes)
            if foreign:
                snapshot = replace(snapshot)
            self.change_actual(10)
            with self.subTest(fresh_modes=fresh_modes, foreign=foreign), self.assertRaises(TradingError):
                self.broker.set_leverage(SYMBOL, 5, checked_snapshot=snapshot)
        self.assertNotIn("/fapi/v3/leverage", self.calls)

    def test_a_recently_completed_but_stale_snapshot_must_be_reread(self):
        snapshot = self.broker.snapshot([SYMBOL], fresh_modes=True)
        snapshot.timestamp -= 9
        self.change_actual(10)
        with self.assertRaises(TradingError):
            self.broker.set_leverage(SYMBOL, 5, checked_snapshot=snapshot)
        self.assertNotIn("/fapi/v3/leverage", self.calls)

    def test_invalid_modes_and_foreign_open_orders_still_prevent_the_upgrade(self):
        for change in ("mode", "orders"):
            with self.subTest(change=change):
                self.responses["/fapi/v3/multiAssetsMargin"]["multiAssetsMargin"] = change == "mode"
                self.responses["/fapi/v3/openOrders"] = [{"symbol": "BTCUSD1"}] if change == "orders" else []
                snapshot = self.broker.snapshot([SYMBOL], fresh_modes=True)
                with self.assertRaises(TradingError):
                    self.broker.set_leverage(SYMBOL, 5, checked_snapshot=snapshot)
        self.assertNotIn("/fapi/v3/leverage", self.calls)

    def test_checked_snapshot_is_consumed_once(self):
        snapshot = self.broker.snapshot([SYMBOL], fresh_modes=True)
        self.broker.set_leverage(SYMBOL, 5, checked_snapshot=snapshot)
        self.change_actual(10)
        with self.assertRaises(TradingError):
            self.broker.set_leverage(SYMBOL, 5, checked_snapshot=snapshot)
        self.assertEqual(self.calls.count("/fapi/v3/leverage"), 1)

    def test_snapshot_cost_estimate_uses_caches_but_keeps_all_account_reads(self):
        self.assertEqual(self.broker.snapshot_weight([SYMBOL]), 133)
        self.broker.snapshot([SYMBOL])
        self.assertEqual(self.broker.snapshot_weight([SYMBOL]), 53)
        self.assertEqual(self.broker.snapshot_weight([SYMBOL], fresh_modes=True), 113)
        self.broker.cached_at["fee:" + SYMBOL] = time.monotonic() - 53
        self.assertEqual(self.broker.snapshot_weight([SYMBOL]), 73)


class SharedQuoteTests(unittest.TestCase):
    def setUp(self):
        self.now, self.wall = 100.0, 100.0
        mono = patch("trading.exchange.time.monotonic", side_effect=lambda: self.now)
        wall = patch("trading.exchange.time.time", side_effect=lambda: self.wall)
        mono.start()
        wall.start()
        self.addCleanup(mono.stop)
        self.addCleanup(wall.stop)
        self.responses = {
            "/fapi/v3/premiumIndex": {"symbol": SYMBOL, "markPrice": "100", "time": 100000},
            "/fapi/v3/ticker/bookTicker": {"symbol": SYMBOL, "bidPrice": "100", "askPrice": "100.01",
                                           "bidQty": "10", "askQty": "10", "time": 100000},
        }
        self.api = FixtureAPI(self.responses)
        self.market = MarketData(self.api)

    def test_reads_within_one_second_share_two_requests_and_refresh_at_expiry(self):
        first = self.market.book(SYMBOL)
        self.now = 100.999
        for _ in range(8):
            self.assertIs(self.market.book(SYMBOL), first)
        self.assertEqual(len(self.api.calls), 2)
        self.now = 101
        self.market.book(SYMBOL)
        self.assertEqual(len(self.api.calls), 4)

    def test_expired_exchange_timestamp_is_rechecked_even_inside_cache_age(self):
        self.market.book(SYMBOL)
        self.now, self.wall = 100.1, 103.1
        with self.assertRaises(TradingError):
            self.market.book(SYMBOL)
        self.assertNotIn(SYMBOL, self.market.books)

    def test_refresh_failure_does_not_fall_back_to_the_previous_quote(self):
        self.market.book(SYMBOL)
        self.now = 101
        self.responses["/fapi/v3/premiumIndex"] = ExchangeError("simulated unavailable quote")
        with self.assertRaises(ExchangeError):
            self.market.book(SYMBOL)
        self.assertNotIn(SYMBOL, self.market.books)
        self.responses["/fapi/v3/premiumIndex"] = {"symbol": SYMBOL, "markPrice": "101", "time": 100000}
        self.assertEqual(self.market.book(SYMBOL).mark, 101)

    def test_network_time_does_not_extend_quote_cache_lifetime(self):
        call = self.api.call
        def slow(*args, **kwargs):
            self.now += .6
            return call(*args, **kwargs)
        with patch.object(self.api, "call", side_effect=slow):
            self.market.book(SYMBOL)
            self.market.book(SYMBOL)
        self.assertEqual(len(self.api.calls), 4)

    def test_eight_concurrent_readers_share_one_quote_fetch(self):
        barrier, entered, release = threading.Barrier(8), threading.Event(), threading.Event()
        call = self.api.call
        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError("concurrent reader did not release fetch")
            return call(*args, **kwargs)
        def read(_):
            barrier.wait(2)
            return self.market.book(SYMBOL)
        with patch.object(self.api, "call", side_effect=blocked), ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(read, i) for i in range(8)]
            self.assertTrue(entered.wait(2))
            release.set()
            quotes = [future.result(2) for future in futures]
        self.assertEqual(len(self.api.calls), 2)
        self.assertTrue(all(quote is quotes[0] for quote in quotes))


class BudgetWaitDisplayTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.engine = Engine(self.f.store, market=self.f.market)
        self.engine.brokers["test"] = self.f.broker
        self.engine.poll_market(SYMBOL)

    def test_budget_wait_inside_symbol_work_stops_the_round_and_honors_retry_time(self):
        with patch.object(self.f.market, "book", side_effect=BudgetWait("test local budget wait", retry_after=43)) as book:
            delay = self.engine.tick_account("test")
        self.assertEqual(delay, 43)
        self.assertEqual(book.call_count, 1)
        self.assertEqual(self.engine.views["test"]["status"], "waiting")
        self.assertTrue(self.f.store.account("test")["enabled"])

    def test_changing_budget_numbers_do_not_create_wait_events_every_poll(self):
        for now in (100, 110, 130, 161):
            with patch("trading.engine.time.monotonic", return_value=now), \
                 patch.object(self.f.broker, "snapshot", side_effect=BudgetWait(f"test budget {now}", retry_after=10)):
                self.engine.tick_account("test")
        self.assertEqual(len([e for e in self.f.store.events() if e["kind"] == "wait"]), 2)

    def test_real_exchange_limit_keeps_error_status_and_exchange_backoff(self):
        with patch.object(self.f.broker, "snapshot", side_effect=ExchangeError("Aster 接口限流", retry_after=180)):
            self.assertEqual(self.engine.tick_account("test"), 180)
        self.assertEqual(self.engine.views["test"]["status"], "error")
        self.assertEqual(self.f.store.events()[0]["kind"], "error")


if __name__ == "__main__":
    unittest.main()
