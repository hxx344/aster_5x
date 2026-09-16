"""UTC quota sizing, recovery and externally visible daily-volume contracts."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from decimal import localcontext
from fractions import Fraction
from unittest import TestCase
from unittest.mock import patch

from tests import test_cycle_engine as engine_cases, test_cycle_planning as planning_cases
from trading.cycle import DailyVolumeLimitError, validate_cycle
from trading.engine import Engine
from trading.models import TradingError, dec
from tests.helpers import seed_cycle_capacity


class DailyQuotaPlanningTests(TestCase):
    setUp = planning_cases.CyclePlanningTests.setUp
    plan = planning_cases.CyclePlanningTests.plan
    hold = planning_cases.CyclePlanningTests.hold

    def test_reserves_four_fills_and_accepts_exact_budget(self):
        with localcontext() as context:
            context.prec = 6
            plan = self.plan(daily_remaining="400.4")
        self.assertEqual(plan.qty, dec("1.001"))
        self.assertEqual(2 * (Fraction(plan.long_notional) + Fraction(plan.short_notional)), Fraction("400.4"))
        self.assertEqual(self.plan(daily_remaining="400.399999999999999999999999999").qty, dec("1"))
        self.assertEqual(self.plan().qty, dec("100"))

    def test_insufficient_daily_budget_has_distinct_wait_and_market_errors_do_not(self):
        for remaining in ("0", "19.999"):
            with self.subTest(remaining=remaining), self.assertRaises(DailyVolumeLimitError):
                self.plan(daily_remaining=remaining)
        self.account["cycle"]["min_notional"] = "100"
        with self.assertRaises(DailyVolumeLimitError):
            self.plan(daily_remaining="399.9")
        self.account["cycle"]["max_notional"] = "4.9"
        self.account["cycle"]["min_notional"] = "0"
        with self.assertRaises(TradingError) as caught:
            self.plan(daily_remaining="0")
        self.assertNotIsInstance(caught.exception, DailyVolumeLimitError)

    def test_closing_ignores_exhausted_daily_allowance(self):
        self.hold()
        self.assertEqual(self.plan(daily_remaining="0").phase, "close")

    def test_limit_validation_is_strict_and_zero_means_unlimited(self):
        self.assertEqual(validate_cycle()["daily_volume_limit"], "0")
        self.assertEqual(validate_cycle({"daily_volume_limit": "1e12"})["daily_volume_limit"], "1000000000000")
        for invalid in (-1, 1, True, "-1", "NaN", "Infinity", "1000000000000.00000000001"):
            with self.subTest(invalid=invalid), self.assertRaises(TradingError):
                validate_cycle({"daily_volume_limit": invalid})


class DailyQuotaEngineTests(TestCase):
    setUp = engine_cases.CycleEngineTests.setUp
    select = engine_cases.CycleEngineTests.select
    expire_hold = engine_cases.CycleEngineTests.expire_hold

    def run_until_holding(self):
        seed_cycle_capacity(self.engine)
        for _ in range(5):
            self.engine.tick_account("test")
            progress = self.f.store.get("cycle:test")
            if progress and progress["phase"] == "holding":
                return progress
        self.fail(str(self.engine.state()["accounts"][0]))

    def capped_round(self):
        self.select(daily_volume_limit="1000")
        self.engine.enable("test", True)
        progress = self.run_until_holding()
        self.assertLessEqual(dec(self.f.store.cycle_daily_volume("test")["volume"]), 500)
        self.expire_hold()
        self.engine.tick_account("test")
        self.assertEqual(self.f.store.get("cycle:test")["completed_cycles"], 1)
        self.engine.tick_account("test")
        state = self.engine.state()["accounts"][0]
        self.assertEqual(state["cycle_state"]["phase"], "daily_limit")
        self.assertTrue(state["enabled"])
        self.assertEqual(len(state["cycle_trades"]), 4)
        self.assertLessEqual(dec(state["cycle_state"]["daily_volume"]["volume"]), 1000)
        return progress

    def test_cap_wait_survives_restart_and_resumes_at_utc_midnight(self):
        now = datetime(2026, 9, 14, 12, tzinfo=timezone.utc).timestamp()
        with patch("trading.engine.time.time", return_value=now):
            self.capped_round()
            before = deepcopy(self.f.broker.state["orders"])
            daily = self.f.store.cycle_daily_volume("test")
            self.engine = Engine(self.f.store, market=self.f.market)
            self.engine.brokers["test"] = self.f.broker
            seed_cycle_capacity(self.engine)
            self.engine.tick_account("test")
            self.assertEqual(before, self.f.broker.state["orders"])
            self.assertEqual(self.engine.state()["accounts"][0]["cycle_state"]["phase"], "daily_limit")
        with patch("trading.engine.time.time", return_value=daily["next_reset_at"]):
            self.run_until_holding()
            state = self.engine.state()["accounts"][0]
            self.assertEqual(state["cycle_state"]["phase"], "holding")
            current = state["cycle_state"]["daily_volume"]
            self.assertNotEqual(current["utc_date"], daily["utc_date"])
            self.assertEqual(current["trade_count"], 2)
            self.assertGreater(dec(state["cycle_state"]["rolling_volume"]["volume"]), dec(daily["volume"]))
            self.assertTrue(state["enabled"])
            self.assertNotEqual(before, self.f.broker.state["orders"])

    @patch("trading.engine.time.time", new=lambda: 1789387200.0)
    def test_manual_pause_survives_utc_midnight(self):
        # Public capacity has a one-second lifetime; freeze the clock while
        # testing pause/quota transitions independently of machine load.
        self.capped_round()
        self.engine.enable("test", False)
        before = deepcopy(self.f.broker.state["orders"])
        midnight = self.f.store.cycle_daily_volume("test")["next_reset_at"]
        with patch("trading.engine.time.time", return_value=midnight + 1):
            self.engine.tick_account("test")
            state = self.engine.state()["accounts"][0]
            self.assertEqual(state["cycle_state"]["phase"], "paused")
            self.assertFalse(state["enabled"])
            self.assertEqual(state["cycle_state"]["daily_volume"]["volume"], "0")
        self.assertEqual(before, self.f.broker.state["orders"])

    def test_price_move_can_exceed_reserve_without_stranding_close(self):
        self.select(daily_volume_limit="1000", hold_seconds=1)
        self.engine.enable("test", True)
        self.run_until_holding()
        self.expire_hold()
        book = self.f.market.book("XAUUSD1")
        depth = self.f.market.depth("XAUUSD1")
        higher_book = replace(book, bid=book.bid * 2, ask=book.ask * 2, mark=book.mark * 2)
        higher_depth = replace(depth, bids=tuple((p * 2, q) for p, q in depth.bids),
                                asks=tuple((p * 2, q) for p, q in depth.asks))
        with patch.object(self.f.market, "book", return_value=higher_book), \
             patch.object(self.f.market, "depth", return_value=higher_depth):
            self.engine.tick_account("test")
        self.assertEqual(self.f.store.get("cycle:test")["completed_cycles"], 1)
        self.assertGreater(dec(self.f.store.cycle_daily_volume("test")["volume"]), 1000)
        before = deepcopy(self.f.broker.state["orders"])
        self.engine.tick_account("test")
        state = self.engine.state()["accounts"][0]
        self.assertEqual(state["cycle_state"]["phase"], "daily_limit")
        self.assertTrue(state["cycle_state"]["daily_volume"]["reached"])
        self.assertEqual(state["cycle_state"]["daily_volume"]["remaining"], "0")
        self.assertEqual(before, self.f.broker.state["orders"])

    def test_fills_straddling_midnight_count_on_their_actual_utc_day(self):
        before = datetime(2026, 9, 14, 23, 59, 55, tzinfo=timezone.utc).timestamp()
        self.select(daily_volume_limit="1000", hold_seconds=1)
        self.engine.enable("test", True)
        with patch("trading.engine.time.time", return_value=before):
            self.run_until_holding()
            first = self.f.store.cycle_daily_volume("test")
        with patch("trading.engine.time.time", return_value=before + 10):
            self.engine.tick_account("test")
            second = self.f.store.cycle_daily_volume("test")
            rows = self.engine.state()["accounts"][0]["cycle_trades"]
        self.assertEqual((first["utc_date"], second["utc_date"]), ("2026-09-14", "2026-09-15"))
        self.assertEqual((first["trade_count"], second["trade_count"]), (2, 2))
        self.assertEqual({r["utc_date"] for r in rows}, {"2026-09-14", "2026-09-15"})
        for daily in (first, second):
            day_rows = [r for r in rows if r["utc_date"] == daily["utc_date"]]
            self.assertEqual(max(dec(r["daily_volume"]) for r in day_rows), dec(daily["volume"]))

    def test_upgrade_preserves_legacy_hold_config_and_timer(self):
        self.select()
        self.engine.enable("test", True)
        original = self.run_until_holding()
        legacy = deepcopy(original)
        legacy["config"].pop("daily_volume_limit")
        self.f.store.put("cycle:test", legacy)
        self.engine.tick_account("test")
        current = self.f.store.get("cycle:test")
        self.assertEqual(current["run_id"], original["run_id"])
        self.assertEqual(current["opened_at"], original["opened_at"])
        self.assertTrue(self.f.store.account("test")["enabled"])
        self.assertEqual(current["config"]["daily_volume_limit"], "0")

    @patch("trading.engine.time.time", new=lambda: 1789387200.0)
    def test_history_sync_failure_does_not_block_close_but_blocks_next_open(self):
        self.select()
        self.engine.enable("test", True)
        with patch("trading.cycle_execution.CycleExecutor.sync_volume", return_value=False):
            self.run_until_holding()
            self.expire_hold()
            self.engine.tick_account("test")
            self.assertEqual(self.f.store.get("cycle:test")["completed_cycles"], 1)
            before = deepcopy(self.f.broker.state["orders"])
            self.engine.tick_account("test")
            self.assertEqual(before, self.f.broker.state["orders"])
            self.assertTrue(self.engine.state()["accounts"][0]["cycle_state"]["daily_volume"]["sync_pending"])
        self.engine.tick_account("test")
        self.assertEqual(self.f.store.get("cycle:test")["phase"], "holding")

    def test_delayed_previous_day_history_keeps_backfilling_after_reset(self):
        before = datetime(2026, 9, 14, 23, 59, 40, tzinfo=timezone.utc).timestamp()
        self.select(daily_volume_limit="1000", hold_seconds=1)
        self.engine.enable("test", True)
        with patch("trading.engine.time.time", return_value=before), \
             patch("trading.cycle_execution.CycleExecutor.sync_volume", return_value=False):
            self.run_until_holding()
            self.expire_hold()
            self.engine.tick_account("test")
            self.assertEqual(self.f.store.cycle_daily_volume("test")["trade_count"], 0)
        with patch("trading.engine.time.time", return_value=before + 30):
            seed_cycle_capacity(self.engine)
            self.engine.tick_account("test")
            state = self.engine.state()["accounts"][0]["cycle_state"]
            self.assertEqual(state["phase"], "holding")
            self.assertEqual(state["daily_volume"]["trade_count"], 2)
            self.assertEqual(state["rolling_volume"]["trade_count"], 6)
            self.assertEqual(self.f.store.cycle_daily_volume("test", now=before)["trade_count"], 4)
            self.assertEqual(self.f.store.cycle_volume_backlog("test", since=0), [])


class DailyQuotaSettingsTests(TestCase):
    setUp = engine_cases.CycleSettingsTests.setUp

    def test_api_daily_cap_roundtrip_and_invalid_updates_are_atomic(self):
        response = self.client.patch("/api/accounts/test", json={"cycle": {"daily_volume_limit": "40000"}})
        self.assertEqual(response.status_code, 200, response.text)
        original = self.f.store.account("test")
        self.assertEqual(original["cycle"]["daily_volume_limit"], "40000")
        for value in (None, True, 40000, "-1", "NaN", "1000000000001"):
            with self.subTest(value=value):
                response = self.client.patch("/api/accounts/test", json={"cycle": {"daily_volume_limit": value}})
                self.assertIn(response.status_code, (409, 422))
                self.assertEqual(self.f.store.account("test"), original)
