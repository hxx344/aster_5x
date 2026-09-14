"""Account isolation, cycle lifecycle and authenticated configuration contracts."""
from copy import deepcopy
import os
import time
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.helpers import Fixture, account
from trading.cycle import DEFAULT_CYCLE
from trading.engine import Engine
from trading.models import TradingError, dec
from trading.server import create_app
from trading.store import Store


class CycleEngineTests(TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        saved = self.f.store.account("test")
        saved.update(enabled=False)
        self.f.store.save_account(saved)
        self.engine = Engine(self.f.store, market=self.f.market)
        self.engine.brokers["test"] = self.f.broker

    def select(self, **changes):
        self.engine.configure("test", {"cycle": {"enabled": True, **changes}})

    def start_holding(self):
        self.select()
        self.engine.enable("test", True)
        with patch("trading.engine.Executor.open_pair", side_effect=AssertionError("ordinary strategy must not run")), \
             patch("trading.engine.Engine.tick_migration", side_effect=AssertionError("migration must not run")):
            for _ in range(6):
                self.engine.tick_account("test")
                progress = self.f.store.get("cycle:test")
                if progress and progress["phase"] == "holding":
                    return progress
        self.fail(str(self.engine.state()["accounts"][0]))

    def expire_hold(self):
        progress = self.f.store.get("cycle:test")
        progress["opened_at"] = time.time() - progress["config"]["hold_seconds"] - 1
        self.f.store.put("cycle:test", progress)

    def test_2x_open_hold_close_and_repeat_survives_engine_restart(self):
        progress = self.start_holding()
        self.assertEqual([p.leverage for p in self.f.broker.snapshot(["XAUUSD1"]).pair("XAUUSD1")], [2, 2])
        self.assertGreater(dec(progress["quantities"]["LONG"]), 0)
        self.assertEqual(progress["quantities"]["LONG"], progress["quantities"]["SHORT"])
        before = deepcopy(self.f.broker.state["orders"])
        self.engine.tick_account("test")
        self.assertEqual(before, self.f.broker.state["orders"])
        self.assertEqual(self.engine.state()["accounts"][0]["cycle_state"]["phase"], "holding")
        reopened = Store(self.f.store.path)
        self.engine = Engine(reopened, market=self.f.market)
        self.expire_hold()
        for _ in range(4):
            self.engine.tick_account("test")
            if self.f.store.get("cycle:test")["completed_cycles"] == 1:
                break
        finished = self.f.store.get("cycle:test")
        self.assertEqual(finished["completed_cycles"], 1)
        self.assertEqual(finished["quantities"], {"LONG": "0", "SHORT": "0"})
        self.assertEqual(finished["phase"], "waiting_open")
        self.assertIsNone(self.f.store.intent("test"))
        self.engine.tick_account("test")
        self.assertEqual(self.f.store.get("cycle:test")["phase"], "holding")

    def test_elapsed_time_does_not_bypass_spread_and_stale_depth_blocks_close(self):
        self.start_holding()
        self.expire_hold()
        before = deepcopy(self.f.broker.state["orders"])
        depth = self.f.market.depth("XAUUSD1")
        from dataclasses import replace
        with patch.object(self.f.market, "depth", return_value=replace(depth, timestamp=time.time() - 4)):
            self.engine.tick_account("test")
        self.assertEqual(before, self.f.broker.state["orders"])
        self.assertEqual(self.engine.state()["accounts"][0]["cycle_state"]["phase"], "waiting_close")
        from fractions import Fraction
        wide = replace(depth, asks=tuple((p + Fraction(1), q) for p, q in depth.asks))
        with patch.object(self.f.market, "depth", return_value=wide):
            self.engine.tick_account("test")
        self.assertEqual(before, self.f.broker.state["orders"])
        self.assertEqual(self.f.store.get("cycle:test")["completed_cycles"], 0)

    def test_restart_treats_null_retry_time_as_no_cooldown(self):
        self.select()
        self.engine.enable("test", True)
        progress = self.f.store.get("cycle:test")
        progress["retry_at"] = None
        self.f.store.put("cycle:test", progress)
        restarted = Engine(self.f.store, market=self.f.market)
        for _ in range(5):
            restarted.tick_account("test")
            if self.f.store.get("cycle:test")["phase"] == "holding":
                break
        self.assertEqual(self.f.store.get("cycle:test")["phase"], "holding")

    def test_pause_keeps_hold_and_blocks_config_until_flat(self):
        progress = self.start_holding()
        self.engine.enable("test", False)
        self.expire_hold()
        before = deepcopy(self.f.broker.state)
        self.engine.tick_account("test")
        self.assertEqual(before, self.f.broker.state)
        for changes in ({"cycle": {"enabled": False}}, {"cycle": {"symbol": "CLUSD1"}}, {"margin_limit": "0.9"}):
            with self.subTest(changes=changes), self.assertRaisesRegex(TradingError, "仍有持仓"):
                self.engine.configure("test", changes)
        self.engine.enable("test", True)
        self.assertEqual(self.f.store.get("cycle:test")["run_id"], progress["run_id"])
        self.engine.tick_account("test")
        self.assertEqual(self.f.store.get("cycle:test")["completed_cycles"], 1)

    def test_external_positions_are_never_adopted_or_closed(self):
        self.select()
        for side in ("LONG", "SHORT"):
            self.f.broker.state["positions"]["XAUUSD1:" + side] = {"qty": "1", "entry": "4412"}
        self.f.store.put("paper:test", self.f.broker.state)
        with self.assertRaisesRegex(TradingError, "已有仓位"):
            self.engine.enable("test", True)
        self.assertFalse(self.f.store.account("test")["enabled"])
        self.assertEqual(self.f.broker.state["orders"], {})

    def test_external_quantity_change_during_hold_pauses_without_orders(self):
        self.start_holding()
        before = deepcopy(self.f.broker.state["orders"])
        row = self.f.broker.state["positions"]["XAUUSD1:LONG"]
        row["qty"] = str(dec(row["qty"]) + 1)
        self.f.store.put("paper:test", self.f.broker.state)
        self.engine.tick_account("test")
        self.assertFalse(self.f.store.account("test")["enabled"])
        self.assertEqual(self.engine.state()["accounts"][0]["cycle_state"]["phase"], "attention")
        self.assertEqual(before, self.f.broker.state["orders"])

    def test_accounts_have_independent_settings_positions_and_timers(self):
        self.start_holding()
        other = account("second")
        other.update(enabled=False)
        self.f.store.save_account(other)
        self.engine.configure("second", {"cycle": {"enabled": True, "symbol": "SPCXUSD1", "leverage": 3,
                                                  "spread_limit_bp": "1", "max_notional": "2000", "hold_seconds": 120}})
        self.engine.enable("second", True)
        for _ in range(5):
            self.engine.tick_account("second")
            if self.f.store.get("cycle:second")["phase"] == "holding":
                break
        first, second = (self.f.store.get("cycle:" + aid) for aid in ("test", "second"))
        self.assertEqual(second["phase"], "holding")
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(first["config"]["hold_seconds"], 60)
        self.assertEqual(second["config"]["hold_seconds"], 120)
        other_snapshot = self.engine.brokers["second"].snapshot(["XAUUSD1", "SPCXUSD1"])
        self.assertEqual(other_snapshot.pair("XAUUSD1")[0].qty, 0)
        self.assertGreater(other_snapshot.pair("SPCXUSD1")[0].qty, 0)


class CycleSettingsTests(TestCase):
    def setUp(self):
        CycleEngineTests.setUp(self)
        password = "test-only-cycle-settings-password"
        env = patch.dict(os.environ, {"ASTER_DASHBOARD_PASSWORD": password}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(create_app(self.engine, start_engine=False))
        self.addCleanup(self.client.close)
        self.client.headers["origin"] = "http://testserver"
        self.assertEqual(self.client.post("/api/login", json={"password": password}).status_code, 200)

    def test_api_save_is_opt_in_and_has_no_order_side_effects(self):
        self.assertEqual(self.f.store.account("test")["cycle"], DEFAULT_CYCLE)
        before = deepcopy(self.f.broker.state)
        response = self.client.patch("/api/accounts/test", json={"cycle": {"enabled": True, "leverage": 2}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(self.f.store.account("test")["cycle"]["enabled"])
        self.assertFalse(self.f.store.account("test")["enabled"])
        self.assertEqual(before, self.f.broker.state)
        self.assertIsNone(self.f.store.get("cycle:test"))

    def test_api_invalid_settings_are_atomic_and_migration_is_exclusive(self):
        original = self.f.store.account("test")
        bad = [None, {}, {"enabled": 1}, {"leverage": True}, {"leverage": 2.5}, {"symbol": "BTCUSDT"},
               {"hold_seconds": 0}, {"spread_limit_bp": "NaN"}, {"spread_limit_bp": "101"},
               {"max_notional": "100", "min_notional": "101"}, {"notional_scope": "unknown"}]
        for cycle in bad:
            with self.subTest(cycle=cycle):
                response = self.client.patch("/api/accounts/test", json={"threshold": "12345", "cycle": cycle})
                self.assertIn(response.status_code, (409, 422), response.text)
                self.assertEqual(self.f.store.account("test"), original)
        response = self.client.patch("/api/accounts/test", json={"cycle": {"enabled": True}, "migration": {"enabled": True}})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.f.store.account("test"), original)

    def test_api_auth_and_origin_apply_to_cycle_configuration(self):
        anonymous = TestClient(self.client.app)
        self.addCleanup(anonymous.close)
        self.assertEqual(anonymous.patch("/api/accounts/test", json={"cycle": {"enabled": True}},
                                        headers={"origin": "http://testserver"}).status_code, 401)
        self.assertEqual(self.client.patch("/api/accounts/test", json={"cycle": {"enabled": True}},
                                          headers={"origin": "https://example.invalid"}).status_code, 403)
