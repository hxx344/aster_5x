"""Selected-account cycle restrictions stay authoritative across UI and dispatch."""
from copy import deepcopy
import os
import time
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests import test_cycle_engine as cycle_cases
from tests.helpers import account
from trading.engine import Engine
from trading.models import dec
from trading.server import create_app


SYMBOL = "XAUUSD1"


class CycleAddGuardStateTests(TestCase):
    setUp = cycle_cases.CycleEngineTests.setUp
    select = cycle_cases.CycleEngineTests.select

    def accounts(self):
        return {row["id"]: row for row in self.engine.state()["accounts"]}

    def test_saved_cycle_selection_blocks_only_its_account_and_market(self):
        self.select()
        self.f.store.save_account(account("second"))
        self.engine.view("test", ordinary_add_blocks={"CLUSD1": "obsolete"})
        self.engine.view("second", ordinary_add_blocks={SYMBOL: "another account's stale block"})
        before_orders = deepcopy(self.f.broker.state["orders"])
        state = self.accounts()
        self.assertEqual(set(state["test"]["ordinary_add_blocks"]), {SYMBOL})
        self.assertIn("5x / 10x / 20x", state["test"]["ordinary_add_blocks"][SYMBOL])
        self.assertEqual(state["second"]["ordinary_add_blocks"], {})
        self.assertEqual(before_orders, self.f.broker.state["orders"])

    def test_switch_disable_and_restart_use_saved_configuration_without_waiting_for_tick(self):
        self.select()
        self.assertEqual(set(self.accounts()["test"]["ordinary_add_blocks"]), {SYMBOL})
        self.engine.configure("test", {"cycle": {"symbol": "CLUSD1"}})
        self.assertEqual(set(self.accounts()["test"]["ordinary_add_blocks"]), {"CLUSD1"})
        self.engine = Engine(self.f.store, market=self.f.market)
        self.assertEqual(set(self.accounts()["test"]["ordinary_add_blocks"]), {"CLUSD1"})
        self.engine.configure("test", {"cycle": {"enabled": False}})
        self.assertEqual(self.accounts()["test"]["ordinary_add_blocks"], {})

    def test_all_tier_capacity_and_preexisting_priority_do_not_enter_ordinary_opening(self):
        self.select()
        self.engine.enable("test", True)
        capacities = {tier: dec("500000") for tier in (5, 10, 20)}
        self.f.store.save_account(account("second"))
        self.engine.wake_capacity_accounts(SYMBOL, capacities, time.time(), self.f.store.accounts())
        self.assertNotIn("test", self.engine.priority_accounts)
        self.assertIn(SYMBOL, self.engine.priority_accounts["second"])
        # A queued signal from before mode selection must not bypass dispatch.
        self.engine.active_priority_accounts.add("test")
        self.engine.active_priority_signals["test"] = {SYMBOL: time.time()}
        with patch("trading.engine.Executor.open_pair", side_effect=AssertionError("ordinary additions are blocked")), \
             patch("trading.engine.Executor.leverage", side_effect=AssertionError("ordinary upgrades are not the cycle path")):
            for _ in range(6):
                self.engine.markets[SYMBOL] = {"status": "ok", "checked_at": time.time(),
                                               "capacities": {str(key): str(value) for key, value in capacities.items()}}
                self.engine.tick_account("test")
                if self.f.store.get("cycle:test")["phase"] == "holding":
                    break
        self.assertEqual(self.f.store.get("cycle:test")["phase"], "holding")
        self.assertEqual(len(self.f.broker.state["orders"]), 2)
        state = self.accounts()
        self.assertEqual(set(state["test"]["ordinary_add_blocks"]), {SYMBOL})
        self.assertEqual(state["second"]["ordinary_add_blocks"], {})
        for tier in (5, 10, 20):
            self.assertEqual(self.engine.state()["markets"][SYMBOL]["capacities"][str(tier)], "500000")

    def test_authenticated_api_exposes_account_specific_blocks_without_changing_controls(self):
        self.select()
        self.f.store.save_account(account("second"))
        with patch.dict(os.environ, {"ASTER_DASHBOARD_PASSWORD": "test-only-cycle-add-guard"}, clear=True):
            client = TestClient(create_app(self.engine, start_engine=False))
            self.addCleanup(client.close)
            client.headers["origin"] = "http://testserver"
            self.assertEqual(client.get("/api/state").status_code, 401)
            self.assertEqual(client.post("/api/login", json={"password": "test-only-cycle-add-guard"}).status_code, 200)
            response = client.get("/api/state")
        self.assertEqual(response.status_code, 200)
        rows = {row["id"]: row for row in response.json()["accounts"]}
        self.assertFalse(rows["test"]["enabled"])
        self.assertTrue(rows["second"]["enabled"])
        self.assertEqual(set(rows["test"]["ordinary_add_blocks"]), {SYMBOL})
        self.assertEqual(rows["second"]["ordinary_add_blocks"], {})
        self.assertIsNone(self.f.store.intent("test"))
        self.assertIsNone(self.f.store.intent("second"))
