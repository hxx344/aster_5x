import copy
from dataclasses import replace
import sqlite3
import unittest
from unittest.mock import patch

from trading.exchange import ExchangeError
from trading.models import TradingError
from trading.paper import PaperBroker
from .helpers import Fixture


class PaperPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)

    @staticmethod
    def order(cid, side="LONG"):
        return {"symbol": "XAUUSD1", "positionSide": side, "side": "BUY" if side == "LONG" else "SELL",
                "type": "MARKET", "quantity": "0.01", "newClientOrderId": cid}

    def test_failed_order_commit_does_not_publish_fill_or_change_balances(self):
        before = copy.deepcopy(self.f.broker.state)
        order = self.order("failed-save")
        with patch.object(self.f.store, "put", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.OperationalError):
                self.f.broker.submit([order])
        self.assertEqual(self.f.broker.state, before)
        with self.assertRaises(ExchangeError):
            self.f.broker.query("XAUUSD1", order["newClientOrderId"])
        self.assertEqual(PaperBroker("test", self.f.market, self.f.store).state, before)
        receipt, = self.f.broker.submit([order])
        self.assertEqual(receipt["status"], "FILLED")
        self.assertEqual(self.f.broker.state, PaperBroker("test", self.f.market, self.f.store).state)

    def test_second_leg_commit_failure_preserves_only_the_committed_first_leg(self):
        saved = self.f.store.put
        committed = []

        def fail_second(key, value):
            if committed:
                raise sqlite3.OperationalError("disk full")
            saved(key, value)
            committed.append(copy.deepcopy(value))

        with patch.object(self.f.store, "put", side_effect=fail_second):
            with self.assertRaises(sqlite3.OperationalError):
                self.f.broker.submit([self.order("long"), self.order("short", "SHORT")])
        self.assertEqual(self.f.broker.state, committed[0])
        self.assertEqual(self.f.broker.state, PaperBroker("test", self.f.market, self.f.store).state)
        self.assertEqual(set(self.f.broker.state["orders"]), {"long"})

    def test_failed_leverage_commit_keeps_memory_and_durable_leverage_unchanged(self):
        before = copy.deepcopy(self.f.broker.state)
        with patch.object(self.f.store, "put", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.OperationalError):
                self.f.broker.set_leverage("XAUUSD1", 5)
        self.assertEqual(self.f.broker.state, before)
        self.assertEqual(PaperBroker("test", self.f.market, self.f.store).state, before)

    def test_slow_quote_reads_do_not_renew_the_account_snapshot_timestamp(self):
        now = 1000.0
        book = self.f.market.book("XAUUSD1")

        def slow_book(symbol):
            nonlocal now
            now += 3
            return replace(book, timestamp=now)

        with patch("trading.paper.time.time", side_effect=lambda: now), \
             patch.object(self.f.market, "book", side_effect=slow_book):
            snapshot = self.f.broker.snapshot(["XAUUSD1"])
            with self.assertRaisesRegex(TradingError, "账户快照"):
                snapshot.require_fresh()
        self.assertEqual(snapshot.timestamp, 1000)
