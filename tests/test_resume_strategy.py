import copy
from dataclasses import replace
import os
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from trading.engine import Engine, snapshot_json
from trading.models import AccountModeError, TradingError, dec
from trading.server import create_app
from .helpers import Fixture


class ResumeStrategyTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.f.account["enabled"] = False
        self.f.store.save_account(self.f.account)
        self.engine = Engine(self.f.store, market=self.f.market)
        self.engine.brokers["test"] = self.f.broker
        self.engine.ready = True
        self.symbols = self.f.account["policy"]["symbols"]
        self.fresh = self.f.broker.snapshot(self.symbols)
        self.stale = snapshot_json(replace(self.fresh, timestamp=time.time() - 65), self.symbols)
        self.engine.view("test", status="paused", reason="策略已暂停", snapshot=self.stale, credential_ready=False)
        for symbol in self.symbols:
            self.engine.strategy("test", symbol, "策略已暂停", "paused")
        password = "test-only-resume-dashboard-password"
        with patch.dict(os.environ, {"ASTER_DASHBOARD_PASSWORD": password}, clear=True):
            app = create_app(self.engine, start_engine=False)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.client.headers["origin"] = "http://testserver"
        result = self.client.post("/api/login", json={"password": password})
        self.assertEqual(result.status_code, 200, result.text)

    def test_api_resumes_from_old_cached_view_and_immediately_publishes_verified_snapshot(self):
        with patch.object(self.f.broker, "snapshot", return_value=self.fresh) as read, \
             patch.object(self.f.broker, "submit", side_effect=AssertionError("enable must not submit")), \
             patch.object(self.f.broker, "set_leverage", side_effect=AssertionError("enable must not adjust leverage")):
            response = self.client.post("/api/accounts/test/enable")
        self.assertEqual(response.status_code, 200, response.text)
        read.assert_called_once_with(self.symbols, fresh_modes=True)
        account = self.client.get("/api/state").json()["accounts"][0]
        self.assertTrue(account["enabled"])
        self.assertTrue(account["credential_ready"])
        self.assertEqual((account["status"], account["reason"]), ("running", "策略运行中"))
        self.assertEqual(account["snapshot"], snapshot_json(self.fresh, self.symbols))
        self.assertGreater(account["snapshot"]["timestamp"], self.stale["timestamp"])
        self.assertEqual(account["strategies"], {symbol: {"phase": "waiting", "reason": "策略已启动，等待下一轮检查"}
                                                 for symbol in self.symbols})
        self.assertIn("test", self.engine.wake_accounts)

    def test_resume_refreshes_all_configured_strategy_messages_only_after_account_commit(self):
        symbols = ["XAUUSD1", "SPCXUSD1", "CLUSD1"]
        account = self.f.store.account("test")
        account["policy"]["symbols"] = symbols
        self.f.store.save_account(account)
        for symbol in symbols:
            self.engine.strategy("test", symbol, "策略已暂停", "paused", projected_ratio="0.2")
        before = copy.deepcopy(self.engine.views["test"])
        save = self.f.store.save_account

        def commit(value):
            self.assertEqual(self.engine.views["test"], before)
            return save(value)

        with patch.object(self.f.store, "save_account", side_effect=commit):
            self.engine.enable("test", True)
        strategies = self.engine.state()["accounts"][0]["strategies"]
        self.assertEqual(strategies, {symbol: {"phase": "waiting", "reason": "策略已启动，等待下一轮检查"}
                                     for symbol in symbols})

    def test_previous_attention_and_invalid_cached_modes_do_not_block_repaired_current_modes(self):
        old_reason = "账户固定模式不符合要求：双向持仓模式"
        self.f.store.save_account({**self.f.account, "pause_reason": old_reason})
        bad_cache = snapshot_json(replace(self.fresh, hedge_mode=False, timestamp=time.time() - 65), self.symbols)
        self.engine.view("test", status="attention", reason=old_reason, snapshot=bad_cache, credential_ready=False)
        with patch.object(self.f.broker, "snapshot", return_value=self.fresh):
            self.engine.enable("test", True)
        saved = self.f.store.account("test")
        self.assertTrue(saved["enabled"])
        self.assertNotIn("pause_reason", saved)
        view = self.engine.views["test"]
        self.assertTrue(view["credential_ready"])
        self.assertTrue(all(view["snapshot"]["mode_checks"].values()))
        self.assertEqual(view["status"], "running")

    def test_current_invalid_modes_still_reject_resume_and_preserve_pause_state(self):
        isolated = copy.deepcopy(self.fresh)
        isolated.pair("XAUUSD1")[0].isolated = True
        before = copy.deepcopy(self.engine.views["test"])
        for snapshot in (isolated, replace(self.fresh, hedge_mode=False), replace(self.fresh, multi_assets=True)):
            with self.subTest(checks=snapshot.mode_checks(self.symbols)), \
                 patch.object(self.f.broker, "snapshot", return_value=snapshot):
                with self.assertRaises(AccountModeError):
                    self.engine.enable("test", True)
                self.assertFalse(self.f.store.account("test")["enabled"])
                self.assertEqual(self.engine.views["test"], before)
                self.assertNotIn("test", self.engine.wake_accounts)

    def test_api_rejects_unfunded_untradable_open_orders_or_stale_current_snapshot(self):
        before = copy.deepcopy(self.engine.views["test"])
        cases = (({"equity": dec(0)}, "总权益不足"),
                 ({"can_trade": False}, "没有交易权限"),
                 ({"open_orders": [{"symbol": "XAUUSD1"}]}, "未完成挂单"),
                 ({"timestamp": time.time() - 10}, "快照已过期"))
        for changes, message in cases:
            with self.subTest(changes=changes), \
                 patch.object(self.f.broker, "snapshot", return_value=replace(self.fresh, **changes)):
                response = self.client.post("/api/accounts/test/enable")
            self.assertEqual(response.status_code, 409, response.text)
            self.assertIn(message, response.json()["detail"])
            self.assertFalse(self.f.store.account("test")["enabled"])
            self.assertEqual(self.engine.views["test"], before)

    def test_pending_batch_rejects_resume_without_querying_or_reconciling(self):
        pending = {"id": "resume-pending", "kind": "pair", "account_id": "test", "symbol": "XAUUSD1", "status": "pending"}
        self.f.store.save_intent(pending)
        with patch.object(self.f.broker, "snapshot", side_effect=AssertionError("pending work must be resolved first")), \
             patch("trading.engine.Executor.reconcile", side_effect=AssertionError("enable must not reconcile")):
            response = self.client.post("/api/accounts/test/enable")
        self.assertEqual(response.status_code, 409)
        self.assertIn("未完成批次", response.json()["detail"])
        self.assertEqual(self.f.store.intent("test"), pending)
        self.assertFalse(self.f.store.account("test")["enabled"])

    def test_every_configured_symbol_must_pass_before_state_changes(self):
        account = self.f.store.account("test")
        account["policy"]["symbols"] = ["XAUUSD1", "SPCXUSD1"]
        self.f.store.save_account(account)
        bad = copy.deepcopy(self.fresh)
        bad.positions = [p for p in bad.positions if (p.symbol, p.side) != ("SPCXUSD1", "SHORT")]
        with patch.object(self.f.broker, "snapshot", return_value=bad):
            with self.assertRaisesRegex(TradingError, "缺少双向持仓信息"):
                self.engine.enable("test", True)
        self.assertFalse(self.f.store.account("test")["enabled"])
        self.assertEqual(self.engine.views["test"]["snapshot"], self.stale)

    def test_live_guard_is_preserved_before_any_current_account_request(self):
        self.f.store.save_account({**self.f.account, "mode": "live"})
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(self.f.broker, "snapshot", side_effect=AssertionError("live guard must reject first")):
            with self.assertRaisesRegex(TradingError, "ASTER_ALLOW_LIVE"):
                self.engine.enable("test", True)
        self.assertFalse(self.f.store.account("test")["enabled"])

    def test_snapshot_serialization_failure_cannot_enable_or_replace_cached_view(self):
        before = copy.deepcopy(self.engine.views["test"])
        with patch.object(self.f.broker, "snapshot", return_value=self.fresh), \
             patch("trading.engine.snapshot_json", side_effect=TradingError("snapshot serialization failed")):
            with self.assertRaisesRegex(TradingError, "serialization failed"):
                self.engine.enable("test", True)
        self.assertFalse(self.f.store.account("test")["enabled"])
        self.assertEqual(self.engine.views["test"], before)
        self.assertEqual(self.engine.accounts_generation, 0)

    def test_account_save_failure_cannot_publish_running_or_fresh_snapshot(self):
        before = copy.deepcopy(self.engine.views["test"])
        with patch.object(self.f.broker, "snapshot", return_value=self.fresh), \
             patch.object(self.f.store, "save_account", side_effect=OSError("account commit failed")):
            with self.assertRaisesRegex(OSError, "account commit failed"):
                self.engine.enable("test", True)
        self.assertFalse(self.f.store.account("test")["enabled"])
        self.assertEqual(self.engine.views["test"], before)
        self.assertEqual(self.engine.wake_accounts, set())
        self.assertEqual(self.engine.accounts_generation, 0)

    def test_pausing_does_not_require_current_snapshot_or_replace_previous_view(self):
        with patch.object(self.f.broker, "snapshot", side_effect=AssertionError("pause must not query")):
            self.engine.enable("test", False)
        self.assertFalse(self.f.store.account("test")["enabled"])
        self.assertEqual(self.engine.views["test"]["snapshot"], self.stale)


if __name__ == "__main__":
    unittest.main()
