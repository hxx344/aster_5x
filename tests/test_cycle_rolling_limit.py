"""Rolling reports never constrain the UTC-day opening allowance."""
from copy import deepcopy
from datetime import datetime, timezone
from fractions import Fraction
from unittest import TestCase
from unittest.mock import patch

from tests import test_cycle_daily_limit as daily_cases
from trading.models import dec
from trading.cycle_projection import project_cycle_state


class RollingQuotaEngineTests(TestCase):
    setUp = daily_cases.DailyQuotaEngineTests.setUp
    select = daily_cases.DailyQuotaEngineTests.select
    expire_hold = daily_cases.DailyQuotaEngineTests.expire_hold
    run_until_holding = daily_cases.DailyQuotaEngineTests.run_until_holding

    def test_today_open_ignores_yesterdays_unexpired_volume(self):
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
            self.assertEqual(dec(second["quantities"]["LONG"]), dec(first["quantities"]["LONG"]))
            self.assertEqual(current["daily_volume"]["trade_count"], 2)
            today_open = Fraction(dec(current["daily_volume"]["volume"]))
            self.assertLessEqual(2 * today_open, 600)
            self.assertEqual(Fraction(dec(current["rolling_volume"]["volume"])),
                             Fraction(dec(yesterday["volume"])) + today_open)
            self.assertEqual(current["daily_volume"]["effective_remaining"], current["daily_volume"]["remaining"])
            self.assertEqual((current["rolling_volume"]["limit"], current["rolling_volume"]["remaining"],
                              current["rolling_volume"]["reached"]), ("0", None, False))

    def test_legacy_rolling_wait_is_cleared_even_before_background_report_arrives(self):
        account = {"enabled": True, "cycle": {"enabled": True}}
        saved = {"phase": "rolling_limit", "reason": "old quota", "diagnostic": {"code": "old"}}
        for background in (False, True):
            state = project_cycle_state(account, saved, {}, None,
                {"reached": False, "remaining": "1000", "utc_date": "2026-09-15"},
                {"reached": True, "volume": "1000"}, background_reports=background)
            self.assertEqual(state["phase"], "waiting_open")
            self.assertIsNone(state["diagnostic"])
        account["enabled"] = False
        self.assertEqual(project_cycle_state(account, saved, {}, None, None, None,
                         background_reports=True)["phase"], "paused")

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

    def test_yesterday_completed_fills_pending_sync_do_not_block_today(self):
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
                saved = self.f.store.account("test")
                with patch.object(self.f.store, "cycle_rolling_volume", side_effect=AssertionError("statistics only")):
                    self.assertEqual(self.engine.cycle_open_allowances(saved), {"daily_remaining": "1000"})
                self.run_until_holding()
                state = self.engine.state()["accounts"][0]["cycle_state"]
                self.assertTrue(state["rolling_volume"]["sync_pending"])
                self.assertTrue(state["daily_volume"]["sync_pending"])
                self.assertNotEqual(before, self.f.broker.state["orders"])
                self.assertEqual(state["daily_volume"]["volume"], "0")
