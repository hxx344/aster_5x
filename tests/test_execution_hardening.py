from dataclasses import replace
import time
import unittest
from unittest.mock import patch

from trading.exchange import ExchangeError
from trading.execution import Executor
from trading.models import Plan, TradingError, dec, plan_pair, wire
from trading.paper import PaperBroker
from trading.store import Store
from .helpers import Fixture


class ExecutionHardeningTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.executor = Executor(self.f.store, self.f.broker, self.f.market)

    def prepared(self, partial=False):
        snapshot = self.f.broker.snapshot(["XAUUSD1"])
        book = self.f.market.book("XAUUSD1")
        plan = plan_pair(snapshot, book, self.f.market.rules["XAUUSD1"], {4: dec(500000)}, self.f.account["policy"])
        original = self.f.broker.submit

        def send(orders):
            return [original(orders[:1])[0], {"code": -2019}] if partial else original(orders)

        with patch.object(self.f.broker, "submit", side_effect=send), \
             patch.object(self.executor, "reconcile", return_value="simulated process stop"):
            self.executor.open_pair(self.f.account, snapshot, "XAUUSD1", plan, book)
        return self.f.store.intent("test")

    def test_stale_entry_and_invalid_quantity_never_create_intent(self):
        snapshot = self.f.broker.snapshot(["XAUUSD1"])
        book = self.f.market.book("XAUUSD1")
        cases = [(replace(snapshot, timestamp=time.time() - 9), book, ".005"),
                 (snapshot, replace(book, timestamp=time.time() - 4), ".005"),
                 (snapshot, book, "0"), (snapshot, book, "-.001"),
                 (snapshot, book, ".0015"), (snapshot, book, "10000.001")]
        for account, quote, qty in cases:
            with self.subTest(qty=qty), patch.object(self.f.broker, "submit") as submit:
                with self.assertRaises(TradingError):
                    self.executor.open_pair(self.f.account, account, "XAUUSD1", Plan(dec(qty)), quote)
                submit.assert_not_called()
                self.assertIsNone(self.f.store.intent("test"))

    def test_stale_filled_snapshot_cannot_acknowledge_batch(self):
        original = self.prepared()
        stale = replace(self.f.broker.snapshot(["XAUUSD1"]), timestamp=time.time() - 9)
        with patch.object(self.f.broker, "snapshot", return_value=stale), \
             patch.object(self.f.broker, "submit") as submit:
            with self.assertRaisesRegex(TradingError, "快照已过期"):
                self.executor.reconcile(self.f.account)
            submit.assert_not_called()
        self.assertEqual(self.f.store.intent("test")["id"], original["id"])
        self.assertIsNone(self.f.store.get("campaign:test"))

    def test_snapshot_expiring_during_repair_quote_does_not_submit_repair(self):
        self.prepared(partial=True)
        snapshot = self.f.broker.snapshot(["XAUUSD1"])
        book = self.f.market.book("XAUUSD1")

        def delayed_quote(symbol):
            snapshot.timestamp = time.time() - 9
            return replace(book, timestamp=time.time())

        with patch.object(self.f.broker, "snapshot", return_value=snapshot), \
             patch.object(self.f.market, "book", side_effect=delayed_quote), \
             patch.object(self.f.broker, "submit") as submit:
            with self.assertRaisesRegex(TradingError, "快照已过期"):
                self.executor.reconcile(self.f.account)
            submit.assert_not_called()
        self.assertEqual(self.f.store.intent("test")["repairs"], [])

    def test_external_orders_or_revoked_permission_stop_compensation(self):
        self.prepared(partial=True)
        snapshot = self.f.broker.snapshot(["XAUUSD1"])
        for changes in ({"can_trade": False}, {"open_orders": [{"symbol": "XAUUSD1"}]}):
            with self.subTest(changes=changes), \
                 patch.object(self.f.broker, "snapshot", return_value=replace(snapshot, **changes)), \
                 patch.object(self.f.broker, "submit") as submit:
                self.executor.reconcile(self.f.account)
                submit.assert_not_called()
                self.assertEqual(self.f.store.intent("test")["repairs"], [])
                account = self.f.store.account("test")
                self.assertFalse(account["enabled"])
                self.assertIn("补偿", account["pause_reason"])

    def test_crash_after_repair_acceptance_recovers_without_duplicate_close(self):
        for side in ("LONG", "SHORT"):
            self.f.broker.state["positions"]["XAUUSD1:" + side] = {"qty": "5", "entry": "4412.015"}
        self.f.broker.save()
        self.prepared(partial=True)
        original = self.f.broker.submit

        def accepted_then_crashed(orders):
            original(orders)
            raise RuntimeError("simulated process loss after accepted repair")

        with patch.object(self.f.broker, "submit", side_effect=accepted_then_crashed):
            with self.assertRaisesRegex(RuntimeError, "simulated process loss"):
                self.executor.reconcile(self.f.account)
        self.assertEqual(len(self.f.store.intent("test")["repairs"]), 1)
        store = Store(self.f.store.path)
        broker = PaperBroker("test", self.f.market, store)
        with patch.object(broker, "submit", side_effect=AssertionError("must not resend repair")):
            Executor(store, broker, self.f.market).reconcile(self.f.account)
        self.assertIsNone(store.intent("test"))
        self.assertTrue(store.get("post_fill_check:test"))
        self.assertEqual(tuple(p.qty for p in broker.snapshot(["XAUUSD1"]).pair("XAUUSD1")), (5, 5))

    def test_crash_before_submission_preserves_intent_and_never_blindly_resends(self):
        snapshot = self.f.broker.snapshot(["XAUUSD1"])
        book = self.f.market.book("XAUUSD1")
        with patch.object(self.f.store, "event", side_effect=RuntimeError("simulated loss before submit")), \
             patch.object(self.f.broker, "submit") as submit:
            with self.assertRaises(RuntimeError):
                self.executor.open_pair(self.f.account, snapshot, "XAUUSD1", Plan(dec(".12")), book)
            submit.assert_not_called()
        intent = self.f.store.intent("test")
        intent["created_at"] = time.time() - 130
        self.f.store.save_intent(intent)
        with patch.object(self.f.broker, "submit", side_effect=AssertionError("must not resend entry")), \
             patch.object(self.f.broker, "query", side_effect=ExchangeError("exchange order not yet visible", code=-2013)):
            self.executor.reconcile(self.f.account)
        self.assertEqual(self.f.store.intent("test")["status"], "attention")
        self.assertFalse(self.f.store.account("test")["enabled"])

    def test_stale_snapshot_cannot_confirm_leverage(self):
        self.executor.leverage(self.f.account, "XAUUSD1", 4, 5)
        snapshot = replace(self.f.broker.snapshot(["XAUUSD1"]), timestamp=time.time() - 9)
        with patch.object(self.f.broker, "snapshot", return_value=snapshot):
            with self.assertRaisesRegex(TradingError, "快照已过期"):
                self.executor.reconcile(self.f.account)
        self.assertIsNotNone(self.f.store.intent("test"))
        self.assertIsNone(self.f.store.get("open_after_leverage:test:XAUUSD1"))

    def test_zero_fill_terminal_receipts_do_not_require_an_average_price(self):
        for average in (None, "NaN", ""):
            with self.subTest(average=average):
                snapshot = self.f.broker.snapshot(["XAUUSD1"])
                book = self.f.market.book("XAUUSD1")

                def expired(orders):
                    rows = [{**order, "clientOrderId": order["newClientOrderId"], "executedQty": "0", "status": "EXPIRED"} for order in orders]
                    if average is not None:
                        for row in rows:
                            row["avgPrice"] = average
                    return rows

                with patch.object(self.f.broker, "submit", side_effect=expired) as submit:
                    self.executor.open_pair(self.f.account, snapshot, "XAUUSD1", Plan(dec(".12")), book)
                    self.assertEqual(submit.call_count, 1)
                self.assertIsNone(self.f.store.intent("test"))
                self.assertIsNone(self.f.store.get("campaign:test"))
                self.assertTrue(self.f.store.get("post_fill_check:test"))


class NumericBoundsTests(unittest.TestCase):
    def test_pathological_decimal_inputs_are_rejected_before_expansion(self):
        for value in ("9" * 129, "1e1000000000", "1e-1000000000", "0e1000000000"):
            with self.subTest(value=value[:30]), self.assertRaises(TradingError):
                wire(value)
        self.assertEqual(wire("1e-18"), "0.000000000000000001")
        self.assertEqual(wire("999999999999.123456789"), "999999999999.123456789")
