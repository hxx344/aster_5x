import unittest
from unittest.mock import patch

from trading.exchange import AmbiguousOrder, ExchangeError, RequestNotSent
from trading.execution import Executor
from trading.models import Plan, dec
from trading.paper import PaperBroker
from trading.store import Store
from .helpers import Fixture


class ExecutionNotSentTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.executor = Executor(self.f.store, self.f.broker, self.f.market)

    def open(self):
        return self.executor.open_pair(self.f.account, self.f.broker.snapshot(["XAUUSD1"]), "XAUUSD1",
                                       Plan(dec("0.12")), self.f.market.book("XAUUSD1"))

    def test_local_budget_rejection_completes_without_unknown_order_queries_or_resubmission(self):
        with patch.object(self.f.broker, "submit", side_effect=RequestNotSent("local request budget exhausted", retry_after=60)) as submit, \
             patch.object(self.f.broker, "query", side_effect=AssertionError("request was never sent")):
            self.open()
        submit.assert_called_once()
        self.assertIsNone(self.f.store.intent("test"))
        completed = self.executor.last_completed_intent
        self.assertEqual(completed["status"], "aborted")
        self.assertEqual(len(completed["receipts"]), 2)
        self.assertTrue(all(row["status"] == "REJECTED" and row["executedQty"] == "0" and row["local_not_sent"]
                            for row in completed["receipts"].values()))
        self.assertEqual(tuple(p.qty for p in self.executor.last_snapshot.pair("XAUUSD1")), (0, 0))
        self.assertIn("本地未发送", self.f.store.events()[0]["message"])

    def test_exchange_rejection_codes_are_retained_without_raw_error_messages(self):
        rows = [{"code": -2019, "msg": "private raw error text"}, {"code": -2027, "msg": "another private detail"}]
        with patch.object(self.f.broker, "submit", return_value=rows), \
             patch.object(self.f.broker, "query", side_effect=AssertionError("both rejections are terminal")):
            self.open()
        completed = self.executor.last_completed_intent
        self.assertEqual({row["reject_code"] for row in completed["receipts"].values()}, {-2019, -2027})
        message = self.f.store.events()[0]["message"]
        self.assertIn("多头 REJECTED（code=-2019）", message)
        self.assertIn("空头 REJECTED（code=-2027）", message)
        self.assertNotIn("private", str(completed) + message)

    def test_unknown_network_result_still_preserves_intent_and_does_not_mark_local_rejection(self):
        with patch.object(self.f.broker, "submit", side_effect=AmbiguousOrder("network response lost")) as submit, \
             patch.object(self.f.broker, "query", side_effect=ExchangeError("exchange order not yet visible", code=-2013)):
            self.open()
            self.executor.reconcile(self.f.account)
        submit.assert_called_once()
        pending = self.f.store.intent("test")
        self.assertIsNotNone(pending)
        self.assertEqual(pending["receipts"], {})
        self.assertIsNone(self.executor.last_snapshot)
        self.assertIsNone(self.executor.last_completed_intent)

    def test_exchange_ambiguous_batch_rows_remain_unresolved(self):
        with patch.object(self.f.broker, "submit", return_value=[{"code": -1006}, {"code": -1007}]), \
             patch.object(self.f.broker, "query", side_effect=ExchangeError("exchange order not yet visible", code=-2013)):
            self.open()
        self.assertEqual(self.f.store.intent("test")["receipts"], {})
        self.assertIsNone(self.executor.last_completed_intent)

    def test_local_budget_rejection_of_leverage_clears_intent_and_preserves_retry_delay(self):
        rejection = RequestNotSent("local request budget exhausted", retry_after=45)
        self.executor.last_snapshot = self.f.broker.snapshot(["XAUUSD1"])
        with patch.object(self.f.broker, "set_leverage", side_effect=rejection), \
             patch.object(self.f.store, "save_intent", wraps=self.f.store.save_intent) as save:
            with self.assertRaises(RequestNotSent) as caught:
                self.executor.leverage(self.f.account, "XAUUSD1", 5, 10)
        self.assertIs(caught.exception, rejection)
        self.assertEqual(caught.exception.retry_after, 45)
        self.assertEqual(save.call_args.args[0]["status"], "aborted")
        self.assertEqual(save.call_args.args[0]["last_error"], "local request budget exhausted")
        self.assertIsNone(self.f.store.intent("test"))
        self.assertIsNone(self.f.store.get("post_fill_check:test"))
        self.assertTrue(self.f.store.account("test")["enabled"])
        self.assertEqual(self.f.broker.state["leverages"]["XAUUSD1"], 5)
        self.assertIsNone(self.executor.last_snapshot)
        self.assertIsNone(self.executor.last_completed_intent)

    def test_local_budget_rejection_before_leverage_intent_does_not_create_unknown_work(self):
        with patch.object(self.f.broker, "snapshot", side_effect=RequestNotSent("read budget exhausted", retry_after=10)), \
             patch.object(self.f.broker, "set_leverage", side_effect=AssertionError("must not write")):
            with self.assertRaises(RequestNotSent):
                self.executor.leverage(self.f.account, "XAUUSD1", 5, 10)
        self.assertIsNone(self.f.store.intent("test"))

    def test_failed_repair_budget_keeps_legacy_exposure_for_recovery_without_querying_absent_repairs(self):
        submit, save = self.f.broker.submit, self.f.store.save_intent
        rejection = RequestNotSent("reserved budget exhausted", retry_after=60)
        def one_leg_then_local_rejection(orders):
            if len(orders) == 2:
                return [submit(orders[:1])[0], {"code": -2019}]
            raise rejection

        def persist(intent):
            if any(row.get("local_not_sent") for row in intent["receipts"].values()):
                self.assertEqual(intent["repair_attempts"], 0)
            return save(intent)

        with patch.object(self.f.broker, "submit", side_effect=one_leg_then_local_rejection) as send, \
             patch.object(self.f.store, "save_intent", side_effect=persist), \
             patch.object(self.f.broker, "query", side_effect=AssertionError("no absent order query")):
            with self.assertRaises(RequestNotSent) as caught:
                self.open()
        self.assertIs(caught.exception, rejection)
        self.assertEqual(caught.exception.retry_after, 60)
        self.assertEqual(send.call_count, 2)
        pending = self.f.store.intent("test")
        self.assertEqual(pending["status"], "repair")
        self.assertEqual(pending["repair_attempts"], 0)
        self.assertEqual(len(pending["repairs"]), 1)
        self.assertTrue(all(pending["receipts"][order["newClientOrderId"]]["local_not_sent"] for order in pending["repairs"]))
        self.assertTrue(self.f.store.account("test")["enabled"])
        self.assertIsNone(self.executor.last_completed_intent)
        self.assertIsNone(self.executor.last_snapshot)
        self.assertEqual(tuple(p.qty for p in self.f.broker.snapshot(["XAUUSD1"]).pair("XAUUSD1")), (dec("0.12"), 0))

        # A new process can finish after the budget recovers, without querying
        # or resending either original leg or the known-absent repair.
        store = Store(self.f.store.path)
        broker = PaperBroker("test", self.f.market, store)
        restarted = Executor(store, broker, self.f.market)
        with patch.object(broker, "submit", wraps=broker.submit) as recovered_send, \
             patch.object(broker, "query", side_effect=AssertionError("terminal receipts need no query")):
            restarted.reconcile(self.f.account)
        recovered_send.assert_called_once()
        repair, = recovered_send.call_args.args[0]
        self.assertEqual((repair["positionSide"], repair["side"], repair["quantity"]), ("LONG", "SELL", "0.12"))
        prior_ids = {order["newClientOrderId"] for order in pending["orders"] + pending["repairs"]}
        self.assertNotIn(repair["newClientOrderId"], prior_ids)
        self.assertIsNone(store.intent("test"))
        self.assertTrue(store.account("test")["enabled"])
        self.assertEqual(restarted.last_completed_intent["repair_attempts"], 1)
        self.assertEqual(tuple(p.qty for p in restarted.last_snapshot.pair("XAUUSD1")), (0, 0))


if __name__ == "__main__":
    unittest.main()
