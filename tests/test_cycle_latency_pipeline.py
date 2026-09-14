"""Exercise the complete WS-to-submit path with delayed, signed mock reads."""
from collections import Counter
from copy import deepcopy
from fractions import Fraction
import threading
import time
import unittest
from unittest.mock import patch

from eth_account import Account
import httpx

from tests.helpers import Fixture
from tests.test_exchange_hardening import account_responses
from trading.cycle import DEFAULT_CYCLE
from trading.engine import Engine
from trading.exchange import API, LiveBroker, RateBudget


class _StopAtSubmit(RuntimeError):
    """Observe the send boundary without sending any order or recovery request."""


def measure_pipeline(*, parallel=True, response_delay=0.04, changed=None, phase="open"):
    fixture = Fixture()
    api = None
    try:
        owner = fixture.store.account("test")
        config = {**DEFAULT_CYCLE, "enabled": True, "spread_notional": "1000", "max_notional": "1000"}
        owner["cycle"] = config
        owner["policy"]["symbols"] = ["XAUUSD1"]
        fixture.store.save_account(owner)
        if phase == "close":
            fixture.store.put("cycle:test", {"run_id": "latency-close", "phase": "holding", "config": config,
                "quantities": {"LONG": "0.2", "SHORT": "0.2"}, "opened_at": time.time() - 120,
                "completed_cycles": 0})
        responses = account_responses()
        balance = responses["/fapi/v3/accountWithJoinMargin"]
        balance["assets"][0].update(crossWalletBalance="25000", crossUnPnl="0", maintMargin="0", availableBalance="24000")
        for row in balance["positions"] + responses["/fapi/v3/positionRisk"]:
            row.update(positionAmt="0.2" if phase == "close" else "0", leverage="2", entryPrice="4412.015",
                       markPrice="4412.015", unRealizedProfit="0")
        calls, inflight, peak = Counter(), 0, 0
        lock = threading.Lock()

        def handle(request):
            nonlocal inflight, peak
            if request.method != "GET":
                raise AssertionError("the fixture must never send an order")
            path = request.url.path
            with lock:
                calls[path] += 1
                attempt = calls[path]
                inflight += 1
                peak = max(peak, inflight)
            try:
                if response_delay:
                    time.sleep(response_delay)
                result = deepcopy(responses[path])
                if changed == "positions" and attempt == 2:
                    rows = result["positions"] if path.endswith("accountWithJoinMargin") else result if path.endswith("positionRisk") else []
                    for row in rows:
                        row["positionAmt"] = "0.3"
                if changed == "mode" and path.endswith("positionSide/dual"):
                    result["dualSidePosition"] = False
                return httpx.Response(200, json=result)
            finally:
                with lock:
                    inflight -= 1

        # Public deterministic signing fixture, never a real trading key.
        key = bytes(range(1, 33))
        credentials = {"private_key": key, "signer": Account.from_key(key).address, "user": "0x" + "22" * 20}
        api = API(credentials, transport=httpx.MockTransport(handle), budget=RateBudget())
        broker = LiveBroker(credentials, fixture.market, api=api)
        # Model a warm running account: the first read can reuse mode/bracket
        # caches, while the final read must fetch both account modes again.
        for name, path in (("dual", "/fapi/v3/positionSide/dual"),
                           ("multi", "/fapi/v3/multiAssetsMargin"),
                           ("bracket:XAUUSD1", "/fapi/v3/leverageBracket")):
            broker.cached[name] = deepcopy(responses[path])
            broker.cached_at[name] = time.monotonic()

        def serial_snapshot(symbols, fresh_modes=False):
            # The pre-optimization call order, using the unchanged ordinary
            # snapshot parser and the exact same GETs as the cycle snapshot.
            snapshot = broker.snapshot(symbols, fresh_modes=fresh_modes)
            orders = []
            for symbol in symbols:
                orders.extend(api.call("GET", "/fapi/v3/openOrders", {"symbol": symbol}, signed=True))
            snapshot.open_orders = orders
            snapshot.require_fresh()
            return snapshot

        engine = Engine(fixture.store, market=fixture.market)
        engine.brokers["test"] = broker
        engine.on_cycle_market_update("XAUUSD1", "depth", time.time(), time.monotonic())
        signal = engine.cycle_wake_candidates([owner])["test"]
        original_signal_keys = set(signal)
        submitted = []

        def observe(orders):
            submitted.append(deepcopy(orders))
            # The production observation adapter has already captured its
            # monotonic submit boundary. Do not continue into POST/reconcile.
            raise _StopAtSubmit()

        selected = broker.cycle_snapshot if parallel else serial_snapshot
        with patch.object(broker, "cycle_snapshot", side_effect=selected), \
             patch.object(broker, "submit", side_effect=observe):
            try:
                engine.tick_account("test", cycle_signal=signal)
            except _StopAtSubmit:
                pass
        quality = fixture.store.get("cycle_execution:test")
        return {"parallel": parallel, "response_delay_ms": response_delay * 1000, "phase": phase,
                "quality": quality, "orders": submitted, "private_gets": sum(calls.values()),
                "calls": dict(calls), "peak_requests": peak, "inflight_at_return": inflight,
                "signal_was_mutated": set(signal) != original_signal_keys,
                "pending": fixture.store.intent("test"), "account_enabled": fixture.store.account("test")["enabled"],
                "reason": engine.views.get("test", {}).get("reason")}
    finally:
        if api is not None:
            api.close()
        fixture.close()


