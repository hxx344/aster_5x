import copy
from dataclasses import replace
import unittest
from unittest.mock import patch

from trading.models import dec
from trading.paper import PaperBroker
from trading.store import Store
from .helpers import Fixture


class PaperMarketOrderTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.symbol = "XAUUSD1"
        self.book = self.f.market.book(self.symbol)

    def order(self, cid, position_side, side, qty="0.01", **fields):
        return {"symbol": self.symbol, "positionSide": position_side, "side": side, "type": "MARKET",
                "quantity": qty, "newClientOrderId": cid, **fields}

    def position(self, side):
        return self.f.broker.state["positions"][self.symbol + ":" + side]

    def test_market_pair_uses_each_submission_quote_after_prices_move(self):
        orders = [self.order("drift-long", "LONG", "BUY"), self.order("drift-short", "SHORT", "SELL")]
        self.assertTrue(all("price" not in order and "timeInForce" not in order for order in orders))
        long_book = replace(self.book, ask=dec("4420"), bid=dec("4419"))
        short_book = replace(self.book, ask=dec("4409"), bid=dec("4408"))
        with patch.object(self.f.market, "book", side_effect=[long_book, short_book]) as read:
            receipts = self.f.broker.submit(orders)
        self.assertEqual(read.call_count, 2)
        self.assertEqual([row["status"] for row in receipts], ["FILLED", "FILLED"])
        self.assertEqual([dec(row["avgPrice"]) for row in receipts], [long_book.ask, short_book.bid])
        self.assertNotEqual(dec(receipts[0]["avgPrice"]), self.book.ask)
        self.assertNotEqual(dec(receipts[1]["avgPrice"]), self.book.bid)
        self.assertEqual(dec(self.position("LONG")["entry"]), long_book.ask)
        self.assertEqual(dec(self.position("SHORT")["entry"]), short_book.bid)
        expected_fees = dec("0.01") * (long_book.ask + short_book.bid) * dec("0.0004")
        self.assertEqual(dec(self.f.broker.state["wallet"]), dec(25000) - expected_fees)

    def test_thin_market_depth_fills_only_visible_quantity_and_terminates_remainder(self):
        shallow = replace(self.book, ask_qty=dec("0.004"), bid_qty=dec("0.008"))
        orders = [self.order("thin-long", "LONG", "BUY"), self.order("thin-short", "SHORT", "SELL")]
        with patch.object(self.f.market, "book", return_value=shallow):
            receipts = self.f.broker.submit(orders)
        self.assertEqual([row["status"] for row in receipts], ["EXPIRED", "EXPIRED"])
        self.assertEqual([dec(row["executedQty"]) for row in receipts], [dec("0.004"), dec("0.008")])
        self.assertEqual([dec(row["origQty"]) for row in receipts], [dec("0.01"), dec("0.01")])
        self.assertEqual([dec(row["avgPrice"]) for row in receipts], [shallow.ask, shallow.bid])
        fees = (dec("0.004") * shallow.ask + dec("0.008") * shallow.bid) * dec("0.0004")
        self.assertEqual(dec(self.f.broker.state["wallet"]), dec(25000) - fees)
        with patch.object(self.f.market, "book", side_effect=AssertionError("completed remainder cannot fill later")):
            self.assertEqual(self.f.broker.query(self.symbol, "thin-long"), receipts[0])
            self.assertEqual(self.f.broker.cancel(self.symbol, "thin-short"), receipts[1])

    def test_zero_depth_prevents_that_leg_and_fractional_depth_respects_quantity_step(self):
        shallow = replace(self.book, ask_qty=dec("0.0039"), bid_qty=dec(0))
        orders = [self.order("lot-long", "LONG", "BUY"), self.order("empty-short", "SHORT", "SELL")]
        with patch.object(self.f.market, "book", return_value=shallow):
            long, short = self.f.broker.submit(orders)
        self.assertEqual((long["status"], long["executedQty"]), ("EXPIRED", "0.003"))
        self.assertEqual((short["status"], short["executedQty"], short["avgPrice"]), ("EXPIRED", "0", "0"))
        self.assertEqual(dec(self.position("SHORT")["qty"]), 0)

    def test_market_compensation_of_either_side_uses_current_quote_and_actual_fill_for_pnl_and_fees(self):
        opening = replace(self.book, bid=dec(99), ask=dec(101))
        with patch.object(self.f.market, "book", return_value=opening):
            self.f.broker.submit([self.order("open-long", "LONG", "BUY", "1"), self.order("open-short", "SHORT", "SELL", "1")])
        closing = replace(self.book, bid=dec(110), ask=dec(112), bid_qty=dec("0.4"), ask_qty=dec("0.4"))
        with patch.object(self.f.market, "book", return_value=closing):
            receipts = self.f.broker.submit([self.order("close-long", "LONG", "SELL", "0.5"),
                                             self.order("close-short", "SHORT", "BUY", "0.5")])
        self.assertEqual([row["status"] for row in receipts], ["EXPIRED", "EXPIRED"])
        self.assertEqual([row["executedQty"] for row in receipts], ["0.4", "0.4"])
        self.assertEqual([row["avgPrice"] for row in receipts], ["110", "112"])
        self.assertEqual([dec(self.position(side)["qty"]) for side in ("LONG", "SHORT")], [dec("0.6"), dec("0.6")])
        self.assertEqual([dec(self.position(side)["entry"]) for side in ("LONG", "SHORT")], [101, 99])
        realized = dec("0.4") * ((closing.bid - opening.ask) + (opening.bid - closing.ask))
        fees = (opening.bid + opening.ask + dec("0.4") * (closing.bid + closing.ask)) * dec("0.0004")
        self.assertEqual(dec(self.f.broker.state["wallet"]), dec(25000) + realized - fees)

    def test_oversized_close_never_changes_existing_holdings_or_wallet(self):
        self.f.broker.submit([self.order("seed-long", "LONG", "BUY"), self.order("seed-short", "SHORT", "SELL")])
        before = copy.deepcopy(self.f.broker.state)
        receipts = self.f.broker.submit([self.order("too-large-long", "LONG", "SELL", "0.02"),
                                         self.order("too-large-short", "SHORT", "BUY", "0.02")])
        self.assertEqual([row["executedQty"] for row in receipts], ["0", "0"])
        self.assertEqual([row["status"] for row in receipts], ["EXPIRED", "EXPIRED"])
        self.assertEqual(self.f.broker.state["positions"], before["positions"])
        self.assertEqual(self.f.broker.state["wallet"], before["wallet"])

    def test_duplicate_partial_market_order_does_not_fill_again_after_restart_or_price_change(self):
        order = self.order("persistent-partial", "LONG", "BUY")
        with patch.object(self.f.market, "book", return_value=replace(self.book, ask_qty=dec("0.003"))):
            receipt, = self.f.broker.submit([order])
        before = copy.deepcopy(self.f.broker.state)
        restarted = PaperBroker("test", self.f.market, Store(self.f.store.path))
        with patch.object(self.f.market, "book", side_effect=AssertionError("duplicate ID must not reach market")):
            self.assertEqual(restarted.submit([order, order]), [receipt, receipt])
        self.assertEqual(restarted.state, before)

    def test_legacy_limit_fok_orders_keep_price_limit_and_all_or_none_behavior(self):
        limit = self.order("old-limit", "LONG", "BUY", type="LIMIT", timeInForce="FOK", price=str(self.book.ask))
        moved = replace(self.book, ask=self.book.ask + 1)
        with patch.object(self.f.market, "book", return_value=moved):
            rejected, = self.f.broker.submit([limit])
        self.assertEqual((rejected["status"], rejected["executedQty"]), ("EXPIRED", "0"))
        limit["newClientOrderId"] = "old-thin-limit"
        with patch.object(self.f.market, "book", return_value=replace(self.book, ask_qty=dec("0.005"))):
            thin, = self.f.broker.submit([limit])
        self.assertEqual((thin["status"], thin["executedQty"]), ("EXPIRED", "0"))
        limit["newClientOrderId"] = "old-filled-limit"
        filled, = self.f.broker.submit([limit])
        self.assertEqual((filled["status"], filled["executedQty"]), ("FILLED", "0.01"))
        self.assertEqual(self.f.broker.query(self.symbol, "old-limit"), rejected)


if __name__ == "__main__":
    unittest.main()
