"""Cycle failure details reach the UI without changing execution or leaking state."""
from copy import deepcopy
from dataclasses import replace
from fractions import Fraction
import os
import time
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests import test_cycle_engine as engine_cases
from tests.helpers import account, seed_cycle_capacity
from trading.cycle import DailyVolumeLimitError
from trading.models import TradingError
from trading.server import create_app


class CycleDiagnosticStateTests(TestCase):
    setUp = engine_cases.CycleEngineTests.setUp
    select = engine_cases.CycleEngineTests.select

    def start(self):
        self.select()
        self.engine.enable("test", True)

    def spread_failure(self, now):
        seed_cycle_capacity(self.engine, now=now)
        original = self.f.market.depth
        def wide(symbol):
            depth = original(symbol)
            return replace(depth, asks=tuple((price * Fraction("1.01"), quantity) for price, quantity in depth.asks))
        with patch("trading.engine.time.time", return_value=now), patch.object(self.f.market, "depth", side_effect=wide):
            for _ in range(5):
                self.engine.tick_account("test")
                result = self.engine.state()["accounts"][0]
                if result["cycle_state"].get("diagnostic"):
                    return result
        self.fail(str(self.engine.views))

    def test_numeric_failure_is_recorded_and_repeated_check_updates_time_without_duplicate_event(self):
        self.start()
        now = time.time()
        first = self.spread_failure(now)
        diagnostic = first["cycle_state"]["diagnostic"]
        self.assertEqual((diagnostic["symbol"], diagnostic["phase"], diagnostic["checked_at"]), ("XAUUSD1", "open", now))
        failures = [check for check in diagnostic["checks"] if check["passed"] is False]
        self.assertTrue(failures)
        self.assertTrue(any(check["unit"] == "bp" for check in failures))
        self.assertTrue(all(isinstance(check["actual"], str) and isinstance(check["required"], str) for check in failures))
        self.assertIn("bp", first["reason"])
        self.assertEqual(self.f.broker.state["orders"], {})
        before_events = deepcopy(self.f.store.events())
        second = self.spread_failure(now + 16)
        self.assertEqual(second["cycle_state"]["diagnostic"]["checked_at"], now + 16)
        self.assertEqual(first["reason"], second["reason"])
        after_events = self.f.store.events()
        self.assertEqual(len(after_events), len(before_events))
        before_check = next(event for event in before_events if event["kind"] == "cycle_check")
        after_check = next(event for event in after_events if event["kind"] == "cycle_check")
        self.assertEqual(after_check["id"], before_check["id"])
        self.assertEqual(after_check["cycle_check"]["count"], before_check["cycle_check"]["count"] + 1)
        self.assertEqual(after_check["cycle_check"]["diagnostic"], second["cycle_state"]["diagnostic"])
        self.assertTrue(second["enabled"])

    def test_successful_open_and_manual_pause_clear_previous_failure_details(self):
        self.start()
        self.spread_failure(time.time())
        self.engine.enable("test", False)
        self.assertIsNone(self.engine.state()["accounts"][0]["cycle_state"]["diagnostic"])
        self.engine.enable("test", True)
        self.spread_failure(time.time())
        for _ in range(5):
            self.engine.tick_account("test")
            if self.f.store.get("cycle:test")["phase"] == "holding":
                break
        current = self.engine.state()["accounts"][0]["cycle_state"]
        self.assertEqual(current["phase"], "holding")
        self.assertIsNone(current["diagnostic"])

    def test_new_unstructured_failure_clears_old_market_numbers(self):
        self.start()
        self.spread_failure(time.time())
        with patch.object(self.f.broker, "cycle_snapshot", side_effect=TradingError("账户快照读取失败")):
            self.engine.tick_account("test")
        current = self.engine.state()["accounts"][0]
        self.assertEqual(current["reason"], "账户快照读取失败")
        self.assertIsNone(current["cycle_state"]["diagnostic"])

    def test_immediate_pause_resume_does_not_resurrect_diagnostic_before_next_check(self):
        self.start()
        first = self.spread_failure(time.time())
        self.assertIsNotNone(first["cycle_state"]["diagnostic"])
        self.engine.enable("test", False)
        self.assertIsNone(self.engine.views["test"]["cycle_state"]["diagnostic"])
        self.engine.enable("test", True)
        current = self.engine.state()["accounts"][0]
        self.assertTrue(current["enabled"])
        self.assertIsNone(current["cycle_state"]["diagnostic"])
        self.assertEqual(self.f.broker.state["orders"], {})
        self.assertIsNotNone(self.spread_failure(time.time())["cycle_state"]["diagnostic"])

    def test_authenticated_api_has_account_isolation_and_server_check_timestamp(self):
        self.start()
        checked_at = time.time()
        first = self.spread_failure(checked_at)
        self.f.store.save_account(account("second"))
        with patch.dict(os.environ, {"ASTER_DASHBOARD_PASSWORD": "test-only-diagnostic-password"}, clear=True):
            client = TestClient(create_app(self.engine, start_engine=False))
            self.addCleanup(client.close)
            self.addCleanup(self.engine.dashboard_reports.close)
            client.headers["origin"] = "http://testserver"
            self.assertEqual(client.get("/api/state").status_code, 401)
            self.assertEqual(client.post("/api/login", json={"password": "test-only-diagnostic-password"}).status_code, 200)
            response = client.get("/api/state")
        self.assertEqual(response.status_code, 200)
        result = {row["id"]: row for row in response.json()["accounts"]}
        self.assertEqual(result["test"]["cycle_state"]["diagnostic"], first["cycle_state"]["diagnostic"])
        self.assertIsNone(result["second"]["cycle_state"].get("diagnostic"))
        checks = [event for event in response.json()["events"] if event["kind"] == "cycle_check"]
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["account_id"], "test")
        self.assertEqual(checks[0]["cycle_check"]["diagnostic"], first["cycle_state"]["diagnostic"])

    def test_reached_daily_limit_details_exclude_rolling_statistics(self):
        self.select(daily_volume_limit="100")
        saved = self.f.store.account("test")
        daily = self.engine.cycle_daily_allowance(saved)
        daily.update(volume="100", quota_volume="100", remaining="0", reached=True)
        with patch.object(self.engine, "cycle_daily_allowance", return_value=daily), \
             patch.object(self.f.store, "cycle_rolling_volume", side_effect=AssertionError("statistics only")):
            with self.assertRaises(DailyVolumeLimitError) as caught:
                self.engine.cycle_open_allowances(saved)
        diagnostic = caught.exception.diagnostic
        self.assertEqual([check["actual"] for check in diagnostic["checks"]], ["100"])
        self.assertTrue(all(check["required"] == "< 100" and check["passed"] is False for check in diagnostic["checks"]))
        self.assertEqual(diagnostic["code"], "daily_volume_limit")