class CycleLatencyPipelineTests(unittest.TestCase):
    def test_two_parallel_read_rounds_reduce_end_to_end_wait_without_removing_checks(self):
        serial = measure_pipeline(parallel=False)
        parallel = measure_pipeline(parallel=True)
        for result in (serial, parallel):
            self.assertEqual(result["private_gets"], 8)
            self.assertEqual(result["inflight_at_return"], 0)
            self.assertEqual(len(result["orders"]), 1)
            self.assertEqual(len(result["orders"][0]), 2)
            self.assertFalse(result["signal_was_mutated"])
            for path in ("accountWithJoinMargin", "positionRisk", "openOrders"):
                self.assertEqual(result["calls"]["/fapi/v3/" + path], 2)
            timing = result["quality"]["timing"]
            stages = timing["pre_submit"]
            self.assertEqual(set(stages), {"queue_ms", "initial_account_ms", "planning_ms", "final_account_ms",
                                          "final_check_ms", "persist_ms", "other_ms"})
            self.assertTrue(all(type(value) in (int, float) and value >= 0 for value in stages.values()), stages)
            self.assertAlmostEqual(sum(stages.values()), timing["trigger_to_request_ms"], places=5)
        self.assertEqual(serial["peak_requests"], 1)
        self.assertGreaterEqual(parallel["peak_requests"], 3)
        serial_ms = serial["quality"]["timing"]["trigger_to_request_ms"]
        parallel_ms = parallel["quality"]["timing"]["trigger_to_request_ms"]
        self.assertLess(parallel_ms, serial_ms * 0.75, {"serial_ms": serial_ms, "parallel_ms": parallel_ms})
        self.assertEqual([(o["side"], o["quantity"]) for o in serial["orders"][0]],
                         [(o["side"], o["quantity"]) for o in parallel["orders"][0]])

    def test_parallel_final_private_read_still_rejects_changed_positions_or_modes(self):
        for changed in ("positions", "mode"):
            with self.subTest(changed=changed):
                result = measure_pipeline(response_delay=0, changed=changed)
                self.assertEqual(result["orders"], [])
                self.assertIsNone(result["pending"])
                self.assertIsNone(result["quality"])
                self.assertEqual(result["inflight_at_return"], 0)
                self.assertIsNotNone(result["reason"])
                if changed == "mode":
                    self.assertFalse(result["account_enabled"])

    def test_close_plan_observation_uses_correct_buy_sell_mapping_and_complete_timings(self):
        result = measure_pipeline(response_delay=0, phase="close")
        self.assertEqual(len(result["orders"]), 1)
        self.assertEqual({(row["side"], row["positionSide"], row["quantity"]) for row in result["orders"][0]},
                         {("SELL", "LONG", "0.2"), ("BUY", "SHORT", "0.2")})
        estimate = result["quality"]["final_estimate"]
        self.assertEqual(estimate["status"], "available")
        self.assertEqual(Fraction(estimate["buy_vwap"]), Fraction("4412.02"))
        self.assertEqual(Fraction(estimate["sell_vwap"]), Fraction("4412.01"))
        self.assertTrue(all(value is not None for value in result["quality"]["timing"]["pre_submit"].values()))

    def test_missing_engine_observation_clock_does_not_change_orders(self):
        with patch("trading.engine.clock_tick", return_value=None):
            result = measure_pipeline(response_delay=0)
        self.assertEqual(len(result["orders"]), 1)
        self.assertEqual(result["quality"]["final_estimate"]["status"], "available")
        stages = result["quality"]["timing"]["pre_submit"]
        for key in ("queue_ms", "initial_account_ms", "planning_ms", "other_ms"):
            self.assertIsNone(stages[key])
        for key in ("final_account_ms", "final_check_ms", "persist_ms"):
            self.assertIsNotNone(stages[key])
