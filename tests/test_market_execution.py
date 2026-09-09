import copy
from dataclasses import replace
import json
import time
import unittest
from unittest.mock import patch

from trading.engine import Engine
from trading.exchange import LiveBroker
from trading.execution import Executor
from trading.models import Plan, dec, plan_pair
from trading.paper import PaperBroker
from trading.store import Store
from .helpers import Fixture


class MarketExecutionTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.calls = []
        self.one_leg = False
        self.entry_book = None
        test = self

        class Gateway:
            budget = None

            def call(self, method, path, params, **kwargs):
                test.calls.append((method, path, copy.deepcopy(params), kwargs))
                if method == "POST":
                    orders = json.loads(params["batchOrders"]) if path.endswith("/batchOrders") else [params]
                    if len(orders) == 2 and test.one_leg:
                        rows = [test.f.broker.submit(orders[:1])[0], {"code": -2019}]
                    elif len(orders) == 2 and test.entry_book:
                        with patch.object(test.f.market, "book", return_value=test.entry_book):
                            rows = test.f.broker.submit(orders)
                    else:
                        rows = test.f.broker.submit(orders)
                    return rows if path.endswith("/batchOrders") else rows[0]
                if method == "GET":
                    return test.f.broker.query(params["symbol"], params["origClientOrderId"])
                raise AssertionError("Unexpected API request")

        self.live = LiveBroker({}, self.f.market, api=Gateway())
        self.reader = patch.object(self.live, "snapshot", side_effect=self.f.broker.snapshot)
        self.reader.start()
        self.addCleanup(self.reader.stop)
        self.executor = Executor(self.f.store, self.live, self.f.market)

    def open(self, qty=".12"):
        snapshot = self.f.broker.snapshot(["XAUUSD1"])
        return self.executor.open_pair(self.f.account, snapshot, "XAUUSD1", Plan(dec(qty)), self.f.market.book("XAUUSD1"))

    def assert_market(self, order):
        self.assertEqual(order["type"], "MARKET")
        self.assertEqual(order["newOrderRespType"], "RESULT")
        self.assertNotIn("price", order)
        self.assertNotIn("timeInForce", order)
        self.assertNotIn("reduceOnly", order)

    def test_signed_batch_contains_equal_quantity_hedge_market_orders(self):
        self.open()
        self.assertEqual(len(self.calls), 1)
        method, path, params, kwargs = self.calls[0]
        self.assertEqual((method, path), ("POST", "/fapi/v3/batchOrders"))
        self.assertTrue(kwargs["signed"])
        orders = json.loads(params["batchOrders"])
        self.assertEqual([(o["positionSide"], o["side"], o["quantity"]) for o in orders],
                         [("LONG", "BUY", "0.12"), ("SHORT", "SELL", "0.12")])
        for order in orders:
            self.assert_market(order)
        self.assertIsNone(self.f.store.intent("test"))
        self.assertEqual(tuple(p.qty for p in self.f.broker.snapshot(["XAUUSD1"]).pair("XAUUSD1")), (dec(".12"), dec(".12")))

    def test_single_leg_repair_is_a_market_close_and_preserves_old_positions(self):
        for side in ("LONG", "SHORT"):
            self.f.broker.state["positions"]["XAUUSD1:" + side] = {"qty": "1", "entry": "4412"}
        self.f.broker.save()
        self.one_leg = True
        self.open()
        self.assertEqual(len(self.calls), 2)
        method, path, repair, kwargs = self.calls[-1]
        self.assertEqual((method, path), ("POST", "/fapi/v3/order"))
        self.assertTrue(kwargs["signed"])
        self.assert_market(repair)
        self.assertEqual((repair["positionSide"], repair["side"], dec(repair["quantity"])), ("LONG", "SELL", dec(".12")))
        self.assertEqual(tuple(p.qty for p in self.f.broker.snapshot(["XAUUSD1"]).pair("XAUUSD1")), (dec(1), dec(1)))

    def test_price_move_after_planning_fills_and_is_recorded_at_actual_market_prices(self):
        book = self.f.market.book("XAUUSD1")
        self.entry_book = replace(book, bid=book.bid + 10, ask=book.ask + 10, mark=book.mark + 10)
        self.open()
        quantities = self.f.store.get("campaign:test")["batches"][0]["quantities"]
        self.assertEqual(dec(quantities["notional"]), dec(".12") * (self.entry_book.bid + self.entry_book.ask))
        self.assertIsNone(self.f.store.intent("test"))

    def test_partial_market_fills_reconcile_and_close_only_actual_excess(self):
        self.entry_book = replace(self.f.market.book("XAUUSD1"), bid_qty=dec(".003"), ask_qty=dec(".006"))
        self.open()
        self.assertEqual(len(self.calls), 2)
        repair = self.calls[-1][2]
        self.assert_market(repair)
        self.assertEqual(dec(repair["quantity"]), dec(".003"))
        self.assertEqual(tuple(p.qty for p in self.f.broker.snapshot(["XAUUSD1"]).pair("XAUUSD1")), (dec(".003"), dec(".003")))
        self.assertIsNone(self.f.store.intent("test"))
        self.assertEqual(self.f.store.get("campaign:test")["batches"][0]["quantities"]["long_qty"], "0.003")

    def test_market_slippage_that_exceeds_actual_margin_limit_persistently_pauses(self):
        self.f.broker.state["wallet"] = "1000"
        self.f.broker.save()
        engine = Engine(self.f.store, market=self.f.market)
        engine.brokers["test"] = self.f.broker
        engine.poll_market("XAUUSD1")
        submit = self.f.broker.submit
        moved = replace(self.f.market.book("XAUUSD1"), ask=dec(4500), bid=dec(4300))

        def changed_at_execution(orders):
            for order in orders:
                self.assert_market(order)
            with patch.object(self.f.market, "book", return_value=moved):
                return submit(orders)

        with patch.object(self.f.broker, "submit", side_effect=changed_at_execution) as sent:
            engine.tick_account("test")
        sent.assert_called_once()
        self.assertIsNone(self.f.store.intent("test"))
        self.assertFalse(self.f.store.account("test")["enabled"])
        self.assertGreater(self.f.broker.snapshot(["XAUUSD1"]).ratio, dec(".5"))
        self.assertIn("保证金占用率超过", self.f.store.account("test")["pause_reason"])

    def test_partial_fill_excess_below_market_minimum_pauses_without_invalid_repairs(self):
        self.f.market.rules["XAUUSD1"].min_qty = dec(".01")
        self.entry_book = replace(self.f.market.book("XAUUSD1"), bid_qty=dec(".019"), ask_qty=dec(".02"))
        self.open()
        self.assertEqual(len(self.calls), 1)
        pending = self.f.store.intent("test")
        self.assertEqual((pending["status"], pending["repair_attempts"], pending["repairs"]), ("attention", 0, []))
        self.assertFalse(self.f.store.account("test")["enabled"])
        self.assertIn("市价最小数量", pending["last_error"])

    def test_market_minimum_notional_uses_mark_price_when_it_differs_from_bbo(self):
        snapshot = self.f.broker.snapshot(["XAUUSD1"])
        book = replace(self.f.market.book("XAUUSD1"), bid=dec(20000), ask=dec(20001), mark=dec(10000))
        for depth, expected in ((".03", 0), (".05", ".05")):
            with self.subTest(depth=depth):
                current = replace(book, bid_qty=dec(depth), ask_qty=dec(depth))
                plan = plan_pair(snapshot, current, self.f.market.rules["XAUUSD1"], {4: dec(500000)},
                                 {**self.f.account["policy"], "order_notional": "2000"})
                self.assertEqual(plan.qty, dec(expected))

    def test_restart_queries_legacy_limit_orders_without_converting_or_resubmitting(self):
        book = self.f.market.book("XAUUSD1")
        orders = [{"symbol": "XAUUSD1", "positionSide": side, "side": "BUY" if side == "LONG" else "SELL",
                   "type": "LIMIT", "timeInForce": "FOK", "quantity": "0.01", "newOrderRespType": "RESULT",
                   "price": str(book.ask if side == "LONG" else book.bid), "newClientOrderId": "legacy-" + side}
                  for side in ("LONG", "SHORT")]
        self.f.broker.submit(orders)
        self.f.store.save_intent({"id": "legacy", "account_id": "test", "kind": "pair", "symbol": "XAUUSD1",
                                  "leverage": 4, "status": "pending", "created_at": time.time(), "orders": orders,
                                  "baseline": {"LONG": "0", "SHORT": "0"}, "receipts": {}, "repairs": [], "repair_attempts": 0})
        restored = Store(self.f.store.path)
        broker = PaperBroker("test", self.f.market, restored)
        with patch.object(broker, "submit", side_effect=AssertionError("legacy entry must not be resubmitted")), \
             patch.object(broker, "query", wraps=broker.query) as query:
            Executor(restored, broker, self.f.market).reconcile(self.f.account)
        self.assertEqual(query.call_count, 2)
        self.assertIsNone(restored.intent("test"))
        self.assertEqual(tuple(p.qty for p in broker.snapshot(["XAUUSD1"]).pair("XAUUSD1")), (dec(".01"), dec(".01")))
