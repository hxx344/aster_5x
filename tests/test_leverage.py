import time
from dataclasses import replace
import unittest
from unittest.mock import Mock, patch

from trading.engine import Engine
from trading.exchange import LiveBroker
from trading.execution import Executor
from trading.models import TradingError, dec, next_leverage
from trading.store import Store
from .helpers import Fixture


class LeverageRulesTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.engine = Engine(self.f.store, market=self.f.market)
        self.engine.brokers["test"] = self.f.broker
        self.engine.poll_market("XAUUSD1")

    def position(self, leverage, qty="0"):
        self.f.broker.state["leverages"]["XAUUSD1"] = leverage
        for side in ("LONG", "SHORT"):
            self.f.broker.state["positions"]["XAUUSD1:" + side] = {"qty": qty, "entry": "4412"}
        self.f.broker.save()

    def capacities(self, values):
        self.engine.markets["XAUUSD1"]["capacities"] = {str(k): str(v) for k, v in values.items()}

    def test_existing_1x_upgrades_to_4x_confirms_then_opens_before_further_upgrade(self):
        self.position(1, "1")
        # All higher tiers are available; the initial snapshot has no 1x capacity.
        self.assertNotIn("1", self.engine.markets["XAUUSD1"]["capacities"])
        with patch.object(self.f.broker, "set_leverage", wraps=self.f.broker.set_leverage) as change, \
             patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            self.engine.tick_account("test")
            change.assert_called_once_with("XAUUSD1", 4)
            submit.assert_not_called()
            self.assertIsNotNone(self.f.store.intent("test"))
            self.engine.tick_account("test")
            submit.assert_not_called()
            self.assertIsNone(self.f.store.intent("test"))
            # Confirmation/first-add ordering survives a service restart.
            restarted = Engine(Store(self.f.store.path), market=self.f.market)
            restarted.brokers["test"] = self.f.broker
            restarted.poll_market("XAUUSD1")
            restarted.tick_account("test")
            self.assertEqual(change.call_count, 1)
            submit.assert_called_once()
            long = self.f.broker.snapshot(["XAUUSD1"]).pair("XAUUSD1")[0]
            self.assertEqual(long.leverage, 4)
            self.assertGreater(long.qty, 1)
            self.assertIsNone(self.f.store.get("open_after_leverage:test:XAUUSD1"))
            restarted.tick_account("test")
            self.assertEqual(change.call_args.args, ("XAUUSD1", 5))

    def test_skips_unavailable_intermediate_tier_and_respects_threshold(self):
        self.position(1, "1")
        snapshot = self.f.broker.snapshot(["XAUUSD1"])
        self.assertEqual(next_leverage(snapshot, "XAUUSD1", {4: dec(0), 5: dec(10000), 10: dec(20000)}, threshold=10000), 10)
        self.assertIsNone(next_leverage(snapshot, "XAUUSD1", {4: dec(10000)}, threshold=10000))
        self.capacities({4: 0, 10: 20000})
        self.engine.tick_account("test")
        self.assertEqual(self.f.store.intent("test")["target"], 10)

    def test_high_leverage_flat_account_never_resets_to_lower_available_tier(self):
        self.position(10)
        self.capacities({4: 100000, 5: 100000, 10: 0, 20: 0})
        with patch.object(self.f.broker, "set_leverage", side_effect=AssertionError("must not reduce")), \
             patch.object(self.f.broker, "submit", side_effect=AssertionError("no 10x capacity")):
            self.engine.tick_account("test")
        self.assertEqual(self.f.broker.state["leverages"]["XAUUSD1"], 10)
        self.assertIsNone(self.f.store.intent("test"))

    def test_high_leverage_flat_account_can_open_at_current_available_tier(self):
        self.position(10)
        self.capacities({4: 100000, 10: 100000, 20: 0})
        with patch.object(self.f.broker, "set_leverage", side_effect=AssertionError("must retain 10x")):
            self.engine.tick_account("test")
        long = self.f.broker.snapshot(["XAUUSD1"]).pair("XAUUSD1")[0]
        self.assertEqual(long.leverage, 10)
        self.assertGreater(long.qty, 0)

    def test_executor_rejects_direct_reduction_and_stale_selection(self):
        self.position(1)
        stale = self.f.broker.snapshot(["XAUUSD1"])
        self.position(10)
        executor = Executor(self.f.store, self.f.broker, self.f.market)
        with patch.object(self.f.broker, "set_leverage", side_effect=AssertionError("must not write")):
            for old in (10, 1):
                with self.subTest(old=old), self.assertRaises(TradingError):
                    executor.leverage(self.f.account, "XAUUSD1", old, 4, snapshot=stale)
        self.assertIsNone(self.f.store.intent("test"))

    def test_equal_leverage_is_a_noop(self):
        self.position(5)
        with patch.object(self.f.broker, "set_leverage", side_effect=AssertionError("must not rewrite")):
            Executor(self.f.store, self.f.broker, self.f.market).leverage(self.f.account, "XAUUSD1", 5, 5)
        self.assertIsNone(self.f.store.intent("test"))

    def test_live_and_paper_brokers_block_reduction_at_mutation_boundary(self):
        self.position(10)
        snapshot = self.f.broker.snapshot(["XAUUSD1"])
        api = Mock()
        live = LiveBroker({}, self.f.market, api=api)
        with patch.object(live, "snapshot", return_value=snapshot):
            with self.assertRaises(TradingError):
                live.set_leverage("XAUUSD1", 4)
            self.assertEqual(live.set_leverage("XAUUSD1", 10)["leverage"], 10)
        api.call.assert_not_called()
        with self.assertRaises(TradingError):
            self.f.broker.set_leverage("XAUUSD1", 4)
        self.assertEqual(self.f.broker.state["leverages"]["XAUUSD1"], 10)

    def test_old_pending_downgrade_is_held_without_execution(self):
        self.position(10)
        intent = {"id": "old-down", "kind": "leverage", "account_id": "test", "symbol": "XAUUSD1",
                  "previous": 10, "target": 4, "created_at": time.time(), "status": "pending"}
        self.f.store.save_intent(intent)
        with patch.object(self.f.broker, "set_leverage", side_effect=AssertionError("must not restore downgrade")):
            self.engine.tick_account("test")
            self.engine.retry("test")
            self.engine.tick_account("test")
        self.assertEqual(self.f.store.intent("test")["status"], "attention")
        self.assertFalse(self.f.store.account("test")["enabled"])

    def test_unconfirmed_upgrade_never_opens_or_resubmits(self):
        self.position(1, "1")
        with patch.object(self.f.broker, "set_leverage", return_value={}) as change, \
             patch.object(self.f.broker, "submit", side_effect=AssertionError("must wait for confirmation")):
            self.engine.tick_account("test")
            self.engine.tick_account("test")
            self.engine.tick_account("test")
        self.assertEqual(change.call_count, 1)
        self.assertEqual(self.f.store.intent("test")["target"], 4)

    def test_higher_actual_leverage_satisfies_confirmation_without_reduction(self):
        self.position(1, "1")
        self.engine.tick_account("test")
        self.position(10, "1")
        with patch.object(self.f.broker, "set_leverage", side_effect=AssertionError("must not revert to target")):
            self.engine.tick_account("test")
            self.engine.tick_account("test")
        self.assertIsNone(self.f.store.intent("test"))
        self.assertEqual(self.f.broker.state["leverages"]["XAUUSD1"], 10)
        self.assertGreater(dec(self.f.broker.state["positions"]["XAUUSD1:LONG"]["qty"]), 1)

    def test_unfunded_first_add_does_not_permanently_block_a_higher_tier(self):
        self.position(1, "1")
        self.engine.tick_account("test")
        self.engine.tick_account("test")
        no_balance = replace(self.f.broker.snapshot(["XAUUSD1"]), available=dec(0))
        with patch.object(self.f.broker, "snapshot", return_value=no_balance), \
             patch.object(self.f.broker, "submit", side_effect=AssertionError("cannot fund a minimum pair")):
            self.engine.tick_account("test")
        self.engine.tick_account("test")
        self.assertEqual(self.f.store.intent("test")["target"], 5)
