"""Bounded, account-scoped history contexts for exact cycle cost calculation."""
from contextlib import contextmanager
from decimal import Decimal
import sqlite3
from unittest import TestCase
from unittest.mock import patch

from tests import test_cycle_volume as ledger_cases
from trading.cycle_volume import FILL_FIELDS
from trading.models import TradingError
from trading.store import Store


DAY = ledger_cases.DAY


class CycleCostStoreTests(TestCase):
    setUp = ledger_cases.CycleVolumeTests.setUp
    intent = ledger_cases.CycleVolumeTests.intent
    fill = ledger_cases.CycleVolumeTests.fill

    def seed_history(self, entries):
        """Seed query-scale rows; ledger admission is covered independently."""
        intent = self.intent("query-seed", quantity="50000", filled="50000")
        self.store.record_cycle_fills(intent, [self.fill(intent, "query-seed", executed_at=DAY - 10 * 86400)])
        with self.store.connect() as db:
            seed = dict(db.execute("SELECT * FROM cycle_fills WHERE account_id=? AND trade_id=?",
                                   ("first", "query-seed")).fetchone())
            fields = tuple(seed)
            rows = ({**seed, "intent_id": intent_id, "trade_id": trade_id, "executed_at": executed_at,
                     "client_id": intent_id + "-long", "order_id": "order-" + intent_id,
                     "utc_date": ledger_cases.utc_day(executed_at)[0]}
                    for intent_id, trade_id, executed_at in entries)
            db.executemany("INSERT INTO cycle_fills(" + ",".join(fields) + ") VALUES (" + ",".join("?" for _ in fields) + ")",
                           (tuple(row[field] for field in fields) for row in rows))

    def test_empty_result_and_default_clock_are_stable(self):
        self.assertEqual(self.store.cycle_cost_records("first", now=DAY), [])
        with patch("trading.store.time.time", return_value=DAY) as clock:
            self.assertEqual(self.store.cycle_cost_records("first"), [])
        clock.assert_called_once_with()

    def test_window_and_latest_selection_have_distinct_exact_boundaries(self):
        stamps = (DAY - 86400 - .001, DAY - 86400, DAY - 86400 + .001,
                  DAY, DAY + .001, DAY + .002)
        for index, executed_at in enumerate(stamps):
            intent = self.intent("boundary-" + str(index))
            self.store.record_cycle_fills(intent, [self.fill(intent, str(index), executed_at=executed_at)])
        # Only the latest future record is selected for display. The other
        # future intent must not enter through the rolling-window selector.
        rows = self.store.cycle_cost_records("first", now=DAY, limit=1)
        self.assertEqual([row["trade_id"] for row in rows], ["2", "3", "5"])
        self.assertEqual(set(rows[0]), FILL_FIELDS | {"account_id", "intent_id", "phase", "utc_date"})

    def test_selected_intent_includes_old_counterpart_and_repair_history(self):
        intent = self.intent("cross-day", quantity="1", filled="1", short_filled="1")
        old = self.fill(intent, "long-old", executed_at=DAY - 86400 - 10)
        recent = self.fill(intent, "short-now", position_side="SHORT", executed_at=DAY)
        repair = {"symbol": "XAUUSD1", "positionSide": "LONG", "side": "SELL",
                  "quantity": "1", "newClientOrderId": "repair-long"}
        intent["repairs"].append(repair)
        intent["receipts"]["repair-long"] = {**repair, "clientOrderId": "repair-long", "orderId": "repair-order",
                                                "status": "FILLED", "executedQty": "1", "avgPrice": "100"}
        self.store.save_intent(intent)
        repair_fill = self.fill(intent, "long-repair", repair=True, executed_at=DAY + 5)
        self.store.record_cycle_fills(intent, [old, recent, repair_fill])
        rows = self.store.cycle_cost_records("first", now=DAY + 10, limit=1)
        self.assertEqual([row["trade_id"] for row in rows], ["long-old", "short-now", "long-repair"])
        self.assertEqual([row["phase"] for row in rows], ["open", "open", "repair"])
        self.assertEqual([row["side"] for row in rows], ["BUY", "SELL", "SELL"])
        self.assertEqual([row["utc_date"] for row in rows], ["2026-09-12", "2026-09-14", "2026-09-14"])

    def test_account_isolation_is_rechecked_for_complete_intent_lookup(self):
        first = self.intent("first-intent")
        second = self.intent("second-intent", account_id="second")
        self.store.record_cycle_fills(first, [self.fill(first, "same-id")])
        self.store.record_cycle_fills(second, [self.fill(second, "same-id", quantity="2")])
        # Even a foreign row with the selected intent_id cannot leak through
        # the context expansion; each ledger read independently scopes account.
        with self.store.connect() as db:
            row = dict(db.execute("SELECT * FROM cycle_fills WHERE account_id=?", ("second",)).fetchone())
            row.update(intent_id=first["id"], trade_id="foreign-context")
            db.execute("INSERT INTO cycle_fills(" + ",".join(row) + ") VALUES (" + ",".join("?" for _ in row) + ")",
                       tuple(row.values()))
        rows = self.store.cycle_cost_records("first", now=DAY + 20)
        self.assertEqual([(row["account_id"], row["trade_id"], row["notional"]) for row in rows],
                         [("first", "same-id", "100")])
        self.assertEqual(self.store.cycle_cost_records("unused", now=DAY + 20), [])

    def test_latest_hundred_selects_complete_old_contexts_without_unrelated_history(self):
        old = DAY - 2 * 86400
        entries = [("old-" + str(index), "trade-" + str(index), old + index) for index in range(101)]
        entries.append(("old-100", "counterpart", old - 5 * 86400))
        self.seed_history(entries)
        rows = self.store.cycle_cost_records("first", now=DAY)
        self.assertEqual(len(rows), 101)
        self.assertEqual(rows[0]["trade_id"], "counterpart")
        self.assertEqual({row["intent_id"] for row in rows}, {"old-" + str(index) for index in range(1, 101)})
        self.assertEqual([row["trade_id"] for row in self.store.cycle_cost_records("first", now=DAY, limit=1)],
                         ["counterpart", "trade-100"])

    def test_all_window_intents_are_read_with_account_time_and_intent_index_seeks(self):
        entries = [("irrelevant-" + str(index), "old-" + str(index), DAY - 3 * 86400 - index / 10)
                   for index in range(20000)]
        entries.extend(("recent-" + str(index), "current-" + str(index), DAY - 600 + index / 10)
                       for index in range(1201))
        entries.append(("recent-0", "old-counterpart", DAY - 2 * 86400))
        self.seed_history(entries)
        statements = []
        original_connect = self.store.connect
        @contextmanager
        def traced_connect():
            with original_connect() as db:
                db.set_trace_callback(statements.append)
                yield db
        with patch.object(self.store, "connect", traced_connect):
            rows = self.store.cycle_cost_records("first", now=DAY, limit=1)
        self.assertEqual(len(rows), 1202)
        self.assertEqual(rows[0]["trade_id"], "old-counterpart")
        self.assertFalse(any(row["intent_id"].startswith("irrelevant-") for row in rows))
        selects = [query for query in statements if "FROM cycle_fills" in query]
        self.assertEqual(len(selects), 1)
        with original_connect() as db:
            plan = " ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + selects[0]))
        self.assertIn("SEARCH cycle_fills USING INDEX idx_cycle_fills_order (intent_id=?)", plan)
        self.assertIn("SEARCH cycle_fills USING INDEX idx_cycle_fills_recent (account_id=? AND executed_at>? AND executed_at<?)", plan)
        self.assertIn("SEARCH cycle_fills USING INDEX idx_cycle_fills_recent (account_id=?)", plan)
        self.assertNotIn("SCAN cycle_fills", plan)

    def test_late_fill_restart_and_duplicate_replay_refresh_complete_context(self):
        intent = self.intent("late", short_filled="10")
        recent = self.fill(intent, "new", executed_at=DAY)
        old = self.fill(intent, "late-arrival", position_side="SHORT", executed_at=DAY - 86410)
        self.store.record_cycle_fills(intent, [recent])
        self.assertEqual([row["trade_id"] for row in self.store.cycle_cost_records("first", now=DAY)], ["new"])
        self.store.record_cycle_fills(intent, [old])
        reopened = Store(self.path)
        self.assertEqual(reopened.record_cycle_fills(intent, [old, recent]), 0)
        self.assertEqual([row["trade_id"] for row in reopened.cycle_cost_records("first", now=DAY)],
                         ["late-arrival", "new"])

    def test_equal_timestamps_use_symbol_then_trade_id_order(self):
        first = self.intent("xau")
        alternate = self.intent("oil")
        alternate["symbol"] = "CLUSD1"
        for order in alternate["orders"]:
            order["symbol"] = "CLUSD1"
        for receipt in alternate["receipts"].values():
            receipt["symbol"] = "CLUSD1"
        self.store.save_intent(alternate)
        self.store.record_cycle_fills(first, [self.fill(first, "b"), self.fill(first, "a")])
        self.store.record_cycle_fills(alternate, [self.fill(alternate, "b"), self.fill(alternate, "a")])
        rows = self.store.cycle_cost_records("first", now=DAY + 20)
        self.assertEqual([(row["symbol"], row["trade_id"]) for row in rows],
                         [("CLUSD1", "a"), ("CLUSD1", "b"), ("XAUUSD1", "a"), ("XAUUSD1", "b")])

    def test_query_is_read_only_and_does_not_change_schema_or_runtime_mode(self):
        intent = self.intent()
        self.store.record_cycle_fills(intent, [self.fill(intent)])
        self.store.bind_runtime_mode(demo=False)
        with self.store.connect() as db:
            before = tuple(db.iterdump())
        original_connect = self.store.connect
        def authorize(action, *args):
            return sqlite3.SQLITE_OK if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ) else sqlite3.SQLITE_DENY
        @contextmanager
        def read_only_connect():
            with original_connect() as db:
                db.set_authorizer(authorize)
                yield db
        with patch.object(self.store, "connect", read_only_connect):
            self.assertEqual(len(self.store.cycle_cost_records("first", now=DAY + 20)), 1)
        with original_connect() as db:
            self.assertEqual(tuple(db.iterdump()), before)
        self.assertEqual(self.store.get("runtime_mode"), "authenticated")
        with self.assertRaises(TradingError):
            Store(self.path, demo=True)

    def test_invalid_inputs_fail_before_reading_and_epoch_is_supported(self):
        with patch.object(self.store, "connect", side_effect=AssertionError("invalid input must not query")):
            for account_id in (None, "", "../first", "FIRST", True):
                with self.subTest(account_id=account_id), self.assertRaises(TradingError):
                    self.store.cycle_cost_records(account_id, now=DAY)
            for now in (True, "123", Decimal("123"), float("nan"), float("inf"), -1, 1e30, 10 ** 1000):
                with self.subTest(now=now), self.assertRaises(TradingError):
                    self.store.cycle_cost_records("first", now=now)
            for limit in (0, -1, 1001, True, "100", 1.5, None):
                with self.subTest(limit=limit), self.assertRaises(TradingError):
                    self.store.cycle_cost_records("first", now=DAY, limit=limit)
        intent = self.intent()
        self.store.record_cycle_fills(intent, [self.fill(intent, executed_at=0)])
        self.assertEqual(self.store.cycle_cost_records("first", now=0)[0]["executed_at"], 0)
