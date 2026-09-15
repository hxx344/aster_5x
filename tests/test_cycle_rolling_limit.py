"""Combined daily/rolling quota sizing and real engine recovery boundaries."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import localcontext
from fractions import Fraction
from unittest import TestCase
from unittest.mock import patch

from tests import test_cycle_daily_limit as daily_cases, test_cycle_planning as planning_cases
from trading.cycle import DailyVolumeLimitError, RollingVolumeLimitError
from trading.models import TradingError, dec
from tests.helpers import seed_cycle_capacity


class RollingQuotaPlanningTests(TestCase):
    setUp = planning_cases.CyclePlanningTests.setUp
    plan = planning_cases.CyclePlanningTests.plan
    hold = planning_cases.CyclePlanningTests.hold

    def test_both_allowances_limit_the_reserved_roundtrip_with_exact_boundary(self):
        for daily, rolling in (("400.4", "1000"), ("1000", "400.4")):
            with self.subTest(daily=daily, rolling=rolling), localcontext() as context:
                context.prec = 6
                plan = self.plan(daily_remaining=daily, rolling_remaining=rolling)
                self.assertEqual(plan.qty, dec("1.001"))
                self.assertEqual(2 * (Fraction(plan.long_notional) + Fraction(plan.short_notional)), Fraction("400.4"))
        self.assertEqual(self.plan(daily_remaining="1000", rolling_remaining="400.39999999999999999999999").qty, dec("1"))

    def test_minimum_round_waits_for_the_tighter_window(self):
        with self.assertRaises(RollingVolumeLimitError):
            self.plan(daily_remaining="1000", rolling_remaining="19.999")
        with self.assertRaises(DailyVolumeLimitError) as caught:
            self.plan(daily_remaining="19.999", rolling_remaining="1000")
        self.assertNotIsInstance(caught.exception, RollingVolumeLimitError)
        self.account["cycle"]["min_notional"] = "100"
        with self.assertRaises(RollingVolumeLimitError):
            self.plan(daily_remaining="1000", rolling_remaining="399.9")
        self.account["cycle"].update(min_notional="0", max_notional="4.9")
        with self.assertRaises(TradingError) as caught:
            self.plan(daily_remaining="1000", rolling_remaining="0")
        self.assertNotIsInstance(caught.exception, DailyVolumeLimitError)

    def test_closing_ignores_both_exhausted_windows(self):
        self.hold()
        self.assertEqual(self.plan(daily_remaining="0", rolling_remaining="0").phase, "close")


class RollingQuotaEngineTests(TestCase):
    setUp = daily_cases.DailyQuotaEngineTests.setUp
    select = daily_cases.DailyQuotaEngineTests.select
    expire_hold = daily_cases.DailyQuotaEngineTests.expire_hold
    run_until_holding = daily_cases.DailyQuotaEngineTests.run_until_holding

    def test_today_open_sizes_against_yesterdays_unexpired_volume(self):
        first_at = datetime(2026, 9, 14, 23, 59, 40, tzinfo=timezone.utc).timestamp()
        self.select(daily_volume_limit="600", max_notional="100", hold_seconds=1)
        self.engine.enable("test", True)
        with patch("trading.engine.time.time", return_value=first_at):
            first = self.run_until_holding()
            self.expire_hold()
            self.engine.tick_account("test")
            yesterday = self.f.store.cycle_daily_volume("test")
        with patch("trading.engine.time.time", return_value=first_at + 30):
            second = self.run_until_holding()
            current = self.engine.state()["accounts"][0]["cycle_state"]
            self.assertLess(dec(second["quantities"]["LONG"]), dec(first["quantities"]["LONG"]))
            self.assertEqual(current["daily_volume"]["trade_count"], 2)
            today_open = Fraction(dec(current["daily_volume"]["volume"]))
            self.assertLessEqual(2 * today_open, 600 - Fraction(dec(yesterday["volume"])))
            self.assertEqual(Fraction(dec(current["rolling_volume"]["volume"])),
                             Fraction(dec(yesterday["volume"])) + today_open)
            self.assertEqual(current["daily_volume"]["effective_remaining"], current["rolling_volume"]["remaining"])

    def test_partial_window_expiry_reopens_a_smaller_round_at_exact_24_hours(self):
        opened = datetime(2026, 9, 14, 12, tzinfo=timezone.utc).timestamp()
        self.select(daily_volume_limit="1000", hold_seconds=60)
        self.engine.enable("test", True)
        with patch("trading.engine.time.time", return_value=opened):
            first = self.run_until_holding()
        with patch("trading.engine.time.time", return_value=opened + 60):
            self.engine.tick_account("test")
        before = deepcopy(self.f.broker.state["orders"])
        with patch("trading.engine.time.time", return_value=opened + 86400 - 0.001):
            seed_cycle_capacity(self.engine)
            self.engine.tick_account("test")
            state = self.engine.state()["accounts"][0]["cycle_state"]
            self.assertEqual(state["phase"], "rolling_limit")
            self.assertEqual(state["rolling_volume"]["next_release_at"], opened + 86400)
            self.assertEqual(before, self.f.broker.state["orders"])
        with patch("trading.engine.time.time", return_value=opened + 86400):
            second = self.run_until_holding()
            state = self.engine.state()["accounts"][0]["cycle_state"]
            self.assertGreater(dec(second["quantities"]["LONG"]), 0)
            self.assertLess(dec(second["quantities"]["LONG"]), dec(first["quantities"]["LONG"]))
            self.assertEqual(state["rolling_volume"]["trade_count"], 4)
            self.assertEqual(state["rolling_volume"]["next_release_at"], opened + 86460)

    def test_manual_pause_does_not_resume_when_rolling_volume_expires(self):
        now = datetime(2026, 9, 14, 12, tzinfo=timezone.utc).timestamp()
        self.select(daily_volume_limit="1000", hold_seconds=1)
        self.engine.enable("test", True)
        with patch("trading.engine.time.time", return_value=now):
            self.run_until_holding()
            self.expire_hold()
            self.engine.tick_account("test")
            self.engine.enable("test", False)
        before = deepcopy(self.f.broker.state["orders"])
        with patch("trading.engine.time.time", return_value=now + 86400):
            self.engine.tick_account("test")
            state = self.engine.state()["accounts"][0]
            self.assertFalse(state["enabled"])
            self.assertEqual(state["cycle_state"]["phase"], "paused")
            self.assertEqual(state["cycle_state"]["rolling_volume"]["volume"], "0")
            self.assertIsNone(state["cycle_state"]["rolling_volume"]["next_release_at"])
            self.assertEqual(before, self.f.broker.state["orders"])

    def test_yesterday_unresolved_fills_block_new_open_even_after_daily_reset(self):
        now = datetime(2026, 9, 14, 23, 59, 40, tzinfo=timezone.utc).timestamp()
        self.select(daily_volume_limit="1000", hold_seconds=1)
        self.engine.enable("test", True)
        with patch("trading.cycle_execution.CycleExecutor.sync_volume", return_value=False):
            with patch("trading.engine.time.time", return_value=now):
                self.run_until_holding()
                self.expire_hold()
                self.engine.tick_account("test")
            before = deepcopy(self.f.broker.state["orders"])
            with patch("trading.engine.time.time", return_value=now + 30):
                self.engine.tick_account("test")
                state = self.engine.state()["accounts"][0]["cycle_state"]
                self.assertTrue(state["rolling_volume"]["sync_pending"])
                self.assertEqual(before, self.f.broker.state["orders"])
                self.assertEqual(state["daily_volume"]["volume"], "0")
