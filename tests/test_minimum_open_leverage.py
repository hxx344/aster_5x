import copy
import time
import unittest
from unittest.mock import patch

from trading.execution import Executor
from trading.models import Plan, TradingError, dec, next_leverage, plan_pair
from .helpers import Fixture


class MinimumOpenLeverageTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.executor = Executor(self.f.store, self.f.broker, self.f.market)
        self.symbol = "XAUUSD1"

    def position(self, leverage, qty="1"):
        self.f.broker.state["leverages"][self.symbol] = leverage
        for side in ("LONG", "SHORT"):
            self.f.broker.state["positions"][self.symbol + ":" + side] = {"qty": qty, "entry": "4412"}
        self.f.broker.save()

    def test_planner_blocks_low_leverage_even_with_capacity_for_existing_or_flat_positions(self):
        for leverage in (1, 2, 3):
            for qty in ("0", "1"):
                with self.subTest(leverage=leverage, qty=qty):
                    self.position(leverage, qty)
                    before = copy.deepcopy(self.f.broker.state)
                    plan = plan_pair(self.f.broker.snapshot([self.symbol]), self.f.market.book(self.symbol),
                                     self.f.market.rules[self.symbol], {leverage: dec(500000)}, self.f.account["policy"])
                    self.assertEqual(plan.qty, 0)
                    self.assertIn("低于 4x", plan.reason)
                    self.assertEqual(self.f.broker.state, before)

    def test_execution_boundary_rejects_a_supplied_plan_below_four_before_creating_intent(self):
        for leverage in (1, 2, 3):
            for qty in ("0", "1"):
                with self.subTest(leverage=leverage, qty=qty):
                    self.position(leverage, qty)
                    before = copy.deepcopy(self.f.broker.state)
                    with patch.object(self.f.broker, "submit", side_effect=AssertionError("must not submit")):
                        with self.assertRaisesRegex(TradingError, "低于 4x"):
                            self.executor.open_pair(self.f.account, self.f.broker.snapshot([self.symbol]),
                                                    self.symbol, Plan(dec("0.005")), self.f.market.book(self.symbol))
                    self.assertIsNone(self.f.store.intent("test"))
                    self.assertEqual(self.f.store.events(), [])
                    self.assertEqual(self.f.broker.state, before)

    def test_four_and_higher_actual_leverages_can_plan_and_open(self):
        for leverage in (4, 5, 10, 20):
            with self.subTest(leverage=leverage):
                self.position(leverage)
                snapshot, book = self.f.broker.snapshot([self.symbol]), self.f.market.book(self.symbol)
                plan = plan_pair(snapshot, book, self.f.market.rules[self.symbol],
                                 {leverage: dec(500000)}, self.f.account["policy"])
                self.assertGreater(plan.qty, 0)
                self.executor.open_pair(self.f.account, snapshot, self.symbol, plan, book)
                long, short = self.f.broker.snapshot([self.symbol]).pair(self.symbol)
                self.assertEqual((long.qty, short.qty), (dec(1) + plan.qty, dec(1) + plan.qty))
                self.assertEqual(long.leverage, leverage)

    def test_low_leverage_holdings_can_still_upgrade_and_confirm(self):
        for leverage in (1, 2, 3):
            with self.subTest(leverage=leverage):
                self.position(leverage)
                snapshot = self.f.broker.snapshot([self.symbol])
                self.assertEqual(next_leverage(snapshot, self.symbol, {4: dec(500000)}, threshold=10000), 4)
                with patch.object(self.f.broker, "submit", side_effect=AssertionError("upgrade must not open")):
                    self.executor.leverage(self.f.account, self.symbol, leverage, 4)
                    self.executor.reconcile(self.f.account)
                long, short = self.f.broker.snapshot([self.symbol]).pair(self.symbol)
                self.assertEqual((long.qty, short.qty, long.leverage), (1, 1, 4))
                self.assertIsNone(self.f.store.intent("test"))

    def legacy_batch(self, leverage, one_leg):
        self.position(leverage)
        book = self.f.market.book(self.symbol)
        orders = [self.executor.order(self.symbol, side, "BUY" if side == "LONG" else "SELL", dec("0.005"),
                                      book.ask if side == "LONG" else book.bid, f"legacy-{leverage}-{side}")
                  for side in ("LONG", "SHORT")]
        # Model orders accepted by the paper exchange before the new gate existed.
        receipts = self.f.broker.submit(orders[:1] if one_leg else orders)
        if one_leg:
            receipts.append({**orders[1], "clientOrderId": orders[1]["newClientOrderId"],
                             "status": "REJECTED", "executedQty": "0", "avgPrice": "0"})
        intent = {"id": f"legacy-{leverage}", "kind": "pair", "account_id": "test", "symbol": self.symbol,
                  "leverage": leverage, "status": "pending", "created_at": time.time(),
                  "baseline": {"LONG": "1", "SHORT": "1"}, "orders": orders,
                  "receipts": {row["clientOrderId"]: row for row in receipts}, "repairs": [], "repair_attempts": 0}
        self.f.store.save_intent(intent)

    def test_legacy_low_leverage_fills_can_still_be_reconciled_without_new_orders(self):
        for leverage in (1, 2, 3):
            with self.subTest(leverage=leverage):
                self.legacy_batch(leverage, one_leg=False)
                with patch.object(self.f.broker, "submit", side_effect=AssertionError("must not resubmit legacy orders")):
                    self.executor.reconcile(self.f.account)
                self.assertIsNone(self.f.store.intent("test"))
                long, short = self.f.broker.snapshot([self.symbol]).pair(self.symbol)
                self.assertEqual((long.qty, short.qty, long.leverage), (dec("1.005"), dec("1.005"), leverage))

    def test_legacy_low_leverage_single_leg_can_still_be_closed_without_touching_old_holdings(self):
        for leverage in (1, 2, 3):
            with self.subTest(leverage=leverage):
                self.legacy_batch(leverage, one_leg=True)
                with patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
                    self.executor.reconcile(self.f.account)
                submit.assert_called_once()
                repair, = submit.call_args.args[0]
                self.assertEqual((repair["positionSide"], repair["side"], repair["quantity"]), ("LONG", "SELL", "0.005"))
                self.assertIsNone(self.f.store.intent("test"))
                long, short = self.f.broker.snapshot([self.symbol]).pair(self.symbol)
                self.assertEqual((long.qty, short.qty, long.leverage), (1, 1, leverage))


if __name__ == "__main__":
    unittest.main()
