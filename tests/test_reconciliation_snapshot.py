from contextlib import contextmanager
from dataclasses import replace
import time
import unittest
from unittest.mock import patch

from trading.exchange import AmbiguousOrder
from trading.execution import Executor
from trading.models import Plan, TradingError, dec
from .helpers import Fixture


class ReconciliationSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.executor = Executor(self.f.store, self.f.broker, self.f.market)
        self.symbol = "XAUUSD1"

    def open(self):
        return self.executor.open_pair(self.f.account, self.f.broker.snapshot([self.symbol]),
                                       self.symbol, Plan(dec("0.12")), self.f.market.book(self.symbol))

    def pending_pair(self):
        with patch.object(self.executor, "reconcile", return_value="simulated interruption"):
            self.open()
        return self.f.store.intent("test")

    def test_success_reuses_exact_post_fill_snapshot_only_after_durable_completion(self):
        account_reads = []
        snapshot = self.f.broker.snapshot
        complete = self.f.store.complete_pair

        def read(*args, **kwargs):
            result = snapshot(*args, **kwargs)
            account_reads.append(result)
            return result

        def acknowledge(*args):
            self.assertIsNone(self.executor.last_snapshot)
            return complete(*args)

        with patch.object(self.f.broker, "snapshot", side_effect=read), \
             patch.object(self.f.store, "complete_pair", side_effect=acknowledge):
            self.open()
        self.assertEqual(len(account_reads), 2)
        self.assertIs(self.executor.last_snapshot, account_reads[-1])
        self.assertEqual(tuple(p.qty for p in self.executor.last_snapshot.pair(self.symbol)), (dec("0.12"), dec("0.12")))
        self.assertIsNone(self.f.store.intent("test"))

    def test_repaired_batch_publishes_only_the_snapshot_after_compensation(self):
        snapshots = []
        snapshot, submit = self.f.broker.snapshot, self.f.broker.submit

        def read(*args, **kwargs):
            result = snapshot(*args, **kwargs)
            snapshots.append(result)
            return result

        def one_leg(orders):
            self.assertIsNone(self.executor.last_snapshot)
            if len(orders) == 2:
                return [submit(orders[:1])[0], {"code": -2019}]
            return submit(orders)

        with patch.object(self.f.broker, "snapshot", side_effect=read), \
             patch.object(self.f.broker, "submit", side_effect=one_leg):
            self.open()
        self.assertEqual(len(snapshots), 3)
        self.assertEqual(tuple(p.qty for p in snapshots[1].pair(self.symbol)), (dec("0.12"), 0))
        self.assertIs(self.executor.last_snapshot, snapshots[-1])
        self.assertEqual(tuple(p.qty for p in self.executor.last_snapshot.pair(self.symbol)), (0, 0))
        self.assertIsNone(self.f.store.intent("test"))

    def test_stale_or_mismatched_account_read_never_publishes_snapshot(self):
        intent = self.pending_pair()
        valid = self.f.broker.snapshot([self.symbol])
        self.executor.last_snapshot = valid
        with patch.object(self.f.broker, "snapshot", return_value=replace(valid, timestamp=time.time() - 9)):
            with self.assertRaisesRegex(TradingError, "快照已过期"):
                self.executor.reconcile(self.f.account, intent)
        self.assertIsNone(self.executor.last_snapshot)
        valid.pair(self.symbol)[0].qty += dec("0.01")
        with patch.object(self.f.broker, "snapshot", return_value=valid):
            self.executor.reconcile(self.f.account, intent)
        self.assertIsNone(self.executor.last_snapshot)
        self.assertEqual(self.f.store.intent("test")["status"], "attention")

    def test_unknown_receipt_or_failed_persistence_never_publishes_snapshot(self):
        prior = self.f.broker.snapshot([self.symbol])
        self.executor.last_snapshot = prior
        with patch.object(self.f.broker, "submit", side_effect=AmbiguousOrder("response lost")):
            self.open()
        self.assertIsNone(self.executor.last_snapshot)
        intent = self.f.store.intent("test")
        self.f.broker.submit(intent["orders"])
        with patch.object(self.f.store, "complete_pair", side_effect=RuntimeError("commit failed")):
            with self.assertRaisesRegex(RuntimeError, "commit failed"):
                self.executor.reconcile(self.f.account)
        self.assertIsNone(self.executor.last_snapshot)

    def test_no_pending_work_and_rejected_new_open_clear_previous_snapshot(self):
        self.open()
        self.assertIsNotNone(self.executor.last_snapshot)
        self.executor.reconcile(self.f.account)
        self.assertIsNone(self.executor.last_snapshot)
        self.executor.last_snapshot = self.f.broker.snapshot([self.symbol])
        self.f.broker.state["leverages"][self.symbol] = 3
        with self.assertRaisesRegex(TradingError, "低于 4x"):
            self.open()
        self.assertIsNone(self.executor.last_snapshot)

    def test_only_reconciliation_queries_and_repairs_use_reserved_budget(self):
        active = 0
        submitted = []
        submit, snapshot = self.f.broker.submit, self.f.broker.snapshot

        @contextmanager
        def reserved():
            nonlocal active
            active += 1
            try:
                yield
            finally:
                active -= 1

        def one_leg(orders):
            submitted.append((len(orders), active))
            if len(orders) == 2:
                self.assertEqual(active, 0)
                return [submit(orders[:1])[0], {"code": -2019}]
            self.assertGreater(active, 0)
            return submit(orders)

        def read(*args, **kwargs):
            if self.f.store.intent("test"):
                self.assertGreater(active, 0)
            return snapshot(*args, **kwargs)

        with patch.object(self.f.broker, "reconciliation_budget", reserved, create=True), \
             patch.object(self.f.broker, "submit", side_effect=one_leg), \
             patch.object(self.f.broker, "snapshot", side_effect=read):
            self.open()
        self.assertEqual(submitted, [(2, 0), (1, 1)])
        self.assertEqual(active, 0)

    def test_pending_query_and_leverage_confirmation_use_reserved_budget_but_upgrade_does_not(self):
        active = False
        query, snapshot, leverage = self.f.broker.query, self.f.broker.snapshot, self.f.broker.set_leverage

        @contextmanager
        def reserved():
            nonlocal active
            active = True
            try:
                yield
            finally:
                active = False

        def change(*args):
            self.assertFalse(active)
            return leverage(*args)

        def confirmed(*args, **kwargs):
            self.assertTrue(active)
            return snapshot(*args, **kwargs)

        def check_order(*args):
            self.assertTrue(active)
            return query(*args)

        intent = self.pending_pair()
        intent["receipts"] = {}
        self.f.store.save_intent(intent)
        with patch.object(self.f.broker, "reconciliation_budget", reserved, create=True):
            with patch.object(self.f.broker, "snapshot", side_effect=confirmed), \
                 patch.object(self.f.broker, "query", side_effect=check_order):
                self.executor.reconcile(self.f.account)
            with patch.object(self.f.broker, "set_leverage", side_effect=change):
                self.executor.leverage(self.f.account, self.symbol, 4, 5)
            with patch.object(self.f.broker, "snapshot", side_effect=confirmed):
                self.executor.reconcile(self.f.account)
        self.assertIsNone(self.executor.last_snapshot)
        self.assertFalse(active)


if __name__ == "__main__":
    unittest.main()
