"""Unified monitoring controls: durable policy, actual I/O and delivery boundaries."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from trading import monitoring
from trading.engine import Engine
from trading.listings import ListingMonitor
from trading.models import SYMBOLS, TradingError
from trading.server import create_app
from trading.store import Store
from tests.helpers import Fixture
from tests.test_listings import symbol, detail


def catalog(*symbols):
    return {"initialized": True, "checked_at": time.time(), "rows": {name: {
        "symbol": name, "status": "TRADING", "seen_trading": True, "notification_phase": "baseline", **detail("2")}
        for name in symbols}}


class MonitoringSettingsTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.store = self.f.store
        self.store.save_listing_state(catalog("BTCUSD1", "ETHUSD1", "XAUUSD1"))
        self.engine = Engine(self.store, market=self.f.market)
        self.addCleanup(self.engine.dashboard_reports.close)

    def queue(self, category, symbols, message_id=None):
        import json
        message_id = message_id or category
        with self.store.connect() as db:
            db.execute("INSERT INTO outbox(id,message,due_at,category,symbols) VALUES (?,?,0,?,?)",
                       (message_id, message_id, category, json.dumps(symbols)))
        return message_id

    def pause(self):
        owner = self.store.account("test")
        owner["enabled"] = False
        self.store.save_account(owner)

    def test_defaults_restart_and_watch_compatibility(self):
        self.assertEqual({key: self.store.monitoring_settings()[key] for key in monitoring.DEFAULTS}, monitoring.DEFAULTS)
        self.store.set_listing_watch("BTCUSD1", True)
        self.store.edit_monitoring({"feishu_enabled": False})
        self.store.edit_monitoring({"monitor": False, "alerts": False}, symbol="ETHUSD1")
        restarted = Store(self.store.path)
        self.assertFalse(restarted.monitoring_settings()["feishu_enabled"])
        self.assertEqual(restarted.listing_watch_symbols(), ["BTCUSD1"])
        self.assertEqual(restarted.monitoring_settings()["symbols"]["ETHUSD1"], {"monitor": False, "alerts": False})

    def test_new_default_only_changes_future_symbols(self):
        self.store.edit_monitoring({"auto_monitor_new": False})
        self.store.save_listing_state(catalog("BTCUSD1", "ETHUSD1", "NEWUSD1"))
        config = self.store.monitoring_settings()
        self.assertTrue(monitoring.monitored(config, "BTCUSD1"))
        self.assertFalse(monitoring.monitored(config, "NEWUSD1"))
        self.store.edit_monitoring({"auto_monitor_new": True})
        self.assertFalse(monitoring.monitored(self.store.monitoring_settings(), "NEWUSD1"))

    def test_concurrent_field_updates_preserve_each_other(self):
        with ThreadPoolExecutor(2) as pool:
            tasks = [pool.submit(Store(self.store.path).edit_monitoring, change, symbol="BTCUSD1")
                     for change in ({"monitor": False}, {"alerts": False})]
            for task in tasks:
                task.result()
        self.assertEqual(self.store.monitoring_settings()["symbols"]["BTCUSD1"], {"monitor": False, "alerts": False})

    def test_repeated_saves_do_not_create_a_new_policy_revision(self):
        self.store.edit_monitoring({"feishu_enabled": False})
        config = self.store.monitoring_settings()
        self.assertEqual(self.store.edit_monitoring({"feishu_enabled": False}), config)
        self.store.edit_monitoring({"monitor": False, "max_capacity_alert": True}, symbol="BTCUSD1")
        config = self.store.monitoring_settings()
        self.assertEqual(self.store.edit_monitoring({"monitor": False, "max_capacity_alert": True}, symbol="BTCUSD1"), config)

    def test_unknown_symbol_invalid_field_and_null_are_rejected(self):
        for body in ({}, {"feishu_enabled": 1}, {"webhook": True}, {"feishu_enabled": None}):
            with self.assertRaises(TradingError):
                self.store.edit_monitoring(body)
        with self.assertRaises(TradingError):
            self.store.edit_monitoring({"alerts": False}, symbol="UNKNOWNUSD1")

    def test_all_categories_and_master_cancel_queued_retries_durably(self):
        for category in monitoring.CATEGORIES:
            with self.subTest(category=category):
                self.store.edit_monitoring({category + "_alerts": True, "feishu_enabled": True})
                identity = self.queue(category, ["BTCUSD1"], category + "-queued")
                # Expiring a retry works even when it is not currently due.
                self.store.notification_result({"id": identity}, False)
                self.store.edit_monitoring({category + "_alerts": False})
                self.store.edit_monitoring({category + "_alerts": True})
                with self.store.connect() as db:
                    self.assertEqual(db.execute("SELECT expires_at FROM outbox WHERE id=?", (identity,)).fetchone()[0], 0)
        self.queue("trade_summary", ["BTCUSD1"], "master")
        self.store.edit_monitoring({"feishu_enabled": False})
        self.store.edit_monitoring({"feishu_enabled": True})
        self.assertIsNone(self.store.notification_for_delivery("master"))

    def test_symbol_alert_mute_applies_to_every_category(self):
        for category in monitoring.CATEGORIES:
            self.queue(category, ["BTCUSD1"])
        self.store.edit_monitoring({"alerts": False}, symbol="BTCUSD1")
        self.assertEqual(self.store.pending_notifications(), 0)
        self.assertEqual(self.store.due_notifications(), [])

    def test_monitoring_off_suppresses_public_alerts_but_preserves_trade_alerts(self):
        for category in monitoring.CATEGORIES:
            self.queue(category, ["BTCUSD1"])
        self.store.edit_monitoring({"monitor": False}, symbol="BTCUSD1")
        self.assertEqual([row["id"] for row in self.store.due_notifications()], ["trade_summary"])

    def test_final_delivery_rechecks_changes_during_an_earlier_send(self):
        self.queue("trade_summary", ["XAUUSD1"], "a-first")
        self.queue("trade_summary", ["CLUSD1"], "b-second")
        def sending(*args):
            self.store.edit_monitoring({"feishu_enabled": False})
        with patch.object(self.engine, "notification_config", return_value={"webhook": "test", "cooldown_seconds": 0}), \
             patch("monitor.send_feishu", side_effect=sending) as sender:
            self.engine.notify()
        self.assertEqual(sender.call_count, 1)

    def test_disabled_rows_cannot_starve_enabled_notifications(self):
        config = self.store.monitoring_settings()
        config["new_listing_alerts"] = False
        with self.store.connect() as db:
            monitoring.write(db, config)
        # Include legacy rows inserted by an older process after the save.
        for index in range(8):
            self.queue("new_listing", ["BTCUSD1"], f"a-muted-{index}")
        self.queue("trade_summary", ["XAUUSD1"], "z-allowed")
        self.assertEqual([row["id"] for row in self.store.due_notifications()], ["z-allowed"])

    def test_old_aggregate_is_classified_without_parsing_message(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO outbox(id,message,due_at) VALUES ('legacy','unstructured old text',0)")
        self.assertIsNotNone(self.store.notification_for_delivery("legacy"))
        self.store.edit_monitoring({"alerts": False}, symbol="CLUSD1")
        self.assertIsNone(self.store.notification_for_delivery("legacy"))

    def test_multi_symbol_campaign_is_split_and_local_history_is_complete(self):
        owner = {**self.f.account, "mode": "live"}
        quantities = {"long_qty": "1", "short_qty": "1", "notional": "1000"}
        self.store.put("campaign:test", {"id": "campaign", "batches": [
            {"symbol": name, "leverage": 5, "quantities": quantities} for name in ("XAUUSD1", "CLUSD1")]})
        self.store.finish_campaign(owner, "done", ".1")
        messages = self.store.due_notifications()
        self.assertEqual(len(messages), 2)
        self.store.edit_monitoring({"alerts": False}, symbol="XAUUSD1")
        messages = self.store.due_notifications()
        self.assertEqual(len(messages), 1)
        self.assertIn("CLUSD1", messages[0]["message"])
        self.assertNotIn("XAUUSD1", messages[0]["message"])
        self.assertIn("XAUUSD1", self.store.events()[0]["message"])
        self.assertIn("CLUSD1", self.store.events()[0]["message"])

    def test_disabling_monitoring_really_stops_public_workers(self):
        self.pause()
        self.store.edit_monitoring({"monitoring_enabled": False})
        with patch.object(self.f.market, "capacities") as capacity, patch.object(self.f.market, "book") as book, \
             patch.object(self.f.market, "depth") as depth, patch.object(self.f.market, "refresh_public_brackets", create=True) as brackets:
            for symbol_name in SYMBOLS:
                self.engine.poll_market(symbol_name)
                self.engine.poll_book(symbol_name)
                self.engine.poll_depth(symbol_name)
                self.engine.poll_public_brackets(symbol_name)
        for call in (capacity, book, depth, brackets):
            call.assert_not_called()
        self.store.edit_monitoring({"monitoring_enabled": True})
        self.assertEqual(self.engine.monitored_market_symbols(), set(SYMBOLS))

    def test_active_trading_and_pending_recovery_keep_required_symbols(self):
        self.store.edit_monitoring({"monitoring_enabled": False})
        self.assertEqual(self.engine.monitored_market_symbols(), {"XAUUSD1"})
        self.pause()
        self.assertEqual(self.engine.monitored_market_symbols(), set())
        self.store.save_intent({"id": "unfinished", "account_id": "test", "status": "pending", "kind": "pair", "symbol": "CLUSD1", "orders": []})
        self.assertEqual(self.engine.monitored_market_symbols(), {"CLUSD1"})
        state = self.engine.monitoring_state()
        row = next(row for row in state["symbols"] if row["symbol"] == "CLUSD1")
        self.assertTrue(row["effective_monitor"])
        self.assertTrue(row["required_by"])

    def test_paused_cycle_holding_and_migration_keep_data(self):
        self.pause()
        self.store.edit_monitoring({"monitoring_enabled": False})
        self.store.put("cycle:test", {"phase": "holding", "config": {"symbol": "SPCXUSD1"}})
        self.assertIn("SPCXUSD1", self.engine.monitored_market_symbols())
        owner = self.store.account("test")
        owner["enabled"] = True
        owner["migration"]["enabled"] = True
        self.store.save_account(owner)
        self.assertEqual(self.engine.monitored_market_symbols(), set(SYMBOLS))

    def test_new_listing_requests_obey_symbol_and_master_switches(self):
        market = Mock()
        market.listing_symbols.return_value = {"symbols": [symbol("BTCUSD1"), symbol("ETHUSD1")]}
        market.listing_detail.return_value = detail("2")
        worker = ListingMonitor(self.store, market, threading.Event())
        self.store.edit_monitoring({"monitor": False}, symbol="BTCUSD1")
        worker.poll()
        market.listing_detail.assert_called_once_with("ETHUSD1")
        market.reset_mock()
        self.store.edit_monitoring({"monitoring_enabled": False})
        worker.catalog_due = 0
        worker.detail_due.clear()
        worker.poll()
        market.listing_symbols.assert_not_called()
        market.listing_detail.assert_not_called()

    def test_directory_disabled_keeps_selected_detail_fresh_and_resumes_with_baseline(self):
        self.store.set_listing_watch("BTCUSD1", True)
        old = self.store.get("usd1_listings")
        old["checked_at"] = time.time() - 1000
        self.store.save_listing_state(old)
        self.store.edit_monitoring({"discovery_enabled": False})
        market = Mock()
        market.listing_detail.return_value = detail("2")
        market.listing_symbols.return_value = {"symbols": [symbol("BTCUSD1"), symbol("NEWUSD1")]}
        worker = ListingMonitor(self.store, market, threading.Event())
        worker.poll()
        market.listing_symbols.assert_not_called()
        self.assertTrue(self.store.due_notifications())
        self.store.edit_monitoring({"discovery_enabled": True})
        worker.detail_due.clear()
        worker.poll()
        self.assertFalse(self.store.get("usd1_listings")["rows"]["NEWUSD1"]["is_new"])

    def test_reenabled_watch_creates_fresh_episode_without_reviving_old_retry(self):
        self.store.set_listing_watch("BTCUSD1", True)
        cached = self.store.get("usd1_listings")
        old = self.store.due_notifications()[0]
        self.store.edit_monitoring({"feishu_enabled": False})
        self.store.edit_monitoring({"feishu_enabled": True})
        self.store.save_listing_state(cached)
        self.assertEqual(self.store.due_notifications(), [])
        self.store.save_listing_state(catalog("BTCUSD1"))
        new = self.store.due_notifications()[0]
        self.assertNotEqual(old["id"], new["id"])
        self.store.notification_result(old, True)
        self.assertEqual(self.store.due_notifications()[0]["id"], new["id"])

    def test_change_during_listing_request_cannot_enqueue_old_discovery(self):
        revision = self.store.monitoring_settings()["revision"]
        self.store.edit_monitoring({"feishu_enabled": False})
        self.store.edit_monitoring({"feishu_enabled": True})
        self.store.save_listing_state(catalog("BTCUSD1"), [("usd1-listing:BTCUSD1", "old discovery")], policy_revision=revision)
        self.assertEqual(self.store.due_notifications(), [])

    def test_scheduler_omits_disabled_market_jobs(self):
        from contextlib import ExitStack
        from tests.test_cycle_ws_scheduler import _Harness, _Pool
        harness = _Harness(self.f)
        self.addCleanup(harness.engine.dashboard_reports.close)
        self.pause()
        self.store.edit_monitoring({"monitoring_enabled": False})
        calls = []
        def submit(function, *args, **kwargs):
            calls.append((getattr(function, "__name__", "mock"), args))
            from tests.test_cycle_ws_scheduler import _Future
            return _Future(60)
        with ExitStack() as stack:
            stack.enter_context(patch.object(harness.engine, "scheduling", return_value=harness.timing))
            stack.enter_context(patch.object(_Pool, "submit", side_effect=submit))
            stack.enter_context(patch("trading.engine.ThreadPoolExecutor", return_value=_Pool()))
            harness.control = lambda _: harness.engine.shutdown.set()
            harness.engine.run()
        self.assertFalse(any(args and args[0] in SYMBOLS for _, args in calls))
        self.assertTrue(any(name == "notify" for name, _ in calls))


class MonitoringAPITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "state.sqlite3")
        self.store.save_listing_state(catalog("BTCUSD1"))
        from trading.paper import DemoMarket
        self.engine = Engine(self.store, market=DemoMarket())
        self.addCleanup(self.engine.dashboard_reports.close)
        with patch.dict(os.environ, {"ASTER_DASHBOARD_PASSWORD": "monitor-test-password"}, clear=True):
            app = create_app(self.engine, start_engine=False)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.client.headers["origin"] = "http://testserver"

    def login(self):
        self.assertEqual(self.client.post("/api/login", json={"password": "monitor-test-password"}).status_code, 200)

    def test_auth_origin_strict_validation_and_no_secrets(self):
        self.assertEqual(self.client.get("/api/monitoring").status_code, 401)
        self.assertEqual(self.client.patch("/api/monitoring", json={"feishu_enabled": False}).status_code, 401)
        self.login()
        self.assertEqual(self.client.patch("/api/monitoring", json={"feishu_enabled": False}, headers={"origin": "https://bad.invalid"}).status_code, 403)
        for path, body in (("/api/monitoring", {}), ("/api/monitoring", {"feishu_enabled": "false"}),
                           ("/api/monitoring", {"feishu_enabled": None}), ("/api/monitoring", {"feishu_enabled": 1}),
                           ("/api/monitoring", {"webhook": "test"}), ("/api/monitoring/symbols/BTCUSD1", {"feishu_enabled": False}),
                           ("/api/monitoring/symbols/BTCUSD1", {"monitor": None})):
            self.assertEqual(self.client.patch(path, json=body).status_code, 422, (path, body))
        self.assertEqual(self.client.patch("/api/monitoring/symbols/UNKNOWNUSD1", json={"monitor": True}).status_code, 409)
        with patch.dict(os.environ, {"FEISHU_WEBHOOK_URL": "secret-sentinel"}):
            response = self.client.get("/api/state")
        self.assertNotIn("secret-sentinel", response.text)

    def test_no_account_persistence_legacy_endpoint_and_demo(self):
        self.login()
        self.assertEqual(self.client.patch("/api/monitoring/symbols/BTCUSD1", json={"max_capacity_alert": True}).status_code, 200)
        self.assertEqual(self.store.listing_watch_symbols(), ["BTCUSD1"])
        self.assertEqual(self.client.patch("/api/listings/BTCUSD1/capacity-alert", json={"enabled": False}).status_code, 200)
        state = self.client.get("/api/state").json()
        self.assertEqual(state["accounts"], [])
        self.assertFalse(next(row for row in state["monitoring"]["symbols"] if row["symbol"] == "BTCUSD1")["max_capacity_alert"])
        self.engine.demo = True
        self.assertEqual(self.client.patch("/api/monitoring", json={"feishu_enabled": True}).status_code, 409)


if __name__ == "__main__":
    unittest.main()
