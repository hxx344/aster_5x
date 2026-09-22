"""Portal summaries stay small and never initiate exchange/history work."""
from datetime import datetime, timezone
import time
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.helpers import Fixture, account
from trading.engine import Engine
from trading.hub_summary import hub_summary
from trading.server import create_app


class HubSummaryTests(TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.engine = Engine(self.f.store, market=self.f.market)
        self.addCleanup(self.engine.dashboard_reports.close)
        self.engine.ready, self.engine.error = True, None
        self.now = time.time()

    def live(self, aid="test", *, enabled=True, margin="12.5", stamp=None, volume="200"):
        saved = account(aid, "live")
        saved["enabled"] = enabled
        self.f.store.save_account(saved)
        saved = self.f.store.account(aid)
        self.engine.view(aid, snapshot={"timestamp": self.now if stamp is None else stamp,
                                       "occupied_margin": margin})
        self.engine.dashboard_reports.entries[aid] = {
            "key": self.engine.dashboard_reports.key(saved), "as_of": self.now, "error": None,
            "data": {"volumes": {saved["cycle"]["symbol"]: {"daily_volume": {
                "volume": volume, "utc_date": datetime.fromtimestamp(self.now, timezone.utc).date().isoformat()}}}},
        }

    def metrics(self, result):
        return {item["key"]: item["value"] for item in result["data"]["metrics"]}

    def test_uses_only_enabled_live_accounts_and_matches_units_without_heavy_reads(self):
        self.live()
        self.live("disabled", enabled=False, margin="999999", volume="999999")
        self.f.store.save_account(account("paper"))
        with patch.object(self.engine, "state", side_effect=AssertionError("full state")), \
             patch.object(self.engine.dashboard_reports, "read", side_effect=AssertionError("start reports")), \
             patch.object(self.engine, "_load_dashboard_report", side_effect=AssertionError("history")), \
             patch.object(self.f.market, "book", side_effect=AssertionError("market request")):
            result = hub_summary(self.engine, now=self.now)
        self.assertEqual(self.metrics(result), {"accounts": 2, "live_accounts": 1, "occupied_margin": 12.5, "daily_volume": 200})
        self.assertEqual(result["data"]["health"]["state"], "online")
        self.assertEqual(result["data"]["health"]["staleAfterSeconds"], 120)
        self.assertEqual([m["unit"] for m in result["data"]["metrics"]][-2:], ["USD1", "USD1"])
        self.assertIsNone(self.engine.dashboard_reports.worker)

    def test_missing_snapshot_and_report_are_null_partial_not_zero(self):
        self.live()
        self.engine.views.clear()
        self.engine.dashboard_reports.entries.clear()
        result = hub_summary(self.engine, now=self.now)
        self.assertEqual(result["data"]["health"]["state"], "partial")
        self.assertIsNone(result["data"]["updatedAt"])
        self.assertIsNone(self.metrics(result)["occupied_margin"])
        self.assertIsNone(self.metrics(result)["daily_volume"])

    def test_oldest_published_snapshot_controls_staleness(self):
        self.live(stamp=self.now - 121)
        self.live("second", stamp=self.now - 10)
        result = hub_summary(self.engine, now=self.now)
        expected = datetime.fromtimestamp(self.now - 121, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        self.assertEqual(result["data"]["updatedAt"], expected)
        self.assertEqual(result["data"]["health"]["state"], "stale")
        self.engine.display_snapshots["test"] = {"timestamp": self.now, "occupied_margin": "20"}
        result = hub_summary(self.engine, now=self.now)
        self.assertEqual(result["data"]["health"]["state"], "online")
        self.assertEqual(self.metrics(result)["occupied_margin"], 32.5)

    def test_utc_rollover_expired_failed_and_config_changed_reports_are_not_ready(self):
        self.live()
        for changes in ({"as_of": self.now - 16}, {"error": "unavailable"}, {"key": ("different", "0")}):
            with self.subTest(changes=changes):
                self.live()
                self.engine.dashboard_reports.entries["test"].update(changes)
                result = hub_summary(self.engine, now=self.now)
                self.assertIsNone(self.metrics(result)["daily_volume"])
                self.assertEqual(result["data"]["health"]["state"], "partial")
        self.live()
        next_midnight = (int(self.now // 86400) + 1) * 86400
        result = hub_summary(self.engine, now=next_midnight)
        self.assertIsNone(self.metrics(result)["daily_volume"])

    def test_demo_no_live_accounts_does_not_claim_zero_private_balances(self):
        self.engine.demo = True
        result = hub_summary(self.engine, now=self.now)
        self.assertEqual(self.metrics(result)["live_accounts"], 0)
        self.assertIsNone(self.metrics(result)["occupied_margin"])
        self.assertIsNone(self.metrics(result)["daily_volume"])
        self.assertIn("演示", result["data"]["health"]["message"])
        self.engine.shutdown.set()
        self.assertEqual(hub_summary(self.engine)["data"]["health"]["state"], "offline")

    def test_previous_utc_day_is_rejected_even_with_a_two_second_old_report(self):
        self.now = (int(self.now // 86400) + 1) * 86400 - 1
        self.live()
        result = hub_summary(self.engine, now=self.now + 2)
        self.assertIsNone(self.metrics(result)["daily_volume"])
        self.assertEqual(result["data"]["health"]["state"], "partial")

    def test_missing_one_account_timestamp_does_not_borrow_another_accounts_freshness(self):
        self.live()
        self.live("second")
        self.engine.views["second"]["snapshot"].pop("timestamp")
        result = hub_summary(self.engine, now=self.now)
        self.assertIsNone(result["data"]["updatedAt"])
        self.assertEqual(result["data"]["health"]["state"], "partial")

    def test_endpoint_shares_authentication_and_no_store_with_state(self):
        self.live()
        with patch.dict("os.environ", {"ASTER_DASHBOARD_PASSWORD": "test-only-summary-password"}), \
             TestClient(create_app(self.engine, start_engine=False)) as client:
            self.assertEqual(client.get("/api/hub/summary?schemaVersion=2").status_code, 401)
            client.headers["origin"] = "http://testserver"
            self.assertEqual(client.post("/api/login", json={"password": "test-only-summary-password"}).status_code, 200)
            with patch.object(self.engine, "state", side_effect=AssertionError("full state")):
                result = client.get("/api/hub/summary?schemaVersion=2")
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["schemaVersion"], 2)
            self.assertEqual(result.headers["cache-control"], "no-store")
            self.assertEqual(client.get("/api/hub/summary?schemaVersion=3").status_code, 422)
            client.post("/api/logout")
            self.assertEqual(client.get("/api/hub/summary?schemaVersion=2").status_code, 401)
