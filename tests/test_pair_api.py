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

    def test_order_recovery_requires_authentication_and_same_origin(self):
        endpoints = ("/api/pairs/gold/recovery-check", "/api/pairs/gold/recovery-preview", "/api/pairs/gold/recovery-confirm", "/api/pairs/gold/recovery-skip")
        body = {"token": "a" * 32, "acknowledge_unknown": True}
        for endpoint in endpoints:
            self.assertEqual(self.client.post(endpoint, json=body).status_code, 401)
        self.login()
        for endpoint in endpoints:
            response = self.client.post(endpoint, json=body, headers={"origin": "https://other.invalid"})
            self.assertEqual(response.status_code, 403, response.text)

    def test_order_recovery_confirmation_rejects_coerced_or_missing_acknowledgment(self):
        from tests.test_pair_order_recovery import seed_pending
        self.login()
        seed_pending(self.engine)
        preview = self.client.post("/api/pairs/gold/recovery-preview")
        self.assertEqual(preview.status_code, 200, preview.text)
        token = preview.json()["token"]
        for body in ({"token": token}, {"token": token, "acknowledge_unknown": "true"},
                     {"token": token, "acknowledge_unknown": 1}, {"token": token, "acknowledge_unknown": None},
                     {"token": token, "acknowledge_unknown": True, "force": True}):
            with self.subTest(body=body):
                response = self.client.post("/api/pairs/gold/recovery-confirm", json=body)
                self.assertEqual(response.status_code, 422, response.text)
        response = self.client.post("/api/pairs/gold/recovery-confirm", json={"token": token, "acknowledge_unknown": False})
        self.assertIn(response.status_code, (409, 422), response.text)
        self.assertIsNotNone(self.f.store.get("pair_runtime:gold")["pending"])

    def test_order_check_finishes_known_batch_and_remains_paused(self):
        from tests.test_pair_order_recovery import seed_pending, receipt_for
        self.login()
        state = seed_pending(self.engine)
        for leg in state["pending"]["legs"]:
            leg["receipt"] = receipt_for(leg)
        self.f.store.put("pair_runtime:gold", state)
        response = self.client.post("/api/pairs/gold/recovery-check")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["completed"])
        self.assertNotIn("token", response.json())
        self.assertFalse(self.f.store.pair("gold")["enabled"])
        self.assertIsNone(self.f.store.get("pair_runtime:gold")["pending"])

    def test_skip_requires_strict_batch_ack_and_rejects_stale_or_unknown_orders(self):
        from tests.test_pair_order_recovery import seed_pending, receipt_for, BATCH_ID
        self.login()
        state = seed_pending(self.engine)
        path = '/api/pairs/gold/recovery-skip'
        body = {'batch_id': BATCH_ID, 'acknowledge_skip': True}
        for bad in ({}, {'batch_id': BATCH_ID}, {**body, 'acknowledge_skip': 1},
                    {**body, 'acknowledge_skip': 'true'}, {**body, 'acknowledge_skip': False},
                    {**body, 'force': True}, {**body, 'batch_id': '../bad'}):
            self.assertEqual(self.client.post(path, json=bad).status_code, 422)
        self.assertEqual(self.client.post(path, json=body).status_code, 409)
        for leg in state['pending']['legs']:
            leg['receipt'] = receipt_for(leg)
        self.f.store.put('pair_runtime:gold', state)
        self.assertEqual(self.client.post(path, json={**body, 'batch_id': 'old'}).status_code, 409)
        response = self.client.post(path, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()['positions_verified'])
        self.assertFalse(self.f.store.pair('gold')['enabled'])
        self.assertIsNone(self.f.store.get('pair_runtime:gold')['pending'])

    def test_order_recovery_routes_archive_and_leave_start_as_a_separate_action(self):
        from tests.test_pair_order_recovery import seed_pending
        self.login()
        original = seed_pending(self.engine)
        preview = self.client.post("/api/pairs/gold/recovery-preview")
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["status"], "review")
        response = self.client.post("/api/pairs/gold/recovery-confirm",
                                    json={"token": preview.json()["token"], "acknowledge_unknown": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["ok"])
        self.assertFalse(self.f.store.pair("gold")["enabled"])
        runtime = self.f.store.get("pair_runtime:gold")
        self.assertIsNone(runtime["pending"])
        self.assertEqual(runtime["owned"], original["owned"])
        state_response = self.client.get("/api/pairs")
        self.assertEqual(state_response.status_code, 200)
        self.assertIsNone(state_response.json()["pairs"][0]["state"]["pending"])
        start = self.client.post("/api/pairs/gold/enable")
        self.assertEqual(start.status_code, 200, start.text)
        self.assertTrue(start.json()["pair"]["enabled"])
