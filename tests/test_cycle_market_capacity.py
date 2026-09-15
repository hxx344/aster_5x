"""Public quota admission, hot wakeups and exact preservation of original legs."""
from copy import deepcopy
from decimal import localcontext
from fractions import Fraction
import time
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient

from tests.helpers import Fixture, seed_cycle_capacity
from trading.cycle import DEFAULT_CYCLE, plan_cycle, validate_cycle
from trading.cycle_capacity import capacity_target, require_cycle_capacity
from trading.engine import Engine, snapshot_json
from trading.models import TradingError, dec
from trading.server import create_app


SYMBOL = "XAUUSD1"


class CycleCapacityThresholdTests(unittest.TestCase):
    def test_multiplier_validation_default_and_exact_decimal_bounds(self):
        self.assertEqual(validate_cycle()["capacity_multiplier"], "1")
        for value in ("1", "1.25", "100"):
            self.assertEqual(validate_cycle({"capacity_multiplier": value})["capacity_multiplier"], value)
        for value in (True, 2, None, "0", "-1", "0.99999999999999999999", "100.00000000000000000001", "NaN", "Infinity"):
            with self.subTest(value=value), self.assertRaises(TradingError):
                validate_cycle({"capacity_multiplier": value})

    def test_target_uses_gross_upper_amount_and_exact_multiplier(self):
        with localcontext() as context:
            context.prec = 5
            config = {**DEFAULT_CYCLE, "max_notional": "12345.6789", "capacity_multiplier": "1.23456789"}
            target, required = capacity_target(config)
            self.assertEqual(target, Fraction("24691.3578"))
            self.assertEqual(required, target * Fraction("1.23456789"))
            self.assertEqual(capacity_target({**config, "notional_scope": "gross"}), (target / 2, required / 2))

    def test_equality_passes_but_one_decimal_below_threshold_does_not(self):
        config = {**DEFAULT_CYCLE, "capacity_multiplier": "2"}
        row = {"status": "ok", "checked_at": 100, "capacities": {"17": "40000"}}
        self.assertEqual(require_cycle_capacity(config, 17, row, now=100.2), dec(40000))
        row["capacities"]["17"] = "39999.99999999999999999999"
        with self.assertRaises(TradingError) as caught:
            require_cycle_capacity(config, 17, row, now=100.2)
        self.assertEqual(caught.exception.diagnostic["checks"][0]["required"], "≥ 40000")

    def test_exact_tier_freshness_and_failure_are_required(self):
        valid = {"status": "ok", "checked_at": 100, "capacities": {"5": "999999"}}
        for changes, leverage in (({}, 2), ({"status": "error"}, 5), ({"checked_at": 98.999}, 5),
                                   ({"checked_at": 102}, 5), ({"checked_at": float("nan")}, 5),
                                   ({"capacity_checked_at": {"5": 98}}, 5),
                                   ({"capacities": {"5": "NaN"}}, 5), ({}, None)):
            with self.subTest(changes=changes, leverage=leverage), self.assertRaises(TradingError):
                require_cycle_capacity(DEFAULT_CYCLE, leverage, {**valid, **changes}, now=100)


class CycleMarketCapacityEngineTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        owner = self.f.store.account("test")
        owner["cycle"] = {**DEFAULT_CYCLE, "enabled": True, "max_notional": "1000", "spread_notional": "1000", "capacity_multiplier": "2"}
        self.f.store.save_account(owner)
        self.f.broker.set_cycle_leverage(SYMBOL, 2)
        self.engine = Engine(self.f.store, market=self.f.market)
        self.engine.brokers["test"] = self.f.broker
        self.engine.view("test", snapshot=snapshot_json(self.f.broker.snapshot([SYMBOL]), [SYMBOL]))
        seed_cycle_capacity(self.engine)

    def quota(self, value):
        self.engine.markets[SYMBOL] = {"status": "ok", "checked_at": time.time(), "capacities": {"2": value}}

    def test_capacity_is_checked_before_spread_in_worker_and_public_hint(self):
        self.quota("3999")
        owner = self.f.store.account("test")
        with patch.object(self.f.market, "depth", side_effect=AssertionError("quota must precede depth")), \
             patch.object(self.f.broker, "submit", side_effect=AssertionError("low quota submitted")):
            with self.assertRaises(TradingError):
                self.engine.cycle_public_hint(owner)
            self.engine.tick_account("test")
        self.assertEqual(self.engine.views["test"]["status"], "waiting")
        self.assertEqual(self.engine.views["test"]["cycle_state"]["diagnostic"]["code"], "cycle_market_capacity")
        self.assertEqual(self.engine.cycle_quote_backoff["test"], self.engine.account_backoff["test"])

    def test_capacity_recovery_wakes_existing_fresh_quote_without_new_ws_event(self):
        self.quota("0")
        owner = self.f.store.account("test")
        self.engine.on_cycle_market_update(SYMBOL, "depth", time.time(), time.monotonic())
        self.assertEqual(self.engine.cycle_wake_candidates([owner]), {})
        self.engine.capacity_targets = {SYMBOL: {2}}
        self.engine.capacity_accounts = [owner]
        self.engine.capacity_full_checked[SYMBOL] = time.monotonic()
        with patch.object(self.f.market, "capacities", return_value={2: dec(4000)}), \
             patch.object(self.engine, "wake_capacity_accounts") as ordinary:
            self.engine.poll_market(SYMBOL)
        ordinary.assert_not_called()
        self.assertTrue(self.engine.scheduler_event.is_set())
        signal = self.engine.cycle_wake_candidates([owner])["test"]
        self.engine.tick_account("test", cycle_signal=signal)
        self.assertEqual(self.f.store.get("cycle:test")["phase"], "holding")

    def test_capacity_drop_after_planning_blocks_final_submit(self):
        def planned(*args, **kwargs):
            result = plan_cycle(*args, **kwargs)
            self.quota("0")
            return result
        with patch("trading.engine.plan_cycle", side_effect=planned), patch.object(self.f.broker, "submit") as submit:
            self.engine.tick_account("test")
        submit.assert_not_called()
        self.assertIsNone(self.f.store.intent("test"))

    def test_drop_after_durable_intent_never_sends_or_retries_opening(self):
        create = self.f.store.create_cycle_intent
        def persist(*args, **kwargs):
            result = create(*args, **kwargs)
            self.quota("0")
            return result
        with patch.object(self.f.store, "create_cycle_intent", side_effect=persist), patch.object(self.f.broker, "submit") as submit:
            self.engine.tick_account("test")
            self.engine.tick_account("test")
        submit.assert_not_called()
        self.assertEqual(self.f.broker.state["orders"], {})
        self.assertIsNone(self.f.store.intent("test"))

    def test_zero_failed_and_missing_quota_allow_close_back_to_unequal_baseline(self):
        baseline = {"LONG": "1", "SHORT": "0.5"}
        for side, qty in baseline.items():
            self.f.broker.state["positions"][SYMBOL + ":" + side] = {"qty": qty, "entry": "4412"}
        self.f.broker.save()
        for row in ({"status": "ok", "checked_at": time.time(), "capacities": {"2": "0"}}, {"status": "error"}, {}):
            seed_cycle_capacity(self.engine)
            self.engine.tick_account("test")
            progress = self.f.store.get("cycle:test")
            self.assertEqual(progress["phase"], "holding")
            self.assertEqual({side: dec(qty) for side, qty in progress["baseline"].items()},
                             {side: dec(qty) for side, qty in baseline.items()})
            progress["opened_at"] = time.time() - 61
            self.f.store.put("cycle:test", progress)
            self.engine.markets[SYMBOL] = deepcopy(row)
            with patch.object(self.engine, "require_cycle_open_capacity", side_effect=AssertionError("closing checked opening quota")):
                self.engine.tick_account("test")
            pair = self.f.broker.snapshot([SYMBOL]).pair(SYMBOL)
            self.assertEqual(tuple(p.qty for p in pair), (dec(1), dec("0.5")))
        self.assertEqual(self.f.store.get("cycle:test")["completed_cycles"], 3)

    def test_saved_multiplier_survives_restart_and_is_locked_during_cycle(self):
        owner = self.f.store.account("test")
        owner["enabled"] = False
        self.f.store.save_account(owner)
        self.engine.configure("test", {"cycle": {**owner["cycle"], "capacity_multiplier": "2.5"}})
        restarted = Engine(self.f.store, market=self.f.market)
        self.assertEqual(restarted.state()["accounts"][0]["cycle"]["capacity_multiplier"], "2.5")
        self.engine.enable("test", True)
        self.engine.tick_account("test")
        self.engine.enable("test", False)
        with self.assertRaises(TradingError):
            self.engine.configure("test", {"cycle": {**owner["cycle"], "capacity_multiplier": "3"}})

    def test_api_saves_decimal_multiplier_and_rejects_invalid_updates_atomically(self):
        owner = self.f.store.account("test")
        owner["enabled"] = False
        self.f.store.save_account(owner)
        with patch.dict("os.environ", {"ASTER_DASHBOARD_PASSWORD": "test-capacity-password"}), \
             TestClient(create_app(self.engine, start_engine=False), headers={"origin": "http://testserver"}) as client:
            self.assertEqual(client.post("/api/login", json={"password": "test-capacity-password"}).status_code, 200)
            self.assertEqual(client.patch("/api/accounts/test", json={"cycle": {"capacity_multiplier": "2.5"}}).status_code, 200)
            saved = self.f.store.account("test")
            self.assertEqual(saved["cycle"]["capacity_multiplier"], "2.5")
            for value in ("0.5", "101", True, 2, None):
                response = client.patch("/api/accounts/test", json={"threshold": "9999", "cycle": {"capacity_multiplier": value}})
                self.assertIn(response.status_code, (409, 422))
                self.assertEqual(self.f.store.account("test"), saved)
