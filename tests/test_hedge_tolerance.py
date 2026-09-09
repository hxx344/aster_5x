from dataclasses import replace
import unittest
from unittest.mock import patch

from trading.engine import Engine
from trading.execution import Executor
from trading.models import Plan, TradingError, dec, hedge_balanced, next_leverage, plan_pair
from trading.paper import PaperBroker
from trading.store import Store
from .helpers import Fixture


class HedgeToleranceTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.executor = Executor(self.f.store, self.f.broker, self.f.market)

    def seed(self, long="5", short="5", leverage=4):
        self.f.broker.state["leverages"]["XAUUSD1"] = leverage
        for side, qty in (("LONG", long), ("SHORT", short)):
            self.f.broker.state["positions"]["XAUUSD1:" + side] = {"qty": qty, "entry": "4412.015"}
        self.f.broker.save()
        return self.f.broker.snapshot(["XAUUSD1"])

    def open(self, qty="0.005"):
        snapshot = self.f.broker.snapshot(["XAUUSD1"])
        return self.executor.open_pair(self.f.account, snapshot, "XAUUSD1", Plan(dec(qty)), self.f.market.book("XAUUSD1"))

    def only_side(self, side):
        submit = self.f.broker.submit

        def partial(orders):
            if len(orders) == 2:
                return [submit([o])[0] if o["positionSide"] == side else {"code": -2019} for o in orders]
            return submit(orders)

        return partial

    def test_boundary_is_inclusive_symmetric_and_zero_safe(self):
        for long, short, expected in (("0", "0", True), ("1", "0", False), ("0", "1", False),
                                      ("1", ".999", True), (".999", "1", True),
                                      ("1", ".9991", True), ("1", ".998999999999", False)):
            with self.subTest(long=long, short=short):
                self.assertEqual(hedge_balanced(long, short), expected)
        with self.assertRaises(TradingError):
            hedge_balanced("-1", "1")

    def test_existing_boundary_allows_sizing_and_upgrade_for_each_market(self):
        for symbol in self.f.market.rules:
            for smaller, allowed in ((".999", True), (".998999", False)):
                with self.subTest(symbol=symbol, smaller=smaller):
                    snapshot = self.f.broker.snapshot([symbol])
                    long, short = snapshot.pair(symbol)
                    long.qty, short.qty = dec(1), dec(smaller)
                    caps = {4: dec(500000), 5: dec(500000)}
                    plan = plan_pair(snapshot, self.f.market.book(symbol), self.f.market.rules[symbol], caps, self.f.account["policy"])
                    self.assertEqual(plan.qty > 0, allowed)
                    self.assertEqual(next_leverage(snapshot, symbol, caps) == 5, allowed)
                    if not allowed:
                        self.assertIn("0.1%", plan.reason)

    def test_unequal_existing_1x_positions_upgrade_confirm_then_add(self):
        self.seed("1", ".999", 1)
        engine = Engine(self.f.store, market=self.f.market)
        engine.brokers["test"] = self.f.broker
        engine.poll_market("XAUUSD1")
        engine.tick_account("test")
        self.assertEqual(self.f.store.intent("test")["target"], 4)
        engine.tick_account("test")
        engine.tick_account("test")
        long, short = self.f.broker.snapshot(["XAUUSD1"]).pair("XAUUSD1")
        self.assertEqual(long.leverage, 4)
        self.assertGreater(long.qty, 1)
        self.assertEqual(long.qty - short.qty, dec(".001"))
        self.assertIsNone(self.f.store.intent("test"))

    def test_execution_entry_and_fresh_leverage_read_reject_above_limit(self):
        stale = self.seed("1", "1")
        current = self.seed("1", ".998")
        with patch.object(self.f.broker, "submit") as submit, patch.object(self.f.broker, "set_leverage") as change:
            with self.assertRaisesRegex(TradingError, "0.1%"):
                self.executor.open_pair(self.f.account, current, "XAUUSD1", Plan(dec(".005")), self.f.market.book("XAUUSD1"))
            with self.assertRaisesRegex(TradingError, "0.1%"):
                self.executor.leverage(self.f.account, "XAUUSD1", 4, 5, snapshot=stale)
            submit.assert_not_called()
            change.assert_not_called()
        self.assertIsNone(self.f.store.intent("test"))

    def test_one_side_fill_at_boundary_is_retained_and_recorded_per_side(self):
        for side in ("LONG", "SHORT"):
            with self.subTest(side=side):
                self.seed("4.995", "4.995")
                self.f.store.put("campaign:test", {"id": side, "batches": []})
                with patch.object(self.f.broker, "submit", side_effect=self.only_side(side)) as submit:
                    self.open()
                    self.assertEqual(submit.call_count, 1)
                long, short = self.f.broker.snapshot(["XAUUSD1"]).pair("XAUUSD1")
                self.assertEqual(abs(long.qty - short.qty) / max(long.qty, short.qty), dec(".001"))
                self.assertIsNone(self.f.store.intent("test"))
                quantities = self.f.store.get("campaign:test")["batches"][0]["quantities"]
                self.assertEqual(dec(quantities["long_qty"]), dec(".005") if side == "LONG" else 0)
                self.assertEqual(dec(quantities["short_qty"]), dec(".005") if side == "SHORT" else 0)
                price = self.f.market.book("XAUUSD1").ask if side == "LONG" else self.f.market.book("XAUUSD1").bid
                self.assertEqual(dec(quantities["notional"]), dec(".005") * price)
                self.assertTrue(self.f.store.get("post_fill_check:test"))

    def test_repair_stops_once_total_position_returns_within_tolerance(self):
        self.seed()
        shallow = replace(self.f.market.book("XAUUSD1"), bid_qty=dec(".015"))
        with patch.object(self.f.market, "book", return_value=shallow), \
             patch.object(self.f.broker, "submit", side_effect=self.only_side("LONG")) as submit:
            self.open(".02")
            self.assertEqual(submit.call_count, 2)
            self.assertEqual(dec(submit.call_args.args[0][0]["quantity"]), dec(".015"))
        long, short = self.f.broker.snapshot(["XAUUSD1"]).pair("XAUUSD1")
        self.assertEqual((long.qty, short.qty), (dec("5.005"), dec(5)))
        self.assertTrue(hedge_balanced(long.qty, short.qty))
        quantities = self.f.store.get("campaign:test")["batches"][0]["quantities"]
        self.assertEqual(dec(quantities["long_qty"]), dec(".005"))
        self.assertEqual(dec(quantities["notional"]), dec(".005") * shallow.ask)

    def test_repair_uses_actual_total_difference_and_keeps_baseline(self):
        for smaller_side in ("LONG", "SHORT"):
            with self.subTest(smaller_side=smaller_side):
                self.seed("4.995" if smaller_side == "LONG" else "5", "4.995" if smaller_side == "SHORT" else "5")
                with patch.object(self.f.broker, "submit", side_effect=self.only_side(smaller_side)) as submit:
                    self.open(".02")
                    self.assertEqual(submit.call_count, 2)
                    self.assertEqual(dec(submit.call_args.args[0][0]["quantity"]), dec(".015"))
                long, short = self.f.broker.snapshot(["XAUUSD1"]).pair("XAUUSD1")
                self.assertEqual((long.qty, short.qty), (5, 5))

    def test_tiny_external_position_change_still_requires_exact_reconciliation(self):
        self.seed()
        submit = self.f.broker.submit

        def external(orders):
            receipts = submit(orders)
            row = self.f.broker.state["positions"]["XAUUSD1:LONG"]
            row["qty"] = str(dec(row["qty"]) + dec(".000001"))
            return receipts

        with patch.object(self.f.broker, "submit", side_effect=external) as sent:
            self.open()
            self.assertEqual(sent.call_count, 1)
        long, short = self.f.broker.snapshot(["XAUUSD1"]).pair("XAUUSD1")
        self.assertTrue(hedge_balanced(long.qty, short.qty))
        self.assertEqual(self.f.store.intent("test")["status"], "attention")
        self.assertIsNone(self.f.store.get("campaign:test"))

    def test_repair_handles_baseline_quantities_from_an_older_lot_step(self):
        self.seed("1", ".9995")
        self.f.market.rules["XAUUSD1"].step = dec(".002")
        with patch.object(self.f.broker, "submit", side_effect=self.only_side("SHORT")) as submit:
            self.open(".002")
            self.assertEqual(submit.call_count, 2)
            self.assertEqual(dec(submit.call_args.args[0][0]["quantity"]), dec(".002"))
        long, short = self.f.broker.snapshot(["XAUUSD1"]).pair("XAUUSD1")
        self.assertEqual((long.qty, short.qty), (dec(1), dec(".9995")))
        self.assertIsNone(self.f.store.intent("test"))
        self.assertIsNone(self.f.store.get("campaign:test"))

    def test_restart_recovers_asymmetric_batch_without_resubmission(self):
        self.seed()
        with patch.object(self.f.broker, "submit", side_effect=self.only_side("SHORT")), \
             patch.object(self.executor, "reconcile", return_value="simulated stop"):
            self.open()
        store = Store(self.f.store.path)
        broker = PaperBroker("test", self.f.market, store)
        executor = Executor(store, broker, self.f.market)
        with patch.object(broker, "submit", side_effect=AssertionError("must not submit")):
            executor.reconcile(self.f.account)
            executor.reconcile(self.f.account)
        batches = store.get("campaign:test")["batches"]
        self.assertEqual(len(batches), 1)
        self.assertEqual(dec(batches[0]["quantities"]["short_qty"]), dec(".005"))

    def test_campaign_aggregates_old_matched_and_new_asymmetric_quantities(self):
        self.seed()
        self.f.store.put("campaign:test", {"id": "legacy", "batches": [
            {"symbol": "XAUUSD1", "leverage": 4, "quantities": {"qty": "1", "notional": "8824.03"}}
        ]})
        with patch.object(self.f.broker, "submit", side_effect=self.only_side("SHORT")):
            self.open()
        self.f.store.finish_campaign({**self.f.account, "mode": "live"}, "done", dec(".45"))
        message = self.f.store.due_notifications()[0]["message"]
        self.assertIn("多头增加 1，空头增加 1.005", message)
        self.assertIn("新增总名义金额 8,846.09 USD1", message)
        self.assertNotIn("每边增加", message)

    def test_adverse_net_position_move_reduces_equity_and_available_cash(self):
        for long_qty, short_qty, old_mark, new_mark in (("1", ".999", "110", "100"), (".999", "1", "90", "100")):
            for available in ("1000", "8"):
                with self.subTest(long=long_qty, available=available):
                    snapshot = self.f.broker.snapshot(["XAUUSD1"])
                    snapshot.equity, snapshot.available = dec(120), dec(available)
                    long, short = snapshot.pair("XAUUSD1")
                    long.qty, short.qty = dec(long_qty), dec(short_qty)
                    long.mark = short.mark = dec(old_mark)
                    book = replace(self.f.market.book("XAUUSD1"), bid=dec(new_mark), ask=dec(new_mark), mark=dec(new_mark))
                    plan = plan_pair(snapshot, book, self.f.market.rules["XAUUSD1"], {4: dec(500000)}, self.f.account["policy"])
                    self.assertGreater(plan.qty, 0)
                    pnl = long.qty * (book.mark - long.mark) - short.qty * (book.mark - short.mark)
                    loss = max(dec(0), -pnl)
                    adjustment = max(dec(0), (long.qty + short.qty) * book.mark / 4 - long.occupied_margin - short.occupied_margin)

                    def ratio(qty):
                        return (snapshot.occupied_margin + adjustment + 2 * qty * book.mark / 4) / (snapshot.equity - loss - qty * 2 * book.mark * dec(".0004"))

                    self.assertEqual(plan.projected_ratio, ratio(plan.qty))
                    self.assertLessEqual(ratio(plan.qty), dec(".5"))
                    self.assertLessEqual(plan.qty * (2 * book.mark / 4 + 2 * book.mark * dec(".0004")) + loss + adjustment, snapshot.available)
                    next_qty = plan.qty + self.f.market.rules["XAUUSD1"].step
                    self.assertTrue(ratio(next_qty) > dec(".5") or next_qty * dec("50.08") + loss + adjustment > snapshot.available)
