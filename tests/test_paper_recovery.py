from contextlib import contextmanager
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from trading.exchange import AmbiguousOrder, ExchangeError, LiveBroker, RequestNotSent
from trading.execution import Executor
from trading.models import Plan, TradingError, dec
from trading.paper import PaperBroker
from trading.store import Store
from .helpers import Fixture


class PaperRecoveryTests(unittest.TestCase):
    symbol = "XAUUSD1"

    def fixture(self):
        fixture = Fixture()
        self.addCleanup(fixture.close)
        return fixture

    def open(self, fixture, executor):
        return executor.open_pair(fixture.account, executor.broker.snapshot([self.symbol]), self.symbol,
                                  Plan(dec("0.12")), fixture.market.book(self.symbol))

    @contextmanager
    def failed_commit(self, store, number, *, after_commit=False):
        saved = store.put
        count = 0

        def put(key, value):
            nonlocal count
            if key == "paper:test":
                count += 1
                if count == number:
                    if after_commit:
                        saved(key, value)
                    raise sqlite3.OperationalError("simulated interrupted paper commit")
            return saved(key, value)

        with patch.object(store, "put", side_effect=put):
            yield

    def recovered(self, fixture, executor, restart):
        if not restart:
            return executor
        store = Store(fixture.store.path)
        return Executor(store, PaperBroker("test", fixture.market, store), fixture.market)

    def test_failed_original_leg_recovers_without_resending_entries(self):
        for failed_leg in (1, 2):
            for restart in (False, True):
                with self.subTest(failed_leg=failed_leg, restart=restart):
                    fixture = self.fixture()
                    for side in ("LONG", "SHORT"):
                        fixture.broker.state["positions"][self.symbol + ":" + side] = {"qty": "1", "entry": "4412"}
                    fixture.broker.save()
                    executor = Executor(fixture.store, fixture.broker, fixture.market)
                    with self.failed_commit(fixture.store, failed_leg), self.assertRaises(sqlite3.OperationalError):
                        self.open(fixture, executor)
                    original_ids = {order["newClientOrderId"] for order in fixture.store.intent("test")["orders"]}
                    executor = self.recovered(fixture, executor, restart)
                    with patch.object(executor.broker, "submit", wraps=executor.broker.submit) as submit:
                        executor.reconcile(fixture.account)
                    self.assertIsNone(executor.store.intent("test"))
                    self.assertEqual(tuple(position.qty for position in executor.last_snapshot.pair(self.symbol)), (1, 1))
                    completed = executor.last_completed_intent
                    self.assertEqual(completed["status"], "aborted")
                    self.assertEqual([completed["receipts"][order["newClientOrderId"]]["executedQty"]
                                      for order in completed["orders"]], ["0.12" if failed_leg == 2 else "0", "0"])
                    self.assertEqual(submit.call_count, failed_leg - 1)
                    if failed_leg == 2:
                        repair, = submit.call_args.args[0]
                        self.assertEqual((repair["positionSide"], repair["side"], repair["quantity"]), ("LONG", "SELL", "0.12"))
                        self.assertNotIn(repair["newClientOrderId"], original_ids)
                    self.assertTrue(executor.store.get("post_fill_check:test"))

    def test_commit_ack_failure_uses_durable_fills_before_compensating(self):
        for failed_leg in (1, 2):
            for restart in (False, True):
                with self.subTest(failed_leg=failed_leg, restart=restart):
                    fixture = self.fixture()
                    executor = Executor(fixture.store, fixture.broker, fixture.market)
                    with self.failed_commit(fixture.store, failed_leg, after_commit=True), self.assertRaises(sqlite3.OperationalError):
                        self.open(fixture, executor)
                    executor = self.recovered(fixture, executor, restart)
                    with patch.object(executor.broker, "submit", wraps=executor.broker.submit) as submit:
                        executor.reconcile(fixture.account)
                    self.assertIsNone(executor.store.intent("test"))
                    expected = (dec("0.12"), dec("0.12")) if failed_leg == 2 else (0, 0)
                    self.assertEqual(tuple(position.qty for position in executor.last_snapshot.pair(self.symbol)), expected)
                    self.assertEqual(submit.call_count, 2 - failed_leg)
                    self.assertEqual(executor.broker.state, executor.store.get("paper:test"))

    def test_missing_repair_refund_is_durable_and_does_not_repeat(self):
        fixture = self.fixture()
        executor = Executor(fixture.store, fixture.broker, fixture.market)
        with self.failed_commit(fixture.store, 2), self.assertRaises(sqlite3.OperationalError):
            self.open(fixture, executor)
        with self.failed_commit(fixture.store, 1), self.assertRaises(sqlite3.OperationalError):
            executor.reconcile(fixture.account)
        failed = fixture.store.intent("test")
        self.assertEqual(failed["repair_attempts"], 1)
        failed_repair = failed["repairs"][0]["newClientOrderId"]
        executor = self.recovered(fixture, executor, True)
        with patch.object(executor.broker, "snapshot", side_effect=TradingError("snapshot temporarily unavailable")), \
             self.assertRaisesRegex(TradingError, "temporarily unavailable"):
            executor.reconcile(fixture.account)
        refunded = executor.store.intent("test")
        self.assertEqual(refunded["repair_attempts"], 0)
        self.assertEqual(refunded["receipts"][failed_repair]["executedQty"], "0")
        executor = self.recovered(fixture, executor, True)
        with patch.object(executor.broker, "submit", wraps=executor.broker.submit) as submit:
            executor.reconcile(fixture.account)
        self.assertIsNone(executor.store.intent("test"))
        submit.assert_called_once()
        repair, = submit.call_args.args[0]
        self.assertNotEqual(repair["newClientOrderId"], failed_repair)
        self.assertEqual(executor.last_completed_intent["repair_attempts"], 1)
        self.assertEqual(tuple(position.qty for position in executor.last_snapshot.pair(self.symbol)), (0, 0))

    def test_unreadable_or_missing_ledger_never_proves_order_absence(self):
        for unavailable in (sqlite3.OperationalError("ledger unreadable"), None):
            with self.subTest(unavailable=unavailable):
                fixture = self.fixture()
                executor = Executor(fixture.store, fixture.broker, fixture.market)
                with self.failed_commit(fixture.store, 1), self.assertRaises(sqlite3.OperationalError):
                    self.open(fixture, executor)
                saved = fixture.store.get

                def read(key, default=None):
                    if key != "paper:test":
                        return saved(key, default)
                    if isinstance(unavailable, Exception):
                        raise unavailable
                    return None

                with patch.object(fixture.store, "get", side_effect=read), \
                     patch.object(fixture.broker, "submit", side_effect=AssertionError("must not submit")):
                    if isinstance(unavailable, Exception):
                        with self.assertRaises(sqlite3.OperationalError):
                            executor.reconcile(fixture.account)
                    else:
                        executor.reconcile(fixture.account)
                pending = fixture.store.intent("test")
                self.assertIsNotNone(pending)
                self.assertEqual(pending["receipts"], {})
                self.assertEqual(pending["repairs"], [])

    def test_failed_leverage_recovers_and_can_be_planned_again(self):
        for restart in (False, True):
            with self.subTest(restart=restart):
                fixture = self.fixture()
                executor = Executor(fixture.store, fixture.broker, fixture.market)
                with self.failed_commit(fixture.store, 1), self.assertRaises(sqlite3.OperationalError):
                    executor.leverage(fixture.account, self.symbol, 5, 10)
                executor = self.recovered(fixture, executor, restart)
                with patch.object(executor.broker, "set_leverage", side_effect=AssertionError("reconcile must not change leverage")):
                    executor.reconcile(fixture.account)
                self.assertIsNone(executor.store.intent("test"))
                self.assertIsNone(executor.store.get("open_after_leverage:test:XAUUSD1"))
                self.assertEqual(executor.broker.state["leverages"][self.symbol], 5)
                executor.leverage(fixture.account, self.symbol, 5, 10)
                executor.reconcile(fixture.account)
                self.assertIsNone(executor.store.intent("test"))
                self.assertEqual(executor.store.get("open_after_leverage:test:XAUUSD1"), 10)

    def test_leverage_committed_before_error_is_confirmed_from_ledger(self):
        for restart in (False, True):
            with self.subTest(restart=restart):
                fixture = self.fixture()
                executor = Executor(fixture.store, fixture.broker, fixture.market)
                with self.failed_commit(fixture.store, 1, after_commit=True), self.assertRaises(sqlite3.OperationalError):
                    executor.leverage(fixture.account, self.symbol, 5, 10)
                executor = self.recovered(fixture, executor, restart)
                with patch.object(executor.broker, "set_leverage", side_effect=AssertionError("must not repeat committed change")):
                    executor.reconcile(fixture.account)
                self.assertIsNone(executor.store.intent("test"))
                self.assertEqual(executor.store.get("open_after_leverage:test:XAUUSD1"), 10)
                self.assertEqual(executor.broker.state["leverages"][self.symbol], 10)

    def test_live_order_not_found_or_query_not_sent_stays_unknown(self):
        for error in (ExchangeError("not yet visible", code=-2013), RequestNotSent("query budget unavailable")):
            with self.subTest(error=error):
                fixture = self.fixture()
                broker = LiveBroker({}, fixture.market, api=SimpleNamespace(budget=None))
                executor = Executor(fixture.store, broker, fixture.market)
                with patch.object(broker, "snapshot", side_effect=fixture.broker.snapshot), \
                     patch.object(broker, "submit", side_effect=AmbiguousOrder("response lost")) as submit, \
                     patch.object(broker, "query", side_effect=error):
                    self.open(fixture, executor)
                    executor.reconcile(fixture.account)
                submit.assert_called_once()
                pending = fixture.store.intent("test")
                self.assertEqual(pending["receipts"], {})
                self.assertEqual(pending["repairs"], [])
                self.assertIsNone(executor.last_completed_intent)

    def test_live_unconfirmed_leverage_remains_pending_without_resubmission(self):
        fixture = self.fixture()
        broker = LiveBroker({}, fixture.market, api=SimpleNamespace(budget=None))
        executor = Executor(fixture.store, broker, fixture.market)
        with patch.object(broker, "snapshot", side_effect=fixture.broker.snapshot), \
             patch.object(broker, "set_leverage", side_effect=AmbiguousOrder("change response lost")) as change:
            executor.leverage(fixture.account, self.symbol, 5, 10)
            executor.reconcile(fixture.account)
            executor.reconcile(fixture.account)
        change.assert_called_once()
        self.assertEqual(fixture.store.intent("test")["target"], 10)
        self.assertIsNone(fixture.store.get("open_after_leverage:test:XAUUSD1"))
