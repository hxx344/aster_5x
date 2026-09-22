"""Cycle fills reconcile exact quantities across price changes and restarts."""
import copy
from unittest import TestCase
from unittest.mock import patch

from tests import test_cycle_execution as execution_cases
from trading.cycle_execution import CycleExecutor
from trading.models import dec
from trading.paper import PaperBroker
from trading.store import Store


SYMBOL = "XAUUSD1"


class CycleQuantityReconciliationTests(TestCase):
    setUp = execution_cases.CycleExecutionTests.setUp
    progress_now = execution_cases.CycleExecutionTests.progress_now
    plan = execution_cases.CycleExecutionTests.plan
    open = execution_cases.CycleExecutionTests.open
    quantities = execution_cases.CycleExecutionTests.quantities

    def pending_price_moved_open(self):
        original_book, original_submit = self.f.market.book, self.f.broker.submit
        execution_started = []
        def moved_book(symbol):
            book = original_book(symbol)
            if execution_started and symbol == SYMBOL:
                book.bid = book.mark = dec("4999.760775")
                book.ask = dec("5000.03348996475")
            return book
        def slipped(orders):
            execution_started.append(True)
            return original_submit(orders)
        with patch.object(self.f.market, "book", side_effect=moved_book), \
             patch.object(self.f.broker, "submit", side_effect=slipped), \
             patch.object(self.executor, "reconcile", return_value="restart before confirmation"):
            self.open()
        return self.f.store.intent("test")

    def restart(self):
        store = Store(self.f.store.path)
        broker = PaperBroker("test", self.f.market, store)
        return store, broker, CycleExecutor(store, broker, self.f.market)

    def test_restart_confirms_persisted_quantities_without_resending_or_reducing(self):
        self.pending_price_moved_open()
        store, broker, executor = self.restart()
        with patch.object(broker, "submit", side_effect=AssertionError("confirmed quantities must not trade")):
            executor.reconcile(store.account("test"))
            executor.reconcile(store.account("test"))
        progress = self.progress_now()
        self.assertEqual(progress["phase"], "holding")
        self.assertEqual(progress["quantities"], {"LONG": "2", "SHORT": "2"})
        self.assertIsNotNone(progress["opened_at"])
        self.assertEqual(self.quantities(), (2, 2))
        self.assertIsNone(store.intent("test"))
        self.assertEqual(self.f.store.cycle_daily_volume("test")["trade_count"], 2)

    def test_restart_finishes_an_already_started_legacy_amount_rollback(self):
        # Preserve unequal original holdings as well as the old repair direction.
        for side, qty in (("LONG", "1"), ("SHORT", "0.5")):
            self.f.broker.state["positions"][SYMBOL + ":" + side] = {"qty": qty, "entry": "4412"}
        self.f.broker.save()
        intent = self.pending_price_moved_open()
        repair = self.executor.order(SYMBOL, "LONG", "SELL", dec(1), "legacy-amount-repair")
        receipt = self.f.broker.submit([repair])[0]
        intent.update(status="repair", repair_attempts=1, rollback_reason="实际成交金额超出本轮范围")
        intent["repairs"].append(repair)
        intent["receipts"][repair["newClientOrderId"]] = receipt
        self.f.store.save_intent(intent)
        store, broker, executor = self.restart()
        with patch.object(broker, "submit", wraps=broker.submit) as submit:
            executor.reconcile(store.account("test"))
        self.assertEqual(submit.call_count, 1)
        self.assertEqual([(o["positionSide"], o["side"], o["quantity"]) for o in submit.call_args.args[0]],
                         [("LONG", "SELL", "1"), ("SHORT", "BUY", "2")])
        self.assertEqual(self.quantities(), (dec(1), dec("0.5")))
        self.assertEqual(self.progress_now()["phase"], "waiting_open")
        self.assertIsNone(self.progress_now()["opened_at"])
        self.assertIsNone(store.intent("test"))

    def test_tiny_underfill_and_overfill_receipts_still_pause_without_trading(self):
        pending = self.pending_price_moved_open()
        owner = copy.deepcopy(self.f.account)
        for quantity, status, message in (
                ("1.999999999999999999", "FILLED", "完全成交回执数量与委托数量不一致"),
                ("2.000000000000000001", "CANCELED", "订单成交数量或状态无效")):
            with self.subTest(quantity=quantity):
                account, intent = copy.deepcopy(owner), copy.deepcopy(pending)
                self.f.store.save_account(account)
                receipt = intent["receipts"][intent["orders"][0]["newClientOrderId"]]
                receipt.update(executedQty=quantity, status=status)
                self.f.store.save_intent(intent)
                with patch.object(self.f.broker, "submit", side_effect=AssertionError("invalid receipt must not trade")):
                    reason = self.executor.reconcile(account)
                self.assertIn(message, reason)
                self.assertEqual(self.f.store.intent("test")["status"], "attention")
                self.assertFalse(self.f.store.account("test")["enabled"])
                self.assertIsNone(self.progress_now()["opened_at"])
                self.assertEqual(self.quantities(), (2, 2))
