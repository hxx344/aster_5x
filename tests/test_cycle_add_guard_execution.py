"""Cycle ownership gates ordinary additions without blocking recovery."""
import copy
import unittest
from unittest.mock import patch

from trading.cycle import DEFAULT_CYCLE
from trading.execution import Executor
from trading.models import TradingError, dec, plan_pair
from trading.paper import PaperBroker
from .helpers import Fixture, account


class CycleAddGuardExecutionTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.executor = Executor(self.f.store, self.f.broker, self.f.market)

    def plan(self, owner=None, broker=None):
        owner, broker = owner or self.f.account, broker or self.f.broker
        snapshot = broker.snapshot(["XAUUSD1"])
        book = self.f.market.book("XAUUSD1")
        tier = snapshot.pair("XAUUSD1")[0].leverage
        plan = plan_pair(snapshot, book, self.f.market.rules["XAUUSD1"],
                         {tier: dec(500000)}, owner["policy"])
        self.assertGreater(plan.qty, 0)
        return snapshot, plan, book

    def save_cycle(self, enabled=True):
        current = self.f.store.account("test")
        current["cycle"] = {**DEFAULT_CYCLE, "enabled": enabled}
        self.f.store.save_account(current)
        return current

    def assert_blocked(self, owner, selected):
        snapshot, plan, book = selected
        with patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit, \
             patch.object(self.f.store, "save_intent", wraps=self.f.store.save_intent) as save_intent:
            with self.assertRaisesRegex(TradingError, "本账户 XAUUSD1.*循环.*禁止"):
                self.executor.open_pair(owner, snapshot, "XAUUSD1", plan, book)
        submit.assert_not_called()
        save_intent.assert_not_called()
        self.assertIsNone(self.f.store.intent("test"))
        self.assertEqual(tuple(p.qty for p in self.f.broker.snapshot(["XAUUSD1"]).pair("XAUUSD1")), (0, 0))

    def test_cycle_rejects_ordinary_pair_with_available_capacity_at_every_tier(self):
        for tier in (5, 10, 20):
            with self.subTest(tier=tier):
                self.f.broker.set_leverage("XAUUSD1", tier)
                selected = self.plan()
                self.assert_blocked(self.save_cycle(), selected)

    def test_saved_cycle_rejects_plan_selected_using_stale_account_mapping(self):
        stale = copy.deepcopy(self.f.account)
        selected = self.plan(stale)
        self.save_cycle()
        self.assert_blocked(stale, selected)

    def test_account_cycle_cannot_be_bypassed_with_an_older_saved_mapping(self):
        selected = self.plan()
        current = {**self.f.account, "cycle": {**DEFAULT_CYCLE, "enabled": True}}
        self.assert_blocked(current, selected)

    def test_saved_cycle_is_checked_after_quote_validation_before_intent_creation(self):
        selected = self.plan()
        with patch.object(selected[2], "require_fresh", side_effect=lambda: self.save_cycle()):
            self.assert_blocked(self.f.account, selected)

    def test_other_account_can_open_same_symbol_without_using_cycle_account_funds(self):
        self.save_cycle()
        other = account("second")
        self.f.store.save_account(other)
        broker = PaperBroker("second", self.f.market, self.f.store)
        snapshot, plan, book = self.plan(other, broker)
        with patch.object(broker, "submit", wraps=broker.submit) as submit:
            Executor(self.f.store, broker, self.f.market).open_pair(other, snapshot, "XAUUSD1", plan, book)
        self.assertEqual(submit.call_count, 1)
        long, short = broker.snapshot(["XAUUSD1"]).pair("XAUUSD1")
        self.assertGreater(long.qty, 0)
        self.assertEqual(long.qty, short.qty)
        self.assertEqual(tuple(p.qty for p in self.f.broker.snapshot(["XAUUSD1"]).pair("XAUUSD1")), (0, 0))
        self.assertIsNone(self.f.store.intent("second"))
        self.assertIsNone(self.f.store.intent("test"))

    def test_disabled_cycle_allows_ordinary_pair(self):
        owner = self.save_cycle(enabled=False)
        snapshot, plan, book = self.plan(owner)
        with patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            self.executor.open_pair(owner, snapshot, "XAUUSD1", plan, book)
        self.assertEqual(submit.call_count, 1)
        self.assertGreater(self.f.broker.snapshot(["XAUUSD1"]).pair("XAUUSD1")[0].qty, 0)

    def test_existing_ordinary_receipts_reconcile_after_cycle_configuration_changes(self):
        snapshot, plan, book = self.plan()
        with patch.object(self.executor, "reconcile", return_value="process interrupted"):
            self.executor.open_pair(self.f.account, snapshot, "XAUUSD1", plan, book)
        owner = self.save_cycle()
        with patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            self.executor.reconcile(owner)
        submit.assert_not_called()
        self.assertIsNone(self.f.store.intent("test"))
        long, short = self.f.broker.snapshot(["XAUUSD1"]).pair("XAUUSD1")
        self.assertEqual((long.qty, short.qty), (plan.qty, plan.qty))

    def test_existing_partial_pair_still_reduces_only_new_excess_after_cycle_enabled(self):
        for side in ("LONG", "SHORT"):
            self.f.broker.state["positions"]["XAUUSD1:" + side] = {"qty": "1", "entry": "4412"}
        self.f.broker.save()
        snapshot, plan, book = self.plan()
        original_submit = self.f.broker.submit

        def partial(orders):
            return [original_submit(orders[:1])[0], {"code": -2019}]

        with patch.object(self.f.broker, "submit", side_effect=partial), \
             patch.object(self.executor, "reconcile", return_value="process interrupted"):
            self.executor.open_pair(self.f.account, snapshot, "XAUUSD1", plan, book)
        owner = self.save_cycle()
        with patch.object(self.f.broker, "submit", wraps=original_submit) as submit:
            self.executor.reconcile(owner)
        self.assertEqual(submit.call_count, 1)
        self.assertEqual([(order["positionSide"], order["side"], dec(order["quantity"]))
                          for order in submit.call_args.args[0]], [("LONG", "SELL", plan.qty)])
        self.assertEqual(tuple(p.qty for p in self.f.broker.snapshot(["XAUUSD1"]).pair("XAUUSD1")), (1, 1))
        self.assertIsNone(self.f.store.intent("test"))
