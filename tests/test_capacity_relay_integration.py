"""Relay samples retain source age and never spend the trading IP's budget."""
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

import httpx

import monitor
from tests.helpers import Fixture, account
from trading.capacity_relay_client import PublicCapacityRelayClient
from trading.cycle import DEFAULT_CYCLE
from trading.cycle_capacity import require_cycle_capacity
from trading.engine import Engine, PUBLIC_POLL_ALLOWANCE, CAPACITY_MONITOR_RESERVE
from trading.exchange import API, MarketData, RateBudget, RequestNotSent
from trading.models import TradingError, dec


SYMBOL = "XAUUSD1"
EPOCH = "00000000-0000-4000-8000-000000000001"


class CapacityRelayIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.wall, self.ticks = 1_800_000_000.0, 100.0
        for name, getter in (("time.time", lambda: self.wall), ("time.monotonic", lambda: self.ticks)):
            clock = patch(name, side_effect=getter)
            clock.start()
            self.addCleanup(clock.stop)
        self.rows = {
            "oi": {"success": True, "code": "000000", "data": {"symbol": SYMBOL,
                   "leverageOiRemainingMap": {"5": "12000", "7": "4000", "10": "3000"}}},
            "brackets": {"success": True, "code": "000000", "data": {"brackets": [
                {"symbol": SYMBOL, "riskBrackets": [{"minOpenPosLeverage": 1,
                 "maxOpenPosLeverage": 20, "bracketNotionalCap": "5000"}]}]}},
        }
        self.started = {kind: self.wall - .05 for kind in self.rows}
        self.sequence = {"oi": 2, "brackets": 1}
        self.relay_requests, self.aster_requests = [], []
        self.failure = None

        def relay_request(request):
            self.relay_requests.append(request)
            if self.failure is not None:
                return httpx.Response(self.failure, json={"detail": "unavailable"})
            kind = request.url.params["kind"]
            return httpx.Response(200, headers={"X-MBX-USED-WEIGHT-1M": "9999"}, json={
                "version": 1, "epoch": EPOCH, "sequence": self.sequence[kind],
                "kind": kind, "symbol": SYMBOL, "sampled_at": self.started[kind],
                "published_at": self.wall, "age_ms": (self.wall - self.started[kind]) * 1000,
                "payload": self.rows[kind]})

        def aster_request(request):
            self.aster_requests.append(request)
            raise AssertionError("Relay mode reached Aster public HTTP")

        self.relay = PublicCapacityRelayClient("https://relay.example:8766", "x" * 40,
            transport=httpx.MockTransport(relay_request), clock=lambda: self.wall,
            monotonic=lambda: self.ticks)
        self.addCleanup(self.relay.close)
        self.budget = RateBudget(capacity_reserve=301)
        self.api = API(budget=self.budget, transport=httpx.MockTransport(aster_request))
        self.addCleanup(self.api.close)
        self.market = MarketData(self.api, capacity_relay=self.relay)
        self.engine = Engine(self.f.store, market=self.market)
        self.addCleanup(self.engine.dashboard_reports.close)

    def advance(self, seconds):
        self.wall += seconds
        self.ticks += seconds

    def configure(self, targets=None):
        targets = {SYMBOL: {7}} if targets is None else targets
        intervals, brackets, enabled = self.engine.capacity_poll_schedule(targets, {SYMBOL})
        self.engine.capacity_targets = targets
        self.engine.capacity_intervals = intervals
        self.engine.capacity_brackets_interval = brackets
        self.engine.capacity_poll_enabled = enabled
        return intervals, brackets, enabled

    def test_exact_tier_minimum_and_source_time_survive_remote_transport(self):
        self.market.refresh_public_brackets(SYMBOL)
        sample = self.market.capacities(SYMBOL, [5, 7, 10, 21])
        self.assertEqual(sample, {5: dec(5000), 7: dec(4000), 10: dec(3000)})
        self.assertAlmostEqual(sample.checked_at, self.started["oi"], places=5)
        self.assertEqual(self.budget.snapshot()["used"], 0)
        self.assertIsNone(self.budget.snapshot()["aster_ip_used"])
        self.assertEqual(self.aster_requests, [])

    def test_rereading_duplicate_remote_cache_cannot_reauthorize_cycle(self):
        self.configure()
        self.market.refresh_public_brackets(SYMBOL)
        self.engine.poll_market(SYMBOL)
        checked_at = self.engine.markets[SYMBOL]["capacity_checked_at"]["7"]
        config = {**DEFAULT_CYCLE, "max_notional": "1000"}
        require_cycle_capacity(config, 7, self.engine.markets[SYMBOL], now=self.wall)
        self.advance(1.1)
        self.engine.poll_market(SYMBOL)
        self.assertEqual(self.engine.markets[SYMBOL]["capacity_checked_at"]["7"], checked_at)
        with self.assertRaisesRegex(TradingError, "过期"):
            require_cycle_capacity(config, 7, self.engine.markets[SYMBOL], now=self.wall)

    def test_remote_sampling_releases_reserve_without_erasing_used_weight_or_cooldown(self):
        self.budget.reserve(100)
        self.budget.block(180)
        intervals, _, enabled = self.configure()
        self.assertTrue(enabled)
        self.assertEqual(intervals[SYMBOL], .2)
        state = self.budget.snapshot()
        self.assertEqual((state["capacity_reserve"], state["execution_limit"], state["used"]), (0, 1500, 100))
        self.market.refresh_public_brackets(SYMBOL)
        self.market.capacities(SYMBOL, [7])
        with self.assertRaises(RequestNotSent):
            self.budget.require_available(1)
        self.assertEqual(self.budget.snapshot()["used"], 100)

    def test_remote_consumption_still_works_when_local_aster_budget_is_zero(self):
        self.budget.limit = 1
        self.budget.reserve(1)
        intervals, _, enabled = self.configure()
        self.assertTrue(enabled)
        self.assertEqual(intervals[SYMBOL], .2)
        self.market.refresh_public_brackets(SYMBOL)
        self.assertEqual(self.market.capacities(SYMBOL, [7]), {7: dec(4000)})
        self.assertEqual(self.budget.snapshot()["used"], 1)

    def test_private_scheduling_deducts_no_hidden_remote_public_cost(self):
        owner = account("live", mode="live")
        self.engine.view("live", snapshot={"positions": [
            {"symbol": SYMBOL, "side": side, "leverage": 5, "qty": "0"}
            for side in ("LONG", "SHORT")]})
        self.configure()
        expected = 60 * 120 / (1500 - PUBLIC_POLL_ALLOWANCE + CAPACITY_MONITOR_RESERVE)
        self.assertAlmostEqual(self.engine.scheduling([owner])["live"]["gap"], expected)

    def test_missing_or_expired_relay_never_falls_back_to_aster(self):
        self.configure()
        self.market.refresh_public_brackets(SYMBOL)
        self.engine.poll_market(SYMBOL)
        self.advance(9)
        self.failure = 503
        delay = self.engine.poll_market(SYMBOL)
        self.assertEqual(self.engine.markets[SYMBOL]["status"], "error")
        self.assertGreaterEqual(delay, .5)
        self.assertEqual(self.aster_requests, [])
        self.assertEqual(self.budget.snapshot()["used"], 0)

    def test_public_bracket_age_does_not_restart_on_repeated_refresh(self):
        self.market.refresh_public_brackets(SYMBOL)
        source = self.market.public_brackets[SYMBOL][0]
        self.advance(60)
        self.market.refresh_public_brackets(SYMBOL)
        self.assertEqual(self.market.public_brackets[SYMBOL][0], source)
        self.advance(241)
        with self.assertRaises(TradingError):
            self.market.refresh_public_brackets(SYMBOL)
        self.assertEqual(self.market.public_brackets[SYMBOL][0], source)

    def test_uncovered_or_mismatched_symbol_cannot_return_another_symbols_quota(self):
        with self.assertRaises(TradingError):
            self.market._public_sample(monitor.OI_PATH, "SPCXUSD1")
        self.assertEqual(self.aster_requests, [])

    def test_stream_lifecycle_closes_relay_even_when_another_stream_fails(self):
        relay, quotes, depth = Mock(), Mock(), Mock()
        market = MarketData(self.api, capacity_relay=relay, stream=quotes, depth_stream=depth)
        depth.start.side_effect = RuntimeError("failed stream")
        with self.assertRaises(RuntimeError):
            market.start_stream()
        relay.start.assert_called_once()
        depth.close.side_effect = RuntimeError("failed close")
        with self.assertRaises(RuntimeError):
            market.close_stream()
        relay.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
