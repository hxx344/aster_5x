"""WS wakes retain the ordinary execution and final-check boundaries."""
from copy import deepcopy
from dataclasses import replace
from fractions import Fraction
import time
from unittest import TestCase
from unittest.mock import patch

import httpx

from tests.helpers import account
from tests import test_cycle_parallel_engine as parallel_cases
from trading.exchange import API, LiveBroker, RateBudget
from trading.models import dec


class CycleSignalEngineTests(TestCase):
    setUp = parallel_cases.CycleParallelEngineTests.setUp
    start = parallel_cases.CycleParallelEngineTests.start
    capacities = parallel_cases.CycleParallelEngineTests.capacities
    pair = parallel_cases.CycleParallelEngineTests.pair
    assert_pair_filled = parallel_cases.CycleParallelEngineTests.assert_pair_filled

    def signal(self):
        owner = self.f.store.account("test")
        self.engine.on_cycle_market_update("XAUUSD1", "depth", time.time(), time.monotonic())
        return self.engine.cycle_wake_candidates([owner])["test"]

    def test_fast_open_only_runs_cycle_and_ordinary_round_still_fills_other_market(self):
        self.start()
        self.capacities({"SPCXUSD1": {5: 500000}})
        signal = self.signal()
        with patch("trading.engine.Executor.open_pair", side_effect=AssertionError("fast wake ran ordinary work")):
            self.engine.tick_account("test", cycle_signal=signal)
        self.assert_pair_filled("XAUUSD1")
        self.assertEqual(tuple(p.qty for p in self.pair("SPCXUSD1")), (0, 0))
        quality = self.f.store.get("cycle_execution:test")
        self.assertEqual(quality["phase"], "open")
        self.assertEqual(quality["trigger"]["source"], "depth")
        self.assertTrue(quality["actual"]["confirmed"])
        for stage in ("trigger_estimate", "final_estimate", "actual"):
            self.assertEqual(dec(quality[stage]["quantity"]), dec(quality["quantity"]))
            self.assertIsNotNone(quality[stage]["spread_bp"])
        self.assertGreaterEqual(quality["timing"]["trigger_to_request_ms"], 0)
        self.engine.tick_account("test")
        self.assert_pair_filled("SPCXUSD1")
        self.assertEqual(self.f.store.get("cycle_execution:test")["intent_id"], quality["intent_id"])

    def test_fast_close_requires_elapsed_hold_and_closes_exact_tracked_quantity(self):
        self.start()
        self.engine.tick_account("test", cycle_signal=self.signal())
        progress = self.f.store.get("cycle:test")
        quantity = progress["quantities"]["LONG"]
        with patch.object(self.f.broker, "cycle_snapshot", side_effect=AssertionError("holding wake read private state")):
            self.engine.tick_account("test", cycle_signal={"symbol": "XAUUSD1", "source": "bbo",
                "received_at": time.time(), "received_monotonic": time.monotonic()})
        progress["opened_at"] = time.time() - progress["config"]["hold_seconds"] - 1
        self.f.store.put("cycle:test", progress)
        self.engine.tick_account("test", cycle_signal=self.signal())
        self.assertEqual(tuple(p.qty for p in self.pair("XAUUSD1")), (0, 0))
        self.assertEqual(self.f.store.get("cycle:test")["completed_cycles"], 1)
        quality = self.f.store.get("cycle_execution:test")
        self.assertEqual(quality["phase"], "close")
        self.assertEqual(quality["quantity"], quantity)
        self.assertTrue(quality["actual"]["confirmed"])

    def test_worker_rejects_paused_mismatched_and_expired_signals_before_private_reads(self):
        self.start()
        original = self.signal()
        for case in ("paused", "symbol", "expired", "future"):
            with self.subTest(case=case):
                owner = self.f.store.account("test")
                owner["enabled"] = case != "paused"
                self.f.store.save_account(owner)
                signal = {**original, "received_at": time.time(), "received_monotonic": time.monotonic()}
                if case == "symbol":
                    signal["symbol"] = "SPCXUSD1"
                if case == "expired":
                    signal["received_monotonic"] -= 4
                if case == "future":
                    signal["received_at"] += 4
                with patch.object(self.engine, "broker", side_effect=AssertionError("ineligible wake reached broker")):
                    self.engine.tick_account("test", cycle_signal=signal)
        self.assertEqual(self.f.broker.state["orders"], {})

    def test_pending_and_post_fill_checks_are_left_for_normal_recovery(self):
        self.start()
        signal = self.signal()
        for case in ("intent", "post_fill"):
            with self.subTest(case=case):
                if case == "post_fill":
                    self.f.store.put("post_fill_check:test", {"intent_id": "previous"})
                with patch.object(self.f.store, "intent", return_value={"id": "existing"} if case == "intent" else None), \
                     patch.object(self.engine, "broker", side_effect=AssertionError("wake bypassed recovery")):
                    self.engine.tick_account("test", cycle_signal=signal)
        self.assertEqual(self.f.broker.state["orders"], {})

    def test_final_depth_change_blocks_orders_after_public_hint_and_private_checks(self):
        self.start()
        signal = self.signal()
        fresh = self.f.market.depth("XAUUSD1")
        wide = replace(fresh, asks=tuple((p + Fraction(1), qty) for p, qty in fresh.asks))
        # The worker rechecks public data, plans once, then checks again at the
        # last pre-submit callback after the fresh private account snapshot.
        with patch.object(self.f.market, "depth", side_effect=[fresh, fresh, wide]) as reads, \
             patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            self.engine.tick_account("test", cycle_signal=signal)
        self.assertEqual(reads.call_count, 3)
        submit.assert_not_called()
        self.assertIsNone(self.f.store.intent("test"))
        self.assertEqual(self.f.broker.state["orders"], {})
        self.assertEqual(self.engine.cycle_quote_backoff["test"], self.engine.account_backoff["test"])

    def test_current_depth_rejection_never_reaches_private_reads(self):
        self.start()
        signal = self.signal()
        stale = replace(self.f.market.depth("XAUUSD1"), timestamp=time.time() - 4)
        with patch.object(self.f.market, "depth", return_value=stale), \
             patch.object(self.engine, "broker", side_effect=AssertionError("stale hint reached broker")):
            self.engine.tick_account("test", cycle_signal=signal)
        self.assertEqual(self.f.broker.state["orders"], {})

    def test_private_position_integrity_is_still_checked_after_public_hint(self):
        self.start()
        signal = self.signal()
        self.f.broker.state["positions"]["XAUUSD1:LONG"] = {"qty": "1", "entry": "4412"}
        self.f.broker.save()
        with patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            self.engine.tick_account("test", cycle_signal=signal)
        submit.assert_not_called()
        self.assertFalse(self.f.store.account("test")["enabled"])
        self.assertEqual(self.engine.views["test"]["cycle_state"]["phase"], "attention")
        self.assertNotIn("test", self.engine.cycle_quote_backoff)

    def test_live_budget_rejects_whole_wake_before_any_http_request(self):
        self.start()
        signal = self.signal()
        seen = []
        budget = RateBudget()
        api = API(transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(500)), budget=budget)
        self.addCleanup(api.close)
        self.engine.brokers["test"] = LiveBroker({}, self.f.market, api=api)
        budget.reserve(1490)
        with patch.object(self.f.market, "depth_weight", return_value=20, create=True):
            self.engine.tick_account("test", cycle_signal=signal)
        self.assertEqual(seen, [])
        self.assertIn("预算", self.engine.views["test"]["reason"])
        self.assertGreater(self.engine.account_backoff["test"], time.monotonic())
        self.assertNotIn("test", self.engine.cycle_quote_backoff)

    def test_quality_state_is_owned_by_saved_account_and_survives_restart(self):
        self.start()
        self.engine.tick_account("test", cycle_signal=self.signal())
        quality = deepcopy(self.f.store.get("cycle_execution:test"))
        self.f.store.save_account(account("second"))
        self.engine.views["second"] = {"cycle_state": {"execution_quality": quality}}
        rows = {row["id"]: row for row in self.engine.state()["accounts"]}
        self.assertEqual(rows["test"]["cycle_state"]["execution_quality"], quality)
        self.assertIsNone(rows["second"]["cycle_state"]["execution_quality"])
        restarted = type(self.engine)(self.f.store, market=self.f.market)
        rows = {row["id"]: row for row in restarted.state()["accounts"]}
        self.assertEqual(rows["test"]["cycle_state"]["execution_quality"], quality)
