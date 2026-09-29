"""A paused archived batch can explicitly adopt manual holdings without trading."""
from copy import deepcopy
import sqlite3
from unittest import TestCase
from unittest.mock import patch

from tests import test_pair_order_recovery as recovery_tests
from trading.exchange import ExchangeError
from trading.models import TradingError
from trading.pair_recovery import require_archived_orders_clear


class ArchivedBaselineTests(TestCase):
    setUp = recovery_tests.PairOrderRecoveryTests.setUp
    seed = recovery_tests.PairOrderRecoveryTests.seed
    state = recovery_tests.PairOrderRecoveryTests.state
    broker = recovery_tests.PairOrderRecoveryTests.broker
    no_writes = recovery_tests.PairOrderRecoveryTests.no_writes
    audit = recovery_tests.PairOrderRecoveryTests.audit

    def archive(self):
        self.seed()
        manager = self.engine.pairs
        manager.confirm_recovery("gold", manager.preview_recovery("gold")["token"], True)
        self.set_quantity("4")
        return deepcopy(self.state())

    def set_quantity(self, quantity):
        for key, side in (("long", "LONG"), ("short", "SHORT")):
            broker = self.broker(key)
            broker.state["positions"]["XAUUSD1:" + side]["qty"] = quantity
            broker.save()

    def preview(self):
        return self.engine.pairs.preview_baseline("gold")

    def confirm(self, token, ack=True):
        return self.engine.pairs.confirm_baseline("gold", token, ack)

    def test_changed_holdings_have_explicit_exit_preserving_unknown_watch_and_history(self):
        before = self.archive()
        audit = deepcopy(self.audit())
        with self.no_writes():
            with self.assertRaisesRegex(TradingError, "核对并采纳"):
                self.engine.pairs.enable("gold", True)
            review = self.preview()
            self.assertEqual(self.state(), before)
            self.assertEqual(review["actual"], {"LONG": "4", "SHORT": "4"})
            self.assertTrue(all(row["status"] == "UNKNOWN" for row in review["orders"]))
            self.confirm(review["token"])
            state = self.state()
            self.assertEqual(state["owned"], review["actual"])
            self.assertEqual(state["progress"]["baseline"], review["actual"])
            self.assertEqual(state["progress"]["quantities"], {"LONG": "0", "SHORT": "0"})
            self.assertEqual(state["progress"]["completed_cycles"], 4)
            for key in ("recovery_watch", "daily_volume", "last_batch"):
                self.assertEqual(state[key], before[key])
            self.assertFalse(self.f.store.pair("gold")["enabled"])
            self.assertEqual(self.audit(), audit)
            record = self.f.store.get("pair_order_recovery:gold:baseline-" + review["token"])
            self.assertEqual(record["status"], "manual_archived_baseline_adopted")
            with self.assertRaises(TradingError):
                self.confirm(review["token"])
            self.engine.pairs.enable("gold", True)
        self.assertTrue(self.f.store.pair("gold")["enabled"])

    def test_active_late_filled_and_failed_queries_cannot_adopt(self):
        original = self.archive()
        leg = original["recovery_watch"]["batches"][0]["legs"][0]
        for status, qty in (("NEW", "0"), ("PARTIALLY_FILLED", "0.1"), ("FILLED", "0.2"), ("CANCELED", "0.1")):
            with self.subTest(status=status), self.no_writes(), patch.object(self.broker(), "query", return_value=recovery_tests.receipt_for(leg, status=status, qty=qty)):
                with self.assertRaises(TradingError):
                    self.preview()
                self.assertEqual(self.state(), original)
        with self.no_writes(), patch.object(self.broker(), "query", side_effect=ExchangeError("offline")):
            with self.assertRaises(TradingError):
                self.preview()
        self.assertEqual(self.state(), original)

    def test_late_fill_after_confirmation_still_blocks_new_work(self):
        self.archive()
        self.confirm(self.preview()["token"])
        leg = self.state()["recovery_watch"]["batches"][0]["legs"][0]
        with self.no_writes(), patch.object(self.broker(), "query", return_value=recovery_tests.receipt_for(leg, status="FILLED", qty="0.2")):
            with self.assertRaises(TradingError):
                self.engine.pairs.enable("gold", True)
            with self.assertRaises(TradingError):
                require_archived_orders_clear(self.engine, self.f.store.pair("gold"))

    def test_confirmation_rechecks_actual_quantity_and_leverage(self):
        original = self.archive()
        for change in ("quantity", "leverage"):
            with self.subTest(change=change):
                self.set_quantity("4")
                review = self.preview()
                if change == "quantity":
                    self.set_quantity("5")
                else:
                    for key in ("long", "short"):
                        broker = self.broker(key)
                        broker.state["leverages"]["XAUUSD1"] = 10
                        broker.save()
                with self.no_writes(), self.assertRaises(TradingError):
                    self.confirm(review["token"])
                self.assertEqual(self.state(), original)

    def test_confirmation_requires_true_ack_and_live_token(self):
        original = self.archive()
        review = self.preview()
        for ack in (False, 1, "true", None):
            with self.assertRaises(TradingError):
                self.confirm(review["token"], ack)
        with self.assertRaises(TradingError):
            self.confirm("0" * 32)
        self.engine.pairs.recovery.baseline_previews["gold"]["expires"] = 0
        with self.assertRaises(TradingError):
            self.confirm(review["token"])
        self.assertEqual(self.state(), original)

    def test_new_pending_cycle_or_transfer_is_not_discarded(self):
        original = self.archive()
        for field in ("pending", "cycle", "margin"):
            with self.subTest(field=field):
                changed = deepcopy(original)
                if field == "pending":
                    changed["pending"] = {"id": "new"}
                elif field == "cycle":
                    changed["progress"]["quantities"]["LONG"] = "1"
                else:
                    self.f.store.put("pair_margin:gold", {"status": "unknown"})
                self.f.store.put("pair_runtime:gold", changed)
                with self.no_writes(), self.assertRaises(TradingError):
                    self.preview()
                self.assertEqual(self.state(), changed)

    def test_configuration_change_and_audit_failure_do_not_commit(self):
        original = self.archive()
        review = self.preview()
        self.engine.pairs.configure("gold", {"name": "changed"})
        with self.assertRaises(TradingError):
            self.confirm(review["token"])
        review = self.preview()
        with self.f.store.connect() as db:
            db.execute("CREATE TRIGGER reject_baseline BEFORE INSERT ON kv WHEN NEW.key LIKE 'pair_order_recovery:gold:baseline-%' BEGIN SELECT RAISE(ABORT, 'audit failed'); END")
        with self.no_writes(), self.assertRaises(sqlite3.DatabaseError):
            self.confirm(review["token"])
        self.assertEqual(self.state(), original)

    def test_transaction_conflict_keeps_the_new_runtime(self):
        self.archive()
        review = self.preview()
        commit = self.f.store.commit_pair_recovery
        changed = deepcopy(self.state())
        changed["daily_volume"]["2026-09-29"]["long"] = "999"
        def conflict(*args, **kwargs):
            self.f.store.put("pair_runtime:gold", changed)
            return commit(*args, **kwargs)
        with self.no_writes(), patch.object(self.f.store, "commit_pair_recovery", side_effect=conflict):
            with self.assertRaises(TradingError):
                self.confirm(review["token"])
        self.assertEqual(self.state(), changed)
