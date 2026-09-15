"""Deterministic capacity cadence, public cache lifetime and account-room checks."""
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import patch

import httpx

from trading.engine import Engine, snapshot_json
from trading.exchange import API, ExchangeError, MarketData, RateBudget
from trading.models import dec
from tests.helpers import Fixture
from tests.test_cycle_ws_scheduler import _Harness, _Future, _Pool

SYMBOL = "XAUUSD1"


def payload(data):
    return {"success": True, "code": "000000", "data": data}


class PublicCapacityCacheTests(unittest.TestCase):
    def setUp(self):
        self.now, self.requests = 100, []
        self.brackets = payload({"brackets": [{"symbol": SYMBOL, "riskBrackets": [
            {"minOpenPosLeverage": 1, "maxOpenPosLeverage": 20, "bracketNotionalCap": "1000"}]}]})
        self.remaining = payload({"symbol": SYMBOL, "leverageOiRemainingMap": {"5": "2000", "10": "200", "20": "0"}})
        self.failure = None
        def request(req):
            self.requests.append(req)
            return self.failure or httpx.Response(200, json=self.brackets if req.method == "POST" else self.remaining)
        self.api = API(transport=httpx.MockTransport(request), budget=RateBudget())
        self.addCleanup(self.api.close)
        self.market = MarketData(self.api)
        clock = patch("trading.exchange.time.monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def test_one_get_per_sample_and_minimum_uses_background_tier(self):
        self.market.refresh_public_brackets(SYMBOL)
        for _ in range(5):
            self.assertEqual(self.market.capacities(SYMBOL, [5, 10, 20]), {5: dec(1000), 10: dec(200), 20: dec(0)})
        self.assertEqual([r.method for r in self.requests], ["POST"] + ["GET"] * 5)
        self.assertEqual(self.api.budget.weight, 6)
        self.assertTrue(all(r.url.host == "www.asterdex.com" and "signature" not in str(r.url) for r in self.requests))

    def test_unready_or_expired_cache_never_fetches_tiers_in_sampling_path(self):
        with self.assertRaises(ExchangeError):
            self.market.capacities(SYMBOL, [5])
        self.assertEqual(self.requests, [])
        self.market.refresh_public_brackets(SYMBOL)
        self.now = 400
        with self.assertRaises(ExchangeError):
            self.market.capacities(SYMBOL, [5])
        self.assertEqual(len(self.requests), 1)

    def test_invalid_hot_update_retains_original_age_then_expires(self):
        self.market.refresh_public_brackets(SYMBOL)
        self.now = 160
        self.brackets["data"]["brackets"][0]["symbol"] = "CLUSD1"
        with self.assertRaises(ExchangeError):
            self.market.refresh_public_brackets(SYMBOL)
        self.assertEqual(self.market.public_brackets[SYMBOL][0], 100)
        self.assertEqual(self.market.capacities(SYMBOL, [5]), {5: dec(1000)})
        self.now = 400
        with self.assertRaises(ExchangeError):
            self.market.capacities(SYMBOL, [5])

    def test_missing_requested_tier_never_falls_back_to_another_leverage(self):
        self.market.refresh_public_brackets(SYMBOL)
        self.remaining["data"]["leverageOiRemainingMap"].pop("5")
        self.assertEqual(self.market.capacities(SYMBOL, [5]), {})
        self.remaining["data"]["symbol"] = "CLUSD1"
        with self.assertRaises(ExchangeError):
            self.market.capacities(SYMBOL, [10])

    def test_cycle_uses_exact_nonordinary_leverage_when_both_public_sources_have_it(self):
        self.market.refresh_public_brackets(SYMBOL)
        self.remaining["data"]["leverageOiRemainingMap"]["2"] = "1500"
        self.assertEqual(self.market.capacities(SYMBOL, [2]), {2: dec(1000)})
        self.assertEqual(self.market.capacities(SYMBOL, [3]), {})

    def test_rate_limit_blocks_all_channels_and_keeps_original_cache_age(self):
        self.market.refresh_public_brackets(SYMBOL)
        self.failure = httpx.Response(429, headers={"Retry-After": "240"})
        with self.assertRaises(ExchangeError) as caught:
            self.market.capacities(SYMBOL, [5])
        self.assertEqual(caught.exception.retry_after, 240)
        with self.api.budget.reconciliation(), self.assertRaises(ExchangeError):
            self.api.budget.reserve(1)
        self.assertEqual(self.market.public_brackets[SYMBOL][0], 100)


class CapacitySelectionTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.h = _Harness(self.f)
        self.engine = self.h.engine
        self.engine.view("test", snapshot={"positions": [{"symbol": SYMBOL, "leverage": 5}]})

    def test_enabled_symbols_share_actual_leverages_and_pause_stops_fast_target(self):
        accounts = self.f.store.accounts()
        accounts.append({**deepcopy(accounts[0]), "id": "second"})
        self.engine.view("test", snapshot={"positions": [{"symbol": SYMBOL, "leverage": 10}]})
        self.engine.view("second", snapshot={"positions": [{"symbol": SYMBOL, "leverage": 20}]})
        self.assertEqual(self.engine.cycle_capacity_targets(accounts), {SYMBOL: {10, 20}})
        accounts[0]["enabled"] = False
        self.assertEqual(self.engine.cycle_capacity_targets(accounts), {SYMBOL: {20}})
        accounts[1]["cycle"]["enabled"] = False
        self.assertEqual(self.engine.cycle_capacity_targets(accounts), {})

    def run_scheduler(self, poll, control):
        h = self.h
        h.control = control
        with ExitStack() as stack:
            stack.enter_context(patch("trading.engine.ThreadPoolExecutor", return_value=_Pool()))
            stack.enter_context(patch("trading.engine.time.monotonic", side_effect=lambda: h.ticks))
            stack.enter_context(patch("trading.engine.time.time", side_effect=lambda: h.wall))
            stack.enter_context(patch.object(h.engine, "scheduling", return_value=h.timing))
            stack.enter_context(patch.object(h.engine, "tick_account", return_value=60))
            stack.enter_context(patch.object(h.engine, "poll_market", side_effect=poll))
            for name in ("poll_book", "poll_depth", "poll_public_brackets", "notify"):
                stack.enter_context(patch.object(h.engine, name, return_value=60))
            h.engine.run()
        if h.failure:
            raise h.failure

    def test_start_cadence_does_not_add_response_latency_or_poll_other_symbols_fast(self):
        h, starts = self.h, []
        def poll(symbol):
            starts.append((symbol, h.ticks))
            if symbol == SYMBOL:
                h.ticks += .12
                return .2
            return 2
        def control(step):
            if step == 1:
                h.ticks = 100.199
            elif step == 2:
                h.ticks = 100.201
            else:
                h.engine.shutdown.set()
        self.run_scheduler(poll, control)
        self.assertEqual([round(t, 3) for s, t in starts if s == SYMBOL], [100, 100.201])
        self.assertEqual(len([s for s, _ in starts if s != SYMBOL]), 2)

    def test_slow_request_has_one_inflight_and_no_catch_up_burst(self):
        h, starts, held = self.h, [], _Future(.2, done=False)
        def poll(symbol):
            if symbol != SYMBOL:
                return 2
            starts.append(h.ticks)
            return held if len(starts) == 1 else .2
        def control(step):
            h.ticks = 100 + step * .5
            if step == 2:
                held.complete = True
            if step == 3:
                h.engine.shutdown.set()
        self.run_scheduler(poll, control)
        self.assertEqual(starts, [100, 101])

    def test_configuration_change_cannot_bypass_failed_request_backoff(self):
        h, starts = self.h, []
        def poll(symbol):
            if symbol == SYMBOL:
                starts.append(h.ticks)
                return 180
            return 2
        def control(step):
            h.ticks = 100 + step * .3
            if step == 2:
                h.engine.view("test", snapshot={"positions": [{"symbol": SYMBOL, "leverage": 20}]})
            if step == 3:
                h.engine.shutdown.set()
        self.run_scheduler(poll, control)
        self.assertEqual(starts, [100])

    def test_pause_during_pending_sample_restores_normal_cadence_on_completion(self):
        h, starts, held = self.h, [], _Future(.2, done=False)
        def poll(symbol):
            if symbol != SYMBOL:
                return 2
            starts.append(h.ticks)
            return held if len(starts) == 1 else 2
        def control(step):
            h.ticks = 100 + step * .3
            if step == 1:
                account = h.fixture.store.account("test")
                account["enabled"] = False
                h.fixture.store.save_account(account)
                h.engine.accounts_generation += 1
            if step == 2:
                held.complete = True
            if step == 4:
                h.engine.shutdown.set()
        self.run_scheduler(poll, control)
        self.assertEqual(starts, [100])

    def test_fast_samples_only_replace_active_tier_between_full_samples(self):
        engine = Engine(self.f.store, market=self.f.market)
        engine.capacity_targets = {SYMBOL: {10}}
        engine.capacity_accounts = self.f.store.accounts()
        with patch("trading.engine.time.monotonic", return_value=100), patch.object(self.f.market, "capacities", return_value={5: dec(50), 10: dec(100), 20: dec(200)}):
            self.assertEqual(engine.poll_market(SYMBOL), .2)
        with patch("trading.engine.time.monotonic", return_value=100.2), patch.object(self.f.market, "capacities", return_value={10: dec(0)}) as read:
            engine.poll_market(SYMBOL)
        read.assert_called_once_with(SYMBOL, {10})
        self.assertEqual(engine.markets[SYMBOL]["capacities"], {"5": "50", "10": "0", "20": "200"})
        row = engine.markets[SYMBOL]
        row["capacity_checked_at"]["5"] = row["checked_at"] - 9
        self.assertNotIn(5, engine.capacities(SYMBOL))


class AccountCapacityTests(unittest.TestCase):
    def test_account_cap_uses_both_sides_and_current_exact_leverage(self):
        f = Fixture()
        self.addCleanup(f.close)
        snapshot = f.broker.snapshot([SYMBOL])
        snapshot.positions = [replace(p, qty=dec(2), mark=dec(100), leverage=5) for p in snapshot.positions]
        snapshot.current_leverage_caps = {SYMBOL: (5, dec(1000))}
        room = snapshot_json(snapshot, [SYMBOL])["account_capacity"][SYMBOL]
        self.assertEqual((room["cap"], room["occupied"], room["remaining"]), ("1000", "400", "600"))
        snapshot.current_leverage_caps[SYMBOL] = (5, dec(300))
        self.assertEqual(snapshot_json(snapshot, [SYMBOL])["account_capacity"][SYMBOL]["remaining"], "0")
        snapshot.brackets, snapshot.current_leverage_caps = {}, {}
        self.assertEqual(snapshot_json(snapshot, [SYMBOL])["account_capacity"], {})

    def test_fallback_cap_keeps_its_original_expiry(self):
        f = Fixture()
        self.addCleanup(f.close)
        snapshot = f.broker.snapshot([SYMBOL])
        snapshot.timestamp = 200
        snapshot.cycle_cap_cached_at[SYMBOL] = 97
        with patch("trading.engine.time.monotonic", return_value=100), patch("trading.engine.time.time", return_value=200):
            self.assertEqual(snapshot_json(snapshot, [SYMBOL])["account_capacity"][SYMBOL]["expires_at"], 202)
