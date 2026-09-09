"""Fixed per-side entry floor, without restricting recovery of older batches."""
from dataclasses import replace
from fractions import Fraction
import time
import unittest
from unittest.mock import patch

from trading.engine import Engine, validate_account
from trading.execution import Executor
from trading.models import Book, Plan, TradingError, dec, plan_pair
from .helpers import Fixture


SYMBOL = "XAUUSD1"


class BatchFloorTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.book = Book(dec(1000), dec(1000), dec(50), dec(50), dec(1000), time.time())
        original = self.f.market.book
        patcher = patch.object(self.f.market, "book", side_effect=lambda symbol:
            replace(self.book, timestamp=time.time()) if symbol == SYMBOL else original(symbol))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.snapshot = self.f.broker.snapshot([SYMBOL])
        self.rule = self.f.market.rules[SYMBOL]
        self.policy = self.f.account["policy"]
        self.executor = Executor(self.f.store, self.f.broker, self.f.market)

    def plan(self, *, snapshot=None, book=None, rule=None, policy=None):
        return plan_pair(snapshot or self.snapshot, book or self.book, rule or self.rule,
                         {4: dec(500000)}, policy or self.policy)

    def test_exactly_500_per_side_can_open_with_aligned_quantity(self):
        policy = {**self.policy, "order_notional": "500"}
        plan = self.plan(policy=policy)
        self.assertEqual(plan.qty, dec("0.5"))
        self.executor.open_pair({**self.f.account, "policy": policy}, self.snapshot, SYMBOL, plan, self.book)
        pair = self.f.broker.snapshot([SYMBOL]).pair(SYMBOL)
        self.assertEqual(tuple(p.notional for p in pair), (500, 500))
        self.assertIsNone(self.f.store.intent("test"))

    def test_below_500_after_quantity_rounding_waits(self):
        plan = self.plan(book=replace(self.book, bid_qty=dec(".4999")))
        self.assertEqual(plan.qty, 0)
        self.assertIn("500 USD1", plan.reason)

    def test_fraction_below_500_cannot_round_up_to_pass_the_floor(self):
        price = dec("999.99999999999999999999999999999999")
        book = replace(self.book, bid=price, ask=price, mark=price, bid_qty=dec(".5"))
        self.assertLess(Fraction(dec(".5")) * Fraction(price), 500)
        self.assertEqual(self.plan(book=book).qty, 0)
        with patch.object(self.f.broker, "submit") as submit, self.assertRaisesRegex(TradingError, "500 USD1"):
            self.executor.open_pair(self.f.account, self.snapshot, SYMBOL, Plan(dec(".5")), book)
        submit.assert_not_called()
        self.assertIsNone(self.f.store.intent("test"))

    def test_executor_rejects_a_direct_small_plan_before_persisting_or_sending(self):
        with patch.object(self.f.store, "save_intent") as save, patch.object(self.f.broker, "submit") as submit:
            with self.assertRaisesRegex(TradingError, "500 USD1"):
                self.executor.open_pair(self.f.account, self.snapshot, SYMBOL, Plan(dec(".499")), self.book)
        save.assert_not_called()
        submit.assert_not_called()

    def test_stricter_exchange_minimum_still_applies(self):
        rule = replace(self.rule, min_notional=dec(750))
        self.assertEqual(self.plan(rule=rule, book=replace(self.book, bid_qty=dec(".7"))).qty, 0)
        self.assertEqual(self.plan(rule=rule, book=replace(self.book, bid_qty=dec(".8"))).qty, dec(".8"))
        with patch.dict(self.f.market.rules, {SYMBOL: rule}), self.assertRaises(TradingError):
            self.executor.open_pair(self.f.account, self.snapshot, SYMBOL, Plan(dec(".7")), self.book)

    def test_notional_floor_uses_mark_price_not_a_higher_ask(self):
        book = replace(self.book, ask=dec("1000.1"), bid_qty=dec(".499"))
        self.assertEqual(self.plan(book=book).qty, 0)

    def test_small_remaining_cash_does_not_force_a_larger_order(self):
        self.assertEqual(self.plan(snapshot=replace(self.snapshot, available=dec(250))).qty, 0)
        plan = self.plan(snapshot=replace(self.snapshot, available=dec("250.4")))
        self.assertEqual(plan.qty, dec(".5"))

    def test_risk_limit_is_not_relaxed_to_make_a_minimum_batch(self):
        tight = replace(self.snapshot, equity=dec(1000), wallet=dec(1000), available=dec(1000))
        self.assertEqual(self.plan(snapshot=tight, policy={**self.policy, "margin_limit": ".25"}).qty, 0)

    def test_old_lower_cap_is_preserved_and_cannot_open(self):
        policy = {**self.policy, "order_notional": "100"}
        self.assertEqual(self.plan(policy=policy).qty, 0)
        self.assertEqual(policy["order_notional"], "100")
        account = {**self.f.account, "enabled": False, "policy": policy}
        self.f.store.save_account(account)
        engine = Engine(self.f.store, market=self.f.market)
        engine.brokers["test"] = self.f.broker
        with patch.object(self.f.broker, "snapshot", side_effect=AssertionError("invalid cap must be rejected first")), \
             self.assertRaisesRegex(TradingError, "500 USD1"):
            engine.enable("test", True)
        self.assertFalse(self.f.store.account("test")["enabled"])
        self.assertEqual(self.f.store.account("test")["policy"]["order_notional"], "100")

    def test_new_policy_rejects_caps_below_500_and_accepts_the_boundary(self):
        for cap in ("1", "499.99999999999999999999999999999999"):
            with self.subTest(cap=cap), self.assertRaisesRegex(TradingError, "500"):
                validate_account({**self.f.account, "policy": {**self.policy, "order_notional": cap}})
        self.assertEqual(validate_account({**self.f.account, "policy": {**self.policy, "order_notional": "500"}})
                         ["policy"]["order_notional"], "500")

    def test_legacy_small_filled_batch_is_reconciled_without_new_entry(self):
        orders = [Executor.order(SYMBOL, side, "BUY" if side == "LONG" else "SELL", dec(".1"), "legacy-" + side)
                  for side in ("LONG", "SHORT")]
        receipts = self.f.broker.submit(orders)
        self.f.store.save_intent({"id": "legacy-small", "account_id": "test", "symbol": SYMBOL, "kind": "pair",
            "status": "pending", "leverage": 4, "created_at": time.time(), "baseline": {"LONG": "0", "SHORT": "0"},
            "orders": orders, "receipts": {row["clientOrderId"]: row for row in receipts}, "repairs": [], "repair_attempts": 0})
        engine = Engine(self.f.store, market=self.f.market)
        engine.brokers["test"] = self.f.broker
        with patch.object(self.f.broker, "submit", side_effect=AssertionError("must only reconcile existing fills")):
            engine.tick_account("test")
        self.assertIsNone(self.f.store.intent("test"))
        self.assertEqual(tuple(p.notional for p in self.f.broker.snapshot([SYMBOL]).pair(SYMBOL)), (100, 100))

    def test_small_repair_after_partial_fill_is_allowed(self):
        original = self.f.broker.submit
        def partial(orders):
            if len(orders) == 1:
                return original(orders)
            first = original(orders[:1])[0]
            second = original([{**orders[1], "quantity": str(dec(orders[1]["quantity"]) - dec(".01"))}])[0]
            second.update(status="EXPIRED", origQty=orders[1]["quantity"])
            self.f.broker.save()
            return [first, second]
        with patch.object(self.f.broker, "submit", side_effect=partial) as submit:
            self.executor.open_pair(self.f.account, self.snapshot, SYMBOL, self.plan(), self.book)
        self.assertEqual(submit.call_count, 2)
        repair, = submit.call_args.args[0]
        self.assertEqual((repair["positionSide"], repair["side"], repair["quantity"]), ("LONG", "SELL", "0.01"))
        self.assertLess(dec(repair["quantity"]) * self.book.mark, 500)
        self.assertIsNone(self.f.store.intent("test"))
        self.assertEqual(tuple(p.qty for p in self.f.broker.snapshot([SYMBOL]).pair(SYMBOL)), (dec(".99"), dec(".99")))


if __name__ == "__main__":
    unittest.main()
