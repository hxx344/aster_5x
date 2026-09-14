"""Bounded private GET concurrency, using public fixture keys and no exchange."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import copy
from dataclasses import replace
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode
import unittest
from unittest.mock import patch

from eth_account import Account
from eth_account.messages import encode_typed_data
import httpx

from trading.exchange import API, AccountModeError, BudgetWait, ExchangeError, LiveBroker, RateBudget, RequestNotSent
from trading.models import TradingError
from trading.paper import DemoMarket
from .test_exchange_hardening import FixtureAPI, account_responses


SYMBOL = "XAUUSD1"
DUAL = "/fapi/v3/positionSide/dual"
MULTI = "/fapi/v3/multiAssetsMargin"
ACCOUNT = "/fapi/v3/accountWithJoinMargin"
POSITIONS = "/fapi/v3/positionRisk"
BRACKET = "/fapi/v3/leverageBracket"
ORDERS = "/fapi/v3/openOrders"


class CycleSnapshotParallelTests(unittest.TestCase):
    def make_broker(self, *, responses=None, budget=None, hook=None):
        responses = account_responses() if responses is None else responses
        # Deterministic public signing fixture; never a funded credential.
        private_key = bytes(range(1, 33))
        credentials = {"private_key": private_key, "signer": Account.from_key(private_key).address,
                       "user": "0x" + "22" * 20}
        observed = SimpleNamespace(requests=[], lock=threading.Lock(), active=0, peak=0)

        def handle(request):
            with observed.lock:
                observed.requests.append(request)
                observed.active += 1
                observed.peak = max(observed.peak, observed.active)
            try:
                if hook is not None:
                    response = hook(request, observed)
                    if response is not None:
                        return response
                value = copy.deepcopy(responses[request.url.path])
                if request.url.path == BRACKET and isinstance(value, dict):
                    value["symbol"] = request.url.params["symbol"]
                return httpx.Response(200, json=value)
            finally:
                with observed.lock:
                    observed.active -= 1

        api = API(credentials, transport=httpx.MockTransport(handle), budget=budget or RateBudget())
        self.addCleanup(api.close)
        return LiveBroker(credentials, DemoMarket(), api=api), observed

    def test_six_http_reads_overlap_with_unique_valid_signatures_and_same_snapshot(self):
        barrier = threading.Barrier(6)
        def hook(request, observed):
            barrier.wait(timeout=3)
        broker, observed = self.make_broker(hook=hook)
        fixed_nonce_time = time.time_ns()
        with patch("trading.exchange.time.time_ns", return_value=fixed_nonce_time):
            result = broker.cycle_snapshot([SYMBOL], fresh_modes=True)
        self.assertEqual(observed.peak, 6)
        self.assertEqual(observed.active, 0)
        self.assertEqual(len(observed.requests), 6)
        self.assertEqual(broker.api.budget.inflight, {})
        self.assertEqual(broker.api.budget.local_weight, 72)
        nonces = []
        for request in observed.requests:
            self.assertEqual(request.method, "GET")
            params = dict(parse_qsl(request.url.query.decode()))
            signature = params.pop("signature")
            nonces.append(int(params["nonce"]))
            message = encode_typed_data(domain_data={"name": "AsterSignTransaction", "version": "1", "chainId": 1666,
                "verifyingContract": "0x" + "00" * 20}, message_types={"Message": [{"name": "msg", "type": "string"}]},
                message_data={"msg": urlencode(params)})
            self.assertEqual(Account.recover_message(message, signature=signature), broker.api.credentials["signer"])
        self.assertEqual(sorted(nonces), list(range(min(nonces), min(nonces) + 6)))
        sequential = LiveBroker({}, DemoMarket(), api=FixtureAPI(account_responses())).snapshot([SYMBOL], fresh_modes=True)
        self.assertEqual(result, replace(sequential, timestamp=result.timestamp, open_orders=[]))

    def test_multiple_symbols_keep_at_most_six_inflight_and_join_all(self):
        first_six, release = threading.Event(), threading.Event()
        def hook(request, observed):
            with observed.lock:
                if len(observed.requests) == 6:
                    first_six.set()
            self.assertTrue(release.wait(timeout=3))
        broker, observed = self.make_broker(hook=hook)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(broker.cycle_snapshot, [SYMBOL, "SPCXUSD1", "CLUSD1"], True)
            try:
                self.assertTrue(first_six.wait(timeout=3))
                with observed.lock:
                    self.assertEqual(len(observed.requests), 6)
                    self.assertEqual(observed.active, 6)
                self.assertFalse(future.done())
            finally:
                release.set()
            result = future.result(timeout=3)
        self.assertEqual(len(observed.requests), 10)
        self.assertEqual(observed.peak, 6)
        self.assertEqual(observed.active, 0)
        self.assertEqual(result.open_orders, [])
        self.assertEqual(set(result.brackets), {SYMBOL, "SPCXUSD1", "CLUSD1"})

    def test_ordinary_snapshot_remains_sequential_and_does_not_query_orders(self):
        broker, observed = self.make_broker()
        snapshot = broker.snapshot([SYMBOL], fresh_modes=True)
        self.assertIsNone(snapshot.open_orders)
        self.assertEqual(observed.peak, 1)
        self.assertEqual([request.url.path for request in observed.requests], [DUAL, MULTI, ACCOUNT, POSITIONS, BRACKET])

    def test_mode_and_bracket_caches_preserve_request_counts_and_forced_refresh(self):
        responses = account_responses()
        broker, observed = self.make_broker(responses=responses)
        broker.cycle_snapshot([SYMBOL], fresh_modes=True)
        self.assertEqual(len(observed.requests), 6)
        responses[DUAL]["dualSidePosition"] = False
        reused = broker.cycle_snapshot([SYMBOL])
        self.assertTrue(reused.hedge_mode)
        self.assertCountEqual([request.url.path for request in observed.requests[6:]], [ACCOUNT, POSITIONS, ORDERS])
        fresh = broker.cycle_snapshot([SYMBOL], fresh_modes=True)
        self.assertFalse(fresh.hedge_mode)
        self.assertEqual(len(observed.requests), 14)
        self.assertCountEqual([request.url.path for request in observed.requests[9:]], [DUAL, MULTI, ACCOUNT, POSITIONS, ORDERS])
        with self.assertRaises(AccountModeError):
            fresh.require_modes([SYMBOL])

    def test_parallel_cache_age_starts_before_request_instead_of_on_completion(self):
        clock = SimpleNamespace(now=100.0)
        barrier = threading.Barrier(6, action=lambda: setattr(clock, "now", 102.0))
        api = FixtureAPI(account_responses())
        original = api.call
        def call(*args, **kwargs):
            barrier.wait(timeout=3)
            return original(*args, **kwargs)
        api.call = call
        broker = LiveBroker({}, DemoMarket(), api=api)
        with patch("trading.exchange.time.monotonic", side_effect=lambda: clock.now):
            broker.cycle_snapshot([SYMBOL], fresh_modes=True)
        self.assertEqual(broker.cached_at, {"dual": 100, "multi": 100, "bracket:" + SYMBOL: 100})
        self.assertEqual(broker.leverage_snapshot[1], 102)

    def test_bracket_expiring_during_other_reads_is_refetched_before_validation(self):
        clock = SimpleNamespace(now=100.0)
        api = FixtureAPI(account_responses())
        broker = LiveBroker({}, DemoMarket(), api=api)
        with patch("trading.exchange.time.monotonic", side_effect=lambda: clock.now):
            broker.cycle_snapshot([SYMBOL])
            api.calls.clear()
            clock.now = 104.9
            original = api.call
            barrier = threading.Barrier(3, action=lambda: setattr(clock, "now", 105.1))
            def call(method, path, *args, **kwargs):
                if path != BRACKET:
                    barrier.wait(timeout=3)
                return original(method, path, *args, **kwargs)
            api.call = call
            broker.cycle_snapshot([SYMBOL])
        self.assertCountEqual([call[1] for call in api.calls], [ACCOUNT, POSITIONS, ORDERS, BRACKET])
        self.assertEqual(broker.cached_at["bracket:" + SYMBOL], 105.1)
        self.assertEqual(broker.cached_at["dual"], 100)

    def test_new_bracket_expiring_during_slow_account_read_uses_refreshed_tier(self):
        clock = SimpleNamespace(mono=100.0, wall=1000.0)
        api = FixtureAPI(account_responses())
        broker = LiveBroker({}, DemoMarket(), api=api)
        original = api.call
        barrier, bracket_returned = threading.Barrier(6), threading.Event()
        bracket_reads = []
        caller_thread = threading.get_ident()
        refreshed = [{"notionalFloor": "0", "notionalCap": "1000", "maintMarginRatio": "0.025",
                      "cum": "0", "initialLeverage": 20}]

        def call(method, path, *args, **kwargs):
            if path == BRACKET and bracket_reads:
                self.assertEqual(threading.get_ident(), caller_thread)
                bracket_reads.append(clock.mono)
                api.responses[BRACKET]["brackets"] = refreshed
                return original(method, path, *args, **kwargs)
            barrier.wait(timeout=3)
            if path == BRACKET:
                bracket_reads.append(clock.mono)
                value = original(method, path, *args, **kwargs)
                clock.mono, clock.wall = 100.1, 1000.1
                bracket_returned.set()
                return value
            if path == ACCOUNT:
                self.assertTrue(bracket_returned.wait(timeout=3))
                clock.mono, clock.wall = 106.0, 1006.0
            return original(method, path, *args, **kwargs)

        api.call = call
        with patch("trading.exchange.time.monotonic", side_effect=lambda: clock.mono), \
             patch("trading.exchange.time.time", side_effect=lambda: clock.wall):
            snapshot = broker.cycle_snapshot([SYMBOL], fresh_modes=True)
            self.assertEqual(snapshot.brackets[SYMBOL], refreshed)
            self.assertEqual(snapshot.timestamp, 1000)
            self.assertEqual(clock.wall - snapshot.timestamp, 6)
            snapshot.require_fresh(now=1006)
            with self.assertRaisesRegex(TradingError, "快照已过期"):
                snapshot.require_fresh(now=1008.001)
        self.assertEqual(bracket_reads, [100, 106])
        self.assertEqual(len(api.calls), 7)
        self.assertEqual(broker.cached["bracket:" + SYMBOL]["brackets"], refreshed)
        self.assertEqual(broker.cached_at["bracket:" + SYMBOL], 106)
        self.assertTrue(all(call[0] == "GET" for call in api.calls))

    def test_slow_entire_round_is_stale_and_cannot_publish_cache_or_leverage_token(self):
        wall = SimpleNamespace(now=1000.0)
        barrier = threading.Barrier(6, action=lambda: setattr(wall, "now", 1009.0))
        api = FixtureAPI(account_responses())
        original = api.call
        def call(*args, **kwargs):
            barrier.wait(timeout=3)
            return original(*args, **kwargs)
        api.call = call
        broker = LiveBroker({}, DemoMarket(), api=api)
        with patch("trading.exchange.time.time", side_effect=lambda: wall.now), self.assertRaisesRegex(TradingError, "快照已过期"):
            broker.cycle_snapshot([SYMBOL], fresh_modes=True)
        self.assertEqual(broker.cached_at, {})
        self.assertIsNone(broker.leverage_snapshot)

    def test_all_reads_finish_after_one_failure_before_return_or_next_round(self):
        barrier = threading.Barrier(6)
        failed, release = threading.Event(), threading.Event()
        def hook(request, observed):
            barrier.wait(timeout=3)
            if request.url.path == DUAL:
                failed.set()
                return httpx.Response(500, json={"code": -1000})
            self.assertTrue(release.wait(timeout=3))
        broker, observed = self.make_broker(hook=hook)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(broker.cycle_snapshot, [SYMBOL], True)
            try:
                self.assertTrue(failed.wait(timeout=3))
                self.assertFalse(future.done())
                self.assertEqual(broker.cached_at, {})
                self.assertIsNone(broker.leverage_snapshot)
            finally:
                release.set()
            with self.assertRaises(ExchangeError) as caught:
                future.result(timeout=3)
        self.assertEqual(caught.exception.code, -1000)
        self.assertEqual(observed.active, 0)
        self.assertEqual(broker.api.budget.inflight, {})
        self.assertEqual(len(observed.requests), 6)
        self.assertEqual(broker.cached_at, {})

    def test_explicit_rate_limit_survives_other_errors_and_blocks_the_next_round(self):
        barrier = threading.Barrier(6)
        def hook(request, observed):
            barrier.wait(timeout=3)
            if request.url.path == DUAL:
                return httpx.Response(500, json={"code": -1000})
            if request.url.path == ORDERS:
                return httpx.Response(429, headers={"Retry-After": "250"})
        broker, observed = self.make_broker(hook=hook)
        with self.assertRaises(ExchangeError) as caught:
            broker.cycle_snapshot([SYMBOL], fresh_modes=True)
        self.assertEqual(caught.exception.http_status, 429)
        self.assertEqual(caught.exception.retry_after, 250)
        with self.assertRaises(RequestNotSent):
            broker.cycle_snapshot([SYMBOL], fresh_modes=True)
        self.assertEqual(len(observed.requests), 6)
        self.assertEqual(observed.active, 0)
        self.assertEqual(broker.api.budget.inflight, {})

    def test_mode_error_is_not_hidden_by_another_local_budget_failure(self):
        api = FixtureAPI(account_responses())
        barrier = threading.Barrier(6)
        original = api.call
        mode = AccountModeError("mode needs attention")
        def call(method, path, *args, **kwargs):
            barrier.wait(timeout=3)
            if path == DUAL:
                raise mode
            if path == MULTI:
                raise BudgetWait("budget")
            return original(method, path, *args, **kwargs)
        api.call = call
        broker = LiveBroker({}, DemoMarket(), api=api)
        with self.assertRaises(AccountModeError) as caught:
            broker.cycle_snapshot([SYMBOL], fresh_modes=True)
        self.assertIs(caught.exception, mode)
        self.assertIsNone(broker.leverage_snapshot)
        self.assertEqual(broker.cached_at, {})

    def test_all_budget_contexts_keep_their_effective_limit_in_workers(self):
        cases = [((False, False, False), 1320, False),
                 ((True, False, False), 1320, True),
                 ((True, True, False), 1320, False),
                 ((True, False, True), 1320, True),
                 ((True, False, True), 1500, False),
                 ((True, True, True), 1500, False)]
        names = ("reconciliation", "cycle_accounting", "capacity_monitoring")
        for flags, used, allowed in cases:
            with self.subTest(flags=flags, used=used):
                budget = RateBudget(capacity_reserve=180)
                with budget.reconciliation():
                    budget.reserve(used)
                seen_flags = []
                def hook(request, observed):
                    seen_flags.append(budget._priority_flags())
                broker, observed = self.make_broker(budget=budget, hook=hook)
                with ExitStack() as contexts:
                    for name, enabled in zip(names, flags):
                        if enabled:
                            contexts.enter_context(getattr(budget, name)())
                    if allowed:
                        broker.cycle_snapshot([SYMBOL], fresh_modes=True)
                    else:
                        with self.assertRaises(BudgetWait):
                            broker.cycle_snapshot([SYMBOL], fresh_modes=True)
                    self.assertEqual(budget._priority_flags(), flags)
                self.assertEqual(budget._priority_flags(), (False, False, False))
                self.assertEqual(len(observed.requests), 6 if allowed else 0)
                self.assertEqual(seen_flags, [flags] * (6 if allowed else 0))
                self.assertEqual(budget.inflight, {})

    def test_inherited_priority_does_not_leak_after_worker_exception(self):
        budget = RateBudget(capacity_reserve=180)
        budget.reserve(1320)
        def inherited():
            with budget._inherit_priority((True, False, False)):
                budget.reserve(1)
                raise RuntimeError("worker failed")
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.assertRaises(RuntimeError):
                pool.submit(inherited).result()
            with self.assertRaises(BudgetWait):
                pool.submit(budget.reserve, 1).result()

    def test_invalid_or_inconsistent_responses_never_publish_partial_snapshot(self):
        for path, change in ((DUAL, lambda value: []),
                             (ACCOUNT, lambda value: {**value, "assets": value["assets"] * 2}),
                             (POSITIONS, lambda value: [{**value[0], "positionAmt": "1.00000000000000000000000000001"}, value[1]]),
                             (POSITIONS, lambda value: [{**value[0], "positionSide": "BOTH"}, value[1]]),
                             (ORDERS, lambda value: [{"symbol": "CLUSD1"}])):
            with self.subTest(path=path):
                responses = account_responses()
                responses[path] = change(responses[path])
                broker, observed = self.make_broker(responses=responses)
                with self.assertRaises(TradingError):
                    broker.cycle_snapshot([SYMBOL], fresh_modes=True)
                self.assertEqual(broker.cached_at, {})
                self.assertIsNone(broker.leverage_snapshot)
                self.assertEqual(observed.active, 0)
                self.assertTrue(all(request.method == "GET" for request in observed.requests))

    def test_unknown_private_read_cannot_authorize_leverage_post(self):
        responses = account_responses()
        for rows in (responses[POSITIONS], responses[ACCOUNT]["positions"]):
            for row in rows:
                row["positionAmt"] = "0"
        def hook(request, observed):
            if request.url.path == ACCOUNT:
                raise httpx.ReadTimeout("mocked timeout", request=request)
        broker, observed = self.make_broker(responses=responses, hook=hook)
        with self.assertRaises(RequestNotSent):
            broker.set_cycle_leverage(SYMBOL, 2)
        self.assertIsNone(broker.leverage_snapshot)
        self.assertEqual(broker.cached_at, {})
        self.assertEqual(observed.active, 0)
        self.assertTrue(all(request.method == "GET" for request in observed.requests))
        self.assertEqual(len(observed.requests), 6)


if __name__ == "__main__":
    unittest.main()
