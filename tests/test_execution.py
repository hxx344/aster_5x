import copy
import time
import unittest
from unittest.mock import patch

from trading.engine import Engine
from trading.exchange import AmbiguousOrder, ExchangeError
from trading.execution import Executor
from trading.models import TradingError, dec, plan_pair
from trading.paper import PaperBroker
from trading.store import Store
from .helpers import Fixture


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.executor = Executor(self.f.store, self.f.broker, self.f.market)

    def open(self):
        snapshot = self.f.broker.snapshot(["XAUUSD1"])
        book = self.f.market.book("XAUUSD1")
        plan = plan_pair(snapshot, book, self.f.market.rules["XAUUSD1"], {4: dec(500000)}, self.f.account["policy"])
        return self.executor.open_pair(self.f.account, snapshot, "XAUUSD1", plan, book)

    def test_timeout_after_acceptance_is_queried_without_resubmission(self):
        original = self.f.broker.submit
        def accepted(orders):
            original(orders)
            raise AmbiguousOrder("test timeout after acceptance")
        with patch.object(self.f.broker, "submit", side_effect=accepted) as send:
            self.open()
            self.assertEqual(send.call_count, 1)
        self.assertIsNone(self.f.store.intent("test"))
        long, short = self.f.broker.snapshot(["XAUUSD1"]).pair("XAUUSD1")
        self.assertEqual(long.qty, short.qty)
        self.assertGreater(long.qty, 0)
        self.assertEqual(len(self.f.store.get("campaign:test")["batches"]), 1)

    def test_unknown_order_keeps_durable_intent_and_never_retries_entry(self):
        with patch.object(self.f.broker, "submit", side_effect=AmbiguousOrder("test timeout")) as send:
            self.open()
            pending = self.f.store.intent("test")
            pending["created_at"] = time.time() - 130
            self.f.store.save_intent(pending)
            self.executor.reconcile(self.f.account)
            self.assertEqual(send.call_count, 1)
        self.assertEqual(self.f.store.intent("test")["status"], "attention")
        self.assertFalse(self.f.store.account("test")["enabled"])

    def test_one_leg_failure_closes_only_new_excess(self):
        # Existing balanced positions belong to the user and must remain intact.
        for side in ("LONG", "SHORT"):
            self.f.broker.state["positions"]["XAUUSD1:" + side] = {"qty": "1", "entry": "4412"}
        self.f.broker.save()
        original = self.f.broker.submit
        def one_leg(orders):
            if len(orders) == 2:
                long = original(orders[:1])[0]
                return [long, {"code": -2019}]
            return original(orders)
        with patch.object(self.f.broker, "submit", side_effect=one_leg) as send:
            self.open()
            self.assertEqual(send.call_count, 2)
            repair = send.call_args_list[1].args[0][0]
            self.assertEqual((repair["positionSide"], repair["side"]), ("LONG", "SELL"))
        long, short = self.f.broker.snapshot(["XAUUSD1"]).pair("XAUUSD1")
        self.assertEqual((long.qty, short.qty), (1, 1))
        self.assertIsNone(self.f.store.intent("test"))
        self.assertIsNone(self.f.store.get("campaign:test"))

    def test_external_position_change_stops_compensation(self):
        original = self.f.broker.submit
        def external_change(orders):
            first = original(orders[:1])[0]
            self.f.broker.state["positions"]["XAUUSD1:LONG"]["qty"] = "7"
            return [first, {"code": -2019}]
        with patch.object(self.f.broker, "submit", side_effect=external_change) as send:
            self.open()
            self.assertEqual(send.call_count, 1)
        self.assertEqual(self.f.store.intent("test")["status"], "attention")

    def test_restart_recovers_existing_receipts_without_new_orders(self):
        with patch.object(self.executor, "reconcile", return_value="simulated process stop"):
            self.open()
        restored_store = Store(self.f.store.path)
        broker = PaperBroker("test", self.f.market, restored_store)
        with patch.object(broker, "submit", side_effect=AssertionError("must not submit")):
            Executor(restored_store, broker, self.f.market).reconcile(self.f.account)
        self.assertIsNone(restored_store.intent("test"))
        self.assertEqual(len(restored_store.get("campaign:test")["batches"]), 1)

    def test_leverage_is_confirmed_from_account_after_unknown_response(self):
        original = self.f.broker.set_leverage
        def change(symbol, leverage):
            original(symbol, leverage)
            raise AmbiguousOrder("test leverage timeout")
        with patch.object(self.f.broker, "set_leverage", side_effect=change) as update:
            self.executor.leverage(self.f.account, "XAUUSD1", 4, 5)
            self.executor.reconcile(self.f.account)
            self.assertEqual(update.call_count, 1)
        self.assertIsNone(self.f.store.intent("test"))

    def test_notification_aggregation_and_retry_are_persistent(self):
        self.open()
        live = {**self.f.account, "mode": "live"}
        self.f.store.finish_campaign(live, "本轮完成", dec(".1"))
        self.f.store.finish_campaign(live, "重复结束", dec(".1"))
        pending = self.f.store.due_notifications()
        self.assertEqual(len(pending), 1)
        self.assertIn("双向开仓完成", pending[0]["message"])
        self.assertIn("保证金占用率（总占用保证金 / 总权益）", pending[0]["message"])
        self.f.store.notification_result(pending[0], False)
        self.assertEqual(self.f.store.pending_notifications(), 1)
        self.assertEqual(self.f.store.due_notifications(), [])
        self.f.store.notification_result(pending[0], True)
        self.assertEqual(Store(self.f.store.path).pending_notifications(), 0)

    def test_paper_completion_never_enters_external_outbox(self):
        self.open()
        self.f.store.finish_campaign(self.f.account, "测试完成", dec(".1"))
        self.assertEqual(self.f.store.pending_notifications(), 0)

    def test_receipt_identity_and_quantities_are_validated(self):
        order = self.executor.order("XAUUSD1", "LONG", "BUY", dec(1), "test")
        row = {**order, "clientOrderId": "different", "executedQty": "1", "avgPrice": "100", "status": "FILLED"}
        from trading.models import TradingError
        with self.assertRaises(TradingError):
            self.executor.validate_receipt(order, row)
        row["clientOrderId"], row["executedQty"] = "test", "2"
        with self.assertRaises(TradingError):
            self.executor.validate_receipt(order, row)

    def test_accounts_have_separate_orders_and_funds(self):
        self.open()
        other = PaperBroker("second", self.f.market, self.f.store)
        self.assertEqual(other.snapshot(["XAUUSD1"]).pair("XAUUSD1")[0].qty, 0)
        self.assertIsNone(self.f.store.intent("second"))


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.engine = Engine(self.f.store, market=self.f.market)
        self.engine.brokers["test"] = self.f.broker
        self.engine.poll_market("XAUUSD1")

    def test_open_then_upgrade_and_continue_using_new_tier(self):
        self.engine.tick_account("test")
        initial_qty = self.f.broker.snapshot(["XAUUSD1"]).pair("XAUUSD1")[0].qty
        self.assertGreater(initial_qty, 0)
        self.engine.tick_account("test")
        self.assertEqual(self.f.store.intent("test")["target"], 5)
        self.engine.tick_account("test")
        with self.engine.lock:
            self.engine.markets["XAUUSD1"]["capacities"].update({"4": "0", "10": "0", "20": "0"})
        self.engine.tick_account("test")
        long = self.f.broker.snapshot(["XAUUSD1"]).pair("XAUUSD1")[0]
        self.assertEqual(long.leverage, 5)
        self.assertGreater(long.qty, initial_qty)

    def test_pause_prevents_new_orders(self):
        self.engine.enable("test", False)
        with patch.object(self.f.broker, "submit", side_effect=AssertionError("must not trade")):
            self.engine.tick_account("test")
        self.assertEqual(self.f.broker.snapshot(["XAUUSD1"]).pair("XAUUSD1")[0].qty, 0)

    def test_live_guard_is_required_even_with_account_enabled(self):
        live = {**self.f.account, "mode": "live"}
        self.f.store.save_account(live)
        with patch.dict("os.environ", {}, clear=True), patch.object(self.f.broker, "submit", side_effect=AssertionError("must not trade")):
            self.engine.tick_account("test")
        self.assertIn("ASTER_ALLOW_LIVE", self.engine.state()["accounts"][0]["reason"])

    def test_same_real_account_with_different_signers_cannot_run_twice(self):
        self.engine.brokers.clear()
        first = {**self.f.account, "mode": "live"}
        second = {**first, "id": "second", "env_prefix": "ASTER_SECOND"}
        credentials = [{"user": "0x123", "signer": "0xabc"}, {"user": "0x123", "signer": "0xdef"}]
        with patch("trading.engine.credentials_for", side_effect=credentials), patch("trading.engine.LiveBroker") as factory:
            self.engine.broker(first)
            with self.assertRaises(TradingError):
                self.engine.broker(second)
            self.assertEqual(factory.call_count, 1)
