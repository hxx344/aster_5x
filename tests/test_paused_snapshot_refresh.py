"""Pausing trading must not skip scheduled account reads."""
from copy import deepcopy
from dataclasses import replace
import os
import threading
import time
from unittest import TestCase
from unittest.mock import patch

from tests.helpers import Fixture
from tests.test_exchange_hardening import FixtureAPI, account_responses
from tests.test_account_market_assets import OTHER, list_other, market_and_api
from trading.engine import Engine, snapshot_json
from trading.exchange import LiveBroker, RateBudget
from trading.models import TradingError


class PausedSnapshotRefreshTests(TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.f.store.save_account({**self.f.account, "enabled": False})
        self.engine = Engine(self.f.store, market=self.f.market)
        self.addCleanup(self.engine.dashboard_reports.close)
        self.engine.brokers["test"] = self.f.broker

    def configure(self, cycle_enabled, ordinary_symbol):
        account = self.f.store.account("test")
        account["policy"]["symbols"] = ["XAUUSD1", "SPCXUSD1"]
        self.f.store.save_account(account)
        self.engine.configure("test", {
            "cycle": {"enabled": cycle_enabled, "symbol": "XAUUSD1"},
            "ordinary_symbol": ordinary_symbol,
        })
        symbols = ["XAUUSD1", "SPCXUSD1"]
        old = replace(self.f.broker.snapshot(symbols), timestamp=time.time() - 780)
        self.engine.view("test", snapshot=snapshot_json(old, symbols))
        return old

    def test_paused_accounts_refresh_with_cycle_only_mixed_or_ordinary_configuration(self):
        for enabled, selected in ((True, "XAUUSD1"), (True, "SPCXUSD1"), (False, "XAUUSD1")):
            with self.subTest(cycle=enabled, ordinary=selected):
                old = self.configure(enabled, selected)
                progress = {"phase": "holding", "opened_at": time.time() - 900, "marker": "keep"}
                self.f.store.put("cycle:test", progress)
                with patch.object(self.f.broker, "snapshot", wraps=self.f.broker.snapshot) as read, \
                     patch.object(self.f.broker, "submit", side_effect=AssertionError("paused account must not trade")), \
                     patch.object(self.f.broker, "set_leverage", side_effect=AssertionError("paused account must not change leverage")):
                    self.engine.tick_account("test")
                read.assert_called_once_with(["XAUUSD1", "SPCXUSD1"])
                view = self.engine.views["test"]
                self.assertGreater(view["snapshot"]["timestamp"], old.timestamp)
                self.assertEqual(view["status"], "paused")
                self.assertFalse(self.f.store.account("test")["enabled"])
                self.assertEqual(self.f.store.get("cycle:test"), progress)
                self.assertIsNone(self.f.store.intent("test"))

    def test_failed_paused_read_keeps_old_timestamp_then_recovers_on_next_attempt(self):
        old = self.configure(True, "XAUUSD1")
        retained = deepcopy(self.engine.views["test"]["snapshot"])
        with patch.object(self.f.broker, "snapshot", side_effect=TradingError("Aster 网络连接失败")):
            self.engine.tick_account("test")
        self.assertEqual(self.engine.views["test"]["snapshot"], retained)
        self.assertEqual(self.engine.views["test"]["reason"], "Aster 网络连接失败")
        self.assertGreater(self.engine.work("test").backoff, time.monotonic())
        self.engine.tick_account("test")
        self.assertGreater(self.engine.views["test"]["snapshot"]["timestamp"], old.timestamp)
        self.assertFalse(self.f.store.account("test")["enabled"])

    def test_state_reports_current_display_cadence_without_exchange_reads(self):
        self.configure(True, "XAUUSD1")
        saved = self.f.store.account("test")
        saved["mode"] = "live"
        for enabled, cycling, expected in ((False, True, 60), (False, False, 60), (True, False, 10), (True, True, 2)):
            with self.subTest(enabled=enabled, cycling=cycling):
                saved["enabled"] = enabled
                saved["cycle"]["enabled"] = cycling
                self.f.store.save_account(saved)
                with patch.object(self.engine, "live_allowed", return_value=True), \
                     patch.object(self.f.broker, "snapshot", side_effect=AssertionError("state must not read exchange")):
                    row = self.engine.state()["accounts"][0]
                self.assertEqual(row["snapshot_refresh"]["interval_seconds"], expected)

    def test_display_schedule_does_not_make_old_snapshot_valid_for_execution(self):
        old = self.configure(True, "XAUUSD1")
        self.assertEqual(self.engine.state()["accounts"][0]["snapshot_refresh"]["interval_seconds"], 60)
        with self.assertRaises(TradingError):
            old.require_fresh()

    def test_paused_live_adapter_reads_account_with_only_get_requests(self):
        old = self.configure(True, "XAUUSD1")
        saved = self.f.store.account("test")
        saved["mode"] = "live"
        saved["policy"]["symbols"] = ["XAUUSD1"]
        self.f.store.save_account(saved)
        api = FixtureAPI(account_responses())
        api.budget = RateBudget()
        self.engine.brokers["test"] = LiveBroker({}, self.f.market, api=api)
        self.engine.tick_account("test")
        snapshot = self.engine.views["test"]["snapshot"]
        self.assertGreater(snapshot["timestamp"], old.timestamp)
        self.assertEqual(snapshot["wallet"], "200")
        self.assertEqual(len(snapshot["positions"]), 2)
        self.assertTrue(api.calls)
        self.assertTrue(all(call[0] == "GET" for call in api.calls))
        self.assertFalse(self.f.store.account("test")["enabled"])
        self.assertIsNone(self.f.store.intent("test"))

    def test_scheduler_reads_moonshot_before_account_has_ever_been_started(self):
        # Exercise the actual scheduler, cold views and live adapter after a
        # restart, including when the account has a saved cycle-only policy.
        for cycling in (False, True):
            with self.subTest(cycling=cycling):
                saved = self.f.store.account("test")
                saved.update(mode="live", enabled=False)
                saved["cycle"] = {"enabled": cycling, "symbol": "XAUUSD1"}
                self.f.store.save_account(saved)
                engine = Engine(self.f.store, market=self.f.market)
                market, api = market_and_api()
                list_other(api)
                engine.brokers["test"] = LiveBroker({}, market, api=api)
                seen = threading.Event()
                original_view = engine.view

                def publish(aid, **updates):
                    original_view(aid, **updates)
                    if updates.get("status") == "paused":
                        seen.set()

                with patch.dict(os.environ, {"ASTER_ALLOW_LIVE": "0"}), \
                     patch.object(engine, "view", side_effect=publish), \
                     patch.object(engine.dashboard_reports, "start"), \
                     patch.object(engine, "notify", return_value=60):
                    engine.start()
                    try:
                        self.assertTrue(seen.wait(3), engine.views)
                        view = engine.state()["accounts"][0]
                        self.assertEqual(view["status"], "paused")
                        self.assertIn(OTHER, [p["symbol"] for p in view["snapshot"]["positions"]])
                        self.assertEqual(view["snapshot"]["occupied_margin"], "90")
                    finally:
                        engine.stop()
                self.assertFalse(self.f.store.account("test")["enabled"])
                self.assertTrue(all(call[0] == "GET" for call in api.calls))
                self.assertIsNone(self.f.store.intent("test"))

    def test_adding_live_account_reads_holdings_without_enabling_it(self):
        market, api = market_and_api()
        list_other(api)
        self.engine.brokers["second"] = LiveBroker({}, market, api=api)
        seen = threading.Event()
        original_view = self.engine.view

        def publish(aid, **updates):
            original_view(aid, **updates)
            if aid == "second" and updates.get("status") == "paused":
                seen.set()

        with patch.dict(os.environ, {"ASTER_ALLOW_LIVE": "0"}), \
             patch.object(self.engine, "view", side_effect=publish), \
             patch.object(self.engine.dashboard_reports, "start"), \
             patch.object(self.engine, "notify", return_value=60):
            self.engine.start()
            try:
                with self.engine.account_lock("second"):
                    self.engine.add_account({"id": "second", "name": "未启动账户", "mode": "live", "env_prefix": "ASTER_SECOND"})
                    # This fixture only returns the selected market's flat legs.
                    saved = self.f.store.account("second")
                    saved["policy"]["symbols"] = ["XAUUSD1"]
                    self.f.store.save_account(saved)
                self.assertTrue(seen.wait(3), self.engine.views)
                positions = self.engine.views["second"]["snapshot"]["positions"]
                self.assertIn(OTHER, [p["symbol"] for p in positions])
            finally:
                self.engine.stop()
        self.assertFalse(self.f.store.account("second")["enabled"])
        self.assertTrue(all(call[0] == "GET" for call in api.calls))
        self.assertIsNone(self.f.store.intent("second"))
