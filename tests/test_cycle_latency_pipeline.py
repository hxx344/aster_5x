"""Warm in the background, then prove WS-to-submit performs zero HTTP reads."""
from collections import Counter
from copy import deepcopy
from fractions import Fraction
from types import SimpleNamespace
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
from trading.exchange import API, LiveBroker, MarketData, RateBudget


class _StopAtSubmit(RuntimeError):
    """Observe the send boundary without sending any order or recovery request."""


def measure_pipeline(*, response_delay=0.04, changed=None, phase="open", warm_modes=True,
                     during_history=False, hot_state="ready"):
    fixture = Fixture()
    api = None
    try:
        owner = fixture.store.account("test")
        config = {**DEFAULT_CYCLE, "enabled": True, "spread_notional": "1000", "max_notional": "1000"}
        owner["cycle"] = config
        owner["mode"] = "live"
        owner["policy"]["symbols"] = ["XAUUSD1"]
        fixture.store.save_account(owner)
        if phase == "close":
            fixture.store.put("cycle:test", {"run_id": "latency-close", "phase": "holding", "config": config,
                "quantities": {"LONG": "0.2", "SHORT": "0.2"}, "opened_at": time.time() - 120,
                "completed_cycles": 0})
        responses = account_responses()
        balance = responses["/fapi/v3/accountWithJoinMargin"]
        balance["assets"][0].update(crossWalletBalance="25000", crossUnPnl="0", maintMargin="0", availableBalance="24000")
        for row in balance["positions"]:
            row.update(positionAmt="0.2" if phase == "close" else "0", leverage="2", entryPrice="4412.015",
                       unrealizedProfit="0")
        calls, inflight, peak = Counter(), 0, 0
        events = []
        lock = threading.Lock()

        def change_account():
            if changed == "positions":
                for row in balance["positions"]:
                    row["positionAmt"] = "NaN"
            elif changed == "mode":
                balance["positions"] = [{**balance["positions"][0], "positionSide": "BOTH"}]
            elif changed == "balance":
                balance["assets"][0].update(crossWalletBalance="1", availableBalance="1")
            elif changed == "cap":
                for row in balance["positions"]:
                    row["maxNotional"] = "0"
            elif changed == "orders":
                responses["/fapi/v3/openOrders"] = [{"symbol": "XAUUSD1", "orderId": 1}]
            elif changed == "multi":
                responses["/fapi/v3/multiAssetsMargin"] = {"multiAssetsMargin": True}

        change_account()

        def handle(request):
            nonlocal inflight, peak
            if request.method != "GET":
                raise AssertionError("the fixture must never send an order")
            path = request.url.path
            with lock:
                calls[path] += 1
                events.append(path)
                inflight += 1
                peak = max(peak, inflight)
            try:
                if response_delay:
                    time.sleep(response_delay)
                result = deepcopy(responses[path])
                return httpx.Response(200, json=result)
            finally:
                with lock:
                    inflight -= 1

        # Public deterministic signing fixture, never a real trading key.
        key = bytes(range(1, 33))
        credentials = {"private_key": key, "signer": Account.from_key(key).address, "user": "0x" + "22" * 20}
        api = API(credentials, transport=httpx.MockTransport(handle), budget=RateBudget())
        market = MarketData(api, stream=SimpleNamespace(book=fixture.market.book),
            depth_stream=SimpleNamespace(snapshot=fixture.market.depth))
        market.rules, market.assets = fixture.market.rules, fixture.market.assets
        broker = LiveBroker(credentials, market, api=api)
        # No dual-mode or tier caches are needed for this fixed-leverage path.
        if warm_modes:
            broker.cached["multi"] = {"multiAssetsMargin": False}
            broker.cached_at["multi"] = time.monotonic()

        broker.cycle_cache.configure(["XAUUSD1"])
        broker.cycle_cache.set_connected(True)
        engine = Engine(fixture.store, market=market)
        engine.brokers["test"] = broker
        if hot_state != "cold":
            with patch.dict("os.environ", {"ASTER_ALLOW_LIVE": "1"}), patch.object(broker, "start_cycle_hot_data"):
                engine.poll_cycle_hot_data("test")
        background_calls = dict(calls)
        calls.clear()
        events.clear()
        if hot_state == "event":
            broker._cycle_account_event("ACCOUNT_UPDATE")
        elif hot_state == "disconnected":
            broker.cycle_cache.set_connected(False)
        elif hot_state == "config":
            broker.cycle_cache.configure(["CLUSD1"])
        elif hot_state == "expired":
            broker.cycle_cache._monotonic = lambda: time.monotonic() + 9
        elif hot_state == "missing_quote":
            market.stream.book = lambda symbol: None
        elif hot_state == "missing_depth":
            market.depth_stream.snapshot = lambda symbol: None

        engine.on_cycle_market_update("XAUUSD1", "depth", time.time(), time.monotonic())
        signal = {"symbol": "XAUUSD1", "source": "depth", "received_at": time.time(),
                  "received_monotonic": time.monotonic()}
        original_signal_keys = set(signal)
        submitted = []

        def observe(orders):
            submitted.append(deepcopy(orders))
            # The production observation adapter has already captured its
            # monotonic submit boundary. Do not continue into POST/reconcile.
            raise _StopAtSubmit()

        with patch.dict("os.environ", {"ASTER_ALLOW_LIVE": "1"}), \
             patch.object(fixture.store, "cycle_volume_backlog", return_value=[{"id": "prior"}] if during_history else []), \
             patch("trading.cycle_execution.CycleExecutor.sync_volume", side_effect=AssertionError("history ran on trade worker")) as history, \
             patch.object(broker, "submit", side_effect=observe):
            try:
                engine.tick_account("test", cycle_signal=signal)
            except _StopAtSubmit:
                pass
        quality = fixture.store.get("cycle_execution:test")
        return {"response_delay_ms": response_delay * 1000, "phase": phase, "events": events,
                "quality": quality, "orders": submitted, "private_gets": sum(calls.values()),
                "background_calls": background_calls, "history_reads": history.call_count,
                "calls": dict(calls), "peak_requests": peak, "inflight_at_return": inflight,
                "signal_was_mutated": set(signal) != original_signal_keys,
                "pending": fixture.store.intent("test"), "account_enabled": fixture.store.account("test")["enabled"],
                "reason": engine.views.get("test", {}).get("reason")}
    finally:
        if api is not None:
            api.close()
        fixture.close()


