import os
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from trading.engine import Engine
from trading.server import create_app
from trading.store import Store
from .helpers import Fixture

PASSWORD = "test-only-dashboard-password"


class DashboardAPITests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.engine = Engine(self.f.store, market=self.f.market)
        self.engine.brokers["test"] = self.f.broker
        self.f.account["enabled"] = False
        self.f.store.save_account(self.f.account)
        self.engine.tick_account("test")
        with patch.dict(os.environ, {"ASTER_DASHBOARD_PASSWORD": PASSWORD}, clear=True):
            self.app = create_app(self.engine, start_engine=False)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.client.headers["origin"] = "http://testserver"

    def login(self):
        result = self.client.post("/api/login", json={"password": PASSWORD})
        self.assertEqual(result.status_code, 200, result.text)
        return result

    def test_private_state_and_controls_require_login(self):
        self.assertEqual(self.client.get("/api/state").status_code, 401)
        self.assertEqual(self.client.post("/api/accounts/test/enable").status_code, 401)
        self.assertEqual(self.client.get("/api/health").status_code, 200)

    def test_session_cookie_is_http_only_and_state_not_cached(self):
        login = self.login()
        self.assertIn("HttpOnly", login.headers["set-cookie"])
        self.assertIn("SameSite=strict", login.headers["set-cookie"])
        result = self.client.get("/api/state")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers["cache-control"], "no-store")

    def test_cross_origin_mutation_rejected(self):
        self.login()
        result = self.client.post("/api/accounts/test/enable", headers={"origin": "https://untrusted.example"})
        self.assertEqual(result.status_code, 403)
        self.assertFalse(self.f.store.account("test")["enabled"])

    def test_login_rate_limit(self):
        for _ in range(5):
            self.assertEqual(self.client.post("/api/login", json={"password": "wrong"}).status_code, 401)
        self.assertEqual(self.client.post("/api/login", json={"password": PASSWORD}).status_code, 429)

    def test_create_configure_start_and_pause_paper_account(self):
        self.login()
        result = self.client.post("/api/accounts", json={"id": "second", "name": "第二账户", "mode": "paper", "env_prefix": "ASTER_SECOND"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertFalse(self.f.store.account("second")["enabled"])
        result = self.client.patch("/api/accounts/second", json={"threshold": "20000", "order_notional": "700"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.client.post("/api/accounts/second/enable").status_code, 200)
        self.assertEqual(self.client.patch("/api/accounts/second", json={"threshold": "0", "order_notional": "500"}).status_code, 409)
        self.assertEqual(self.f.store.account("second")["policy"]["threshold"], "20000")
        self.assertEqual(self.client.post("/api/accounts/second/pause").status_code, 200)
        self.assertFalse(self.f.store.account("second")["enabled"])

    def test_credentials_and_arbitrary_fields_are_not_accepted_or_echoed(self):
        self.login()
        secret = "must-not-be-returned-or-stored"
        result = self.client.post("/api/accounts", json={"id": "bad", "name": "bad", "mode": "live", "env_prefix": "ASTER_BAD", "private_key": secret})
        self.assertEqual(result.status_code, 422)
        self.assertNotIn(secret, result.text)
        self.assertIsNone(self.f.store.account("bad"))
        with patch.dict(os.environ, {"ASTER_TEST_PRIVATE_KEY": secret}):
            self.assertNotIn(secret, self.client.get("/api/state").text)

    def test_bad_policy_does_not_replace_existing_values(self):
        self.login()
        result = self.client.patch("/api/accounts/test", json={"threshold": "NaN", "order_notional": "100"})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.f.store.account("test")["policy"]["threshold"], "10000")

    def test_logout_removes_access(self):
        self.login()
        self.client.post("/api/logout")
        self.assertEqual(self.client.get("/api/state").status_code, 401)

    def test_demo_rejects_live_accounts(self):
        store = Store(Path(self.f.directory.name) / "demo.sqlite3")
        store.bind_runtime_mode(demo=True)
        demo = Engine(store, demo=True, market=self.f.market)
        with TestClient(create_app(demo, start_engine=False)) as client:
            response = client.post("/api/accounts", headers={"origin": "http://testserver"}, json={"id": "live", "name": "live", "mode": "live", "env_prefix": "ASTER_LIVE"})
        self.assertEqual(response.status_code, 409)

    def test_fixed_modes_are_read_only(self):
        self.login()
        for key, value in (("margin_type", "isolated"), ("hedge_mode", False), ("multi_assets", True)):
            with self.subTest(key=key):
                response = self.client.patch("/api/accounts/test", json={"threshold": "10000", "order_notional": "1000", key: value})
                self.assertEqual(response.status_code, 422)
        checks = self.client.get("/api/state").json()["accounts"][0]["snapshot"]["mode_checks"]
        self.assertEqual(checks, {"cross": True, "hedge": True, "single_asset": True})
