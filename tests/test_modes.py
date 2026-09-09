import copy
from dataclasses import replace
import unittest
from unittest.mock import patch

from trading.engine import Engine
from trading.execution import Executor
from trading.models import AccountModeError, dec, plan_pair
from .helpers import Fixture


class FixedModeTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.engine = Engine(self.f.store, market=self.f.market)
        self.engine.brokers["test"] = self.f.broker
        self.engine.poll_market("XAUUSD1")

    def invalid_snapshots(self):
        snapshot = self.f.broker.snapshot(["XAUUSD1"])
        isolated = copy.deepcopy(snapshot)
        isolated.positions[0].isolated = True
        return (isolated, replace(snapshot, hedge_mode=False), replace(snapshot, multi_assets=True))

    def test_each_fixed_mode_is_required_at_start(self):
        self.engine.enable("test", False)
        for snapshot in self.invalid_snapshots():
            with self.subTest(modes=snapshot.mode_checks(["XAUUSD1"])):
                with patch.object(self.f.broker, "snapshot", return_value=snapshot) as read:
                    with self.assertRaises(AccountModeError):
                        self.engine.enable("test", True)
                    self.assertTrue(read.call_args.kwargs["fresh_modes"])
                self.assertFalse(self.f.store.account("test")["enabled"])

    def test_running_mode_mismatch_stays_paused_after_modes_recover(self):
        for snapshot in self.invalid_snapshots():
            with self.subTest(modes=snapshot.mode_checks(["XAUUSD1"])):
                self.f.store.save_account({**self.f.account, "enabled": True})
                with patch.object(self.f.broker, "snapshot", return_value=snapshot), \
                     patch.object(self.f.broker, "submit", side_effect=AssertionError("must not trade")), \
                     patch.object(self.f.broker, "set_leverage", side_effect=AssertionError("must not change leverage")):
                    self.engine.tick_account("test")
                self.assertFalse(self.f.store.account("test")["enabled"])
                state = self.engine.state()["accounts"][0]
                self.assertEqual(state["status"], "attention")
                self.assertFalse(all(state["snapshot"]["mode_checks"].values()))
                events = len(self.f.store.events())
                with patch.object(self.f.broker, "snapshot", return_value=snapshot):
                    self.engine.tick_account("test")
                self.assertEqual(len(self.f.store.events()), events)
                with patch.object(self.f.broker, "submit", side_effect=AssertionError("must not auto-resume")):
                    self.engine.tick_account("test")
                self.assertFalse(self.f.store.account("test")["enabled"])

    def test_flat_second_market_in_isolated_mode_blocks_whole_account(self):
        account = self.f.store.account("test")
        account["policy"]["symbols"].append("SPCXUSD1")
        self.f.store.save_account(account)
        snapshot = self.f.broker.snapshot(account["policy"]["symbols"])
        snapshot.pair("SPCXUSD1")[0].isolated = True
        with patch.object(self.f.broker, "snapshot", return_value=snapshot), \
             patch.object(self.f.broker, "submit", side_effect=AssertionError("must not trade first market")):
            self.engine.tick_account("test")
        self.assertFalse(self.f.store.account("test")["enabled"])

    def test_pending_intent_is_preserved_without_reconciliation_when_mode_invalid(self):
        Executor(self.f.store, self.f.broker, self.f.market).leverage(self.f.account, "XAUUSD1", 4, 5)
        original = self.f.store.intent("test")
        bad = replace(self.f.broker.snapshot(["XAUUSD1"]), multi_assets=True)
        with patch.object(self.f.broker, "snapshot", return_value=bad), \
             patch("trading.engine.Executor.reconcile", side_effect=AssertionError("must not reconcile in wrong mode")):
            self.engine.tick_account("test")
        pending = self.f.store.intent("test")
        self.assertEqual(pending["id"], original["id"])
        self.assertEqual(pending["status"], "attention")
        self.assertFalse(self.f.store.account("test")["enabled"])

    def test_mode_change_after_one_leg_fill_blocks_compensation(self):
        snapshot = self.f.broker.snapshot(["XAUUSD1"])
        book = self.f.market.book("XAUUSD1")
        plan = plan_pair(snapshot, book, self.f.market.rules["XAUUSD1"], {4: dec(500000)}, self.f.account["policy"])
        original = self.f.broker.submit
        def one_leg(orders):
            return [original(orders[:1])[0], {"code": -2019}]
        bad = replace(snapshot, multi_assets=True)
        with patch.object(self.f.broker, "submit", side_effect=one_leg) as submit, \
             patch.object(self.f.broker, "snapshot", return_value=bad):
            Executor(self.f.store, self.f.broker, self.f.market).open_pair(self.f.account, snapshot, "XAUUSD1", plan, book)
        self.assertEqual(submit.call_count, 1)
        pending = self.f.store.intent("test")
        self.assertEqual(pending["status"], "attention")
        self.assertEqual(pending["repairs"], [])
        self.assertGreater(dec(self.f.broker.state["positions"]["XAUUSD1:LONG"]["qty"]), 0)

    def test_executor_does_not_adjust_leverage_in_invalid_mode(self):
        bad = self.invalid_snapshots()[0]
        with patch.object(self.f.broker, "snapshot", return_value=bad), \
             patch.object(self.f.broker, "set_leverage", side_effect=AssertionError("must not change leverage")):
            with self.assertRaises(AccountModeError):
                Executor(self.f.store, self.f.broker, self.f.market).leverage(self.f.account, "XAUUSD1", 4, 5)
        self.assertIsNone(self.f.store.intent("test"))
