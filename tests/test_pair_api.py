"""Authenticated pair-group controls exercise the same origin boundary as accounts."""
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.helpers import Fixture, account
from tests.test_pair_integration import pair_config
from trading.engine import Engine
from trading.server import create_app


class PairApiTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        for aid in ("test", "second"):
            self.f.store.save_account({**account(aid), "enabled": False})
        self.engine = Engine(self.f.store, market=self.f.market)
        with patch.dict(os.environ, {"ASTER_DASHBOARD_PASSWORD": "pair-test-password"}):
            self.app = create_app(self.engine, start_engine=False)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.client.headers["origin"] = str(self.client.base_url).rstrip("/")

    def login(self):
        self.assertEqual(self.client.post("/api/login", json={"password": "pair-test-password"}).status_code, 200)

    def test_authentication_origin_and_strict_config(self):
        self.assertEqual(self.client.get("/api/pairs").status_code, 401)
        self.assertEqual(self.client.post("/api/pairs", json=pair_config()).status_code, 401)
        self.assertEqual(self.client.post("/api/pairs/gold/reconcile-flat").status_code, 401)
        self.login()
        response = self.client.post("/api/pairs", json=pair_config(), headers={"origin": "https://other.invalid"})
        self.assertEqual(response.status_code, 403)
        for values in ({"symbol": "CLUSD1"}, {"api_secret": "never-store"}, {"margin": {"enabled": "true"}}):
            response = self.client.post("/api/pairs", json=pair_config(**values))
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.f.store.pairs(), [])

    def test_create_edit_pause_and_flat_release(self):
        self.login()
        response = self.client.post("/api/pairs", json=pair_config())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["pair"]["enabled"])
        self.assertEqual(self.client.get("/api/pairs").json()["pairs"][0]["state"]["phase"], "paused")
        self.assertEqual(self.client.get("/api/state").json()["pairs"][0]["id"], "gold")
        response = self.client.patch("/api/pairs/gold", json={"ordinary": {"enabled": True}, "cycle": {"enabled": False}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["pair"]["ordinary"]["enabled"])
        self.assertEqual(self.client.patch("/api/pairs/gold", json={"enabled": True}).status_code, 422)
        response = self.client.post("/api/pairs/gold/pause")
        self.assertEqual(response.status_code, 200, response.text)
        response = self.client.post("/api/pairs/gold/reconcile-flat")
        self.assertEqual(response.status_code, 200, response.text)
        response = self.client.delete("/api/pairs/gold")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get("/api/pairs").json(), {"pairs": []})

    def test_nonzero_or_duplicate_members_are_rejected_without_creating_group(self):
        self.login()
        response = self.client.post("/api/pairs", json=pair_config(short_account_id="test"))
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIsNone(self.f.store.pair("gold"))
        response = self.client.post("/api/pairs", json=pair_config(enabled=True))
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIsNone(self.f.store.pair("gold"))

    def test_paused_start_adopts_actual_positions_without_an_extra_request_flag(self):
        self.login()
        self.assertEqual(self.client.post("/api/pairs", json=pair_config()).status_code, 200)
        for aid, side, qty in (("test", "LONG", "0.25"), ("second", "SHORT", "0.5")):
            broker = self.engine.broker(self.f.store.account(aid))
            broker.state["positions"]["XAUUSD1:" + side].update(qty=qty, entry="4412.015")
            broker.save()
        response = self.client.post("/api/pairs/gold/enable")
        self.assertEqual(response.status_code, 200, response.text)
        state = self.f.store.get("pair_runtime:gold")
        self.assertEqual(state["owned"], {"LONG": "0.25", "SHORT": "0.5"})
        self.assertEqual(state["progress"]["baseline"], state["owned"])
        self.assertEqual(self.client.post("/api/pairs/gold/enable").status_code, 409)
        self.assertEqual(self.f.store.get("pair_runtime:gold"), state)