class CycleLatencyPipelineTests(unittest.TestCase):
    def test_background_snapshot_authorizes_open_and_close_with_zero_trigger_http(self):
        for phase in ("open", "close"):
            result = measure_pipeline(phase=phase)
            self.assertEqual(result["private_gets"], 0)
            self.assertEqual(result["inflight_at_return"], 0)
            self.assertEqual(len(result["orders"]), 1)
            self.assertEqual(len(result["orders"][0]), 2)
            self.assertFalse(result["signal_was_mutated"])
            self.assertEqual(result["calls"], {})
            self.assertEqual(result["background_calls"], {"/fapi/v3/accountWithJoinMargin": 1,
                                                          "/fapi/v3/multiAssetsMargin": 1})
            self.assertEqual(result["history_reads"], 0)
            self.assertEqual(result["peak_requests"], 2)
            timing = result["quality"]["timing"]
            stages = timing["pre_submit"]
            self.assertEqual(set(stages), {"queue_ms", "initial_account_ms", "planning_ms", "final_account_ms",
                                          "final_check_ms", "persist_ms", "other_ms"})
            self.assertTrue(all(type(value) in (int, float) and value >= 0 for value in stages.values()), stages)
            self.assertAlmostEqual(sum(stages.values()), timing["trigger_to_request_ms"], places=5)

    def test_hot_snapshot_still_blocks_invalid_account(self):
        for changed in ("positions", "mode", "balance", "cap"):
            with self.subTest(changed=changed):
                result = measure_pipeline(response_delay=0, changed=changed)
                self.assertEqual(result["orders"], [])
                self.assertIsNone(result["pending"])
                self.assertIsNone(result["quality"])
                self.assertEqual(result["inflight_at_return"], 0)
                self.assertIsNotNone(result["reason"])
                if changed == "mode":
                    self.assertFalse(result["account_enabled"])

    def test_background_mode_baseline_overrides_preexisting_cached_false(self):
        for warm_modes in (False, True):
            with self.subTest(warm_modes=warm_modes):
                result = measure_pipeline(response_delay=0, warm_modes=warm_modes)
                self.assertEqual(result["private_gets"], 0)
                self.assertEqual(result["background_calls"]["/fapi/v3/multiAssetsMargin"], 1)
                self.assertEqual(len(result["orders"]), 1)
                changed = measure_pipeline(response_delay=0, warm_modes=warm_modes, changed="multi")
                self.assertFalse(changed["account_enabled"])
                self.assertEqual(changed["orders"], [])
                self.assertIsNone(changed["pending"])

    def test_missing_hot_state_waits_without_any_http_fallback(self):
        for state in ("cold", "event", "disconnected", "config", "expired", "missing_quote", "missing_depth"):
            with self.subTest(state=state):
                result = measure_pipeline(response_delay=0, hot_state=state)
                self.assertEqual(result["calls"], {})
                self.assertEqual(result["orders"], [])
                self.assertIsNone(result["pending"])
                self.assertTrue(result["account_enabled"])

    def test_unsynced_history_blocks_open_locally_without_querying_on_trade_worker(self):
        result = measure_pipeline(response_delay=0, during_history=True)
        self.assertEqual(result["private_gets"], 0)
        self.assertEqual(result["history_reads"], 0)
        self.assertEqual(result["orders"], [])
        self.assertIsNone(result["pending"])

    def test_external_order_response_is_never_requested(self):
        result = measure_pipeline(response_delay=0, changed="orders")
        self.assertEqual(len(result["orders"]), 1)
        self.assertNotIn("/fapi/v3/openOrders", result["background_calls"])
        self.assertEqual(result["calls"], {})

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
