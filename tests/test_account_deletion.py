"""Deletion removes management, not exchange positions or historical ledgers."""
from concurrent.futures import ThreadPoolExecutor
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from trading.engine import Engine
from trading.exchange import LiveBroker
from trading.models import TradingError
from trading.paper import DemoMarket
from trading.server import create_app
from trading.store import Store
from .helpers import Fixture, account


class AccountDeletionTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        saved = self.f.store.account("test")
        saved["enabled"] = False
        self.f.store.save_account(saved)
        self.engine = Engine(self.f.store, market=self.f.market)
        self.engine.ready = True

    def test_removal_retains_history_and_prevents_reuse_or_stale_resurrection(self):
        saved = self.f.store.account("test")
        self.f.store.event("test", "fill", "retained trade")
        self.f.store.put("paper:test", {"positions": {"CLUSD1:LONG": {"qty": "1"}}})
        self.engine.delete_account("test")
        self.engine.delete_account("test")
        self.assertEqual(self.engine.state()["accounts"], [])
        restarted = Store(self.f.store.path)
        self.assertIsNone(restarted.account("test"))
        self.assertTrue(restarted.account_id_used("test"))
        self.assertTrue(any(row["message"] == "retained trade" for row in restarted.events()))
        self.assertEqual(restarted.get("paper:test")["positions"]["CLUSD1:LONG"]["qty"], "1")
        with self.assertRaisesRegex(TradingError, "新的账户标识"):
            restarted.save_account(saved)
        with self.assertRaisesRegex(TradingError, "新的账户标识"):
            self.engine.add_account(saved)
        replacement = self.engine.add_account({"id": "replacement", "name": "Replacement", "mode": "paper", "env_prefix": saved["env_prefix"]})
        self.assertFalse(replacement["enabled"])
        self.assertEqual([row["id"] for row in self.engine.state()["accounts"]], ["replacement"])

    def test_running_account_cannot_be_deleted_and_dashboard_explains_why(self):
        saved = self.f.store.account("test")
        saved["enabled"] = True
        self.f.store.save_account(saved)
        self.assertIn("暂停", self.engine.state()["accounts"][0]["deletion_block"])
        with self.assertRaisesRegex(TradingError, "暂停"):
            self.engine.delete_account("test")
        self.assertIsNotNone(self.f.store.account("test"))

    def test_pending_order_and_post_fill_check_block_deletion(self):
        for blocker in ("intent", "post_fill"):
            with self.subTest(blocker=blocker):
                if blocker == "intent":
                    self.f.store.save_intent({"id": "pending", "account_id": "test", "status": "attention", "kind": "open"})
                else:
                    self.f.store.put("post_fill_check:test", {"required": True})
                with self.assertRaisesRegex(TradingError, "未完成交易"):
                    self.engine.delete_account("test")
                self.assertIsNotNone(self.f.store.account("test"))
                with self.f.store.connect() as db:
                    db.execute("DELETE FROM intents WHERE id='pending'")

    def test_held_or_invalid_cycle_cannot_be_deleted_even_if_cycle_switch_is_off(self):
        for progress in ({"phase": "holding"}, {"phase": "waiting_close"}, {"opened_at": 1},
                         {"quantities": {"LONG": "0.1", "SHORT": "0.1"}}, {"quantities": {"LONG": "NaN"}},
                         {"phase": "unknown"}, ["invalid"]):
            with self.subTest(progress=progress):
                self.f.store.put("cycle:test", progress)
                with self.assertRaisesRegex(TradingError, "循环"):
                    self.engine.delete_account("test")
                self.assertIsNotNone(self.f.store.account("test"))
        self.f.store.put("cycle:test", {"phase": "waiting_open", "quantities": {"LONG": "0", "SHORT": "0"}, "opened_at": None})
        self.engine.delete_account("test")

    def test_cleanup_closes_broker_and_blocks_stale_worker_recreation(self):
        saved = self.f.store.account("test")
        broker = Mock(spec=LiveBroker)
        self.engine.brokers["test"] = broker
        self.engine.signers["signer"] = "test"
        self.engine.users["user"] = "test"
        self.engine.view("test", reason="old")
        self.engine.delete_account("test")
        broker.close.assert_called_once()
        self.assertEqual(self.engine.signers, {})
        self.assertEqual(self.engine.users, {})
        self.assertNotIn("test", self.engine.views)
        with self.assertRaisesRegex(TradingError, "已删除"):
            self.engine.broker(saved)
        self.engine.tick_account("test")
        broker.submit.assert_not_called()

    def test_deletion_waits_for_execution_lock_then_checks_latest_state(self):
        attempted = threading.Event()
        def remove():
            attempted.set()
            self.engine.delete_account("test")
        with ThreadPoolExecutor(1) as pool:
            with self.engine.account_lock("test"):
                future = pool.submit(remove)
                self.assertTrue(attempted.wait(2))
                self.assertFalse(future.done())
                saved = self.f.store.account("test")
                saved["enabled"] = True
                self.f.store.save_account(saved)
            with self.assertRaisesRegex(TradingError, "暂停"):
                future.result(timeout=3)

    def test_api_requires_login_and_same_origin_and_refreshes_empty_state(self):
        password = "deletion-test-password"
        with patch.dict(os.environ, {"ASTER_DASHBOARD_PASSWORD": password}, clear=True):
            app = create_app(self.engine, start_engine=False)
        with TestClient(app) as client:
            self.assertEqual(client.delete("/api/accounts/test").status_code, 401)
            self.assertEqual(client.post("/api/login", json={"password": password}, headers={"origin": "http://testserver"}).status_code, 200)
            for origin in (None, "https://other.example"):
                self.assertEqual(client.delete("/api/accounts/test", headers={"origin": origin} if origin else {}).status_code, 403)
            self.assertIsNotNone(self.f.store.account("test"))
            response = client.delete("/api/accounts/test", headers={"origin": "http://testserver"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(client.get("/api/state").json()["accounts"], [])
            self.assertEqual(client.delete("/api/accounts/missing", headers={"origin": "http://testserver"}).status_code, 409)

    def test_deleted_demo_is_not_seeded_again_on_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "demo.db", demo=True)
            demo = Engine(store, demo=True, market=DemoMarket())
            demo.delete_account("demo")
            restarted = Engine(Store(store.path, demo=True), demo=True, market=DemoMarket())
            self.assertEqual(restarted.state()["accounts"], [])
