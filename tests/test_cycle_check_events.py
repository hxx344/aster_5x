"""Persistent account-local aggregation of consecutive cycle checks."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from unittest import TestCase
from unittest.mock import patch

from trading.models import TradingError
from trading.store import Store


SYMBOL = "XAUUSD1"
EVENT_FIELDS = {"id", "account_id", "kind", "message", "created_at"}


class CycleCheckEventTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "events.sqlite3"
        self.store = Store(self.path)

    def check(self, now, message="循环价差未满足", *, account="first", symbol=SYMBOL, phase="open", diagnostic=None, store=None):
        with patch("trading.store.time.time", return_value=now):
            (store or self.store).record_cycle_check(account, symbol, phase, message, diagnostic)

    def event(self, now, account, kind, message="普通事件"):
        with patch("trading.store.time.time", return_value=now):
            self.store.event(account, kind, message)

    def account_events(self, account="first"):
        return [event for event in self.store.events(limit=1000) if event["account_id"] == account]

    def test_changed_numbers_and_interleaved_reasons_update_one_check(self):
        first_diagnostic = {"symbol": SYMBOL, "phase": "open", "checks": [{"actual": "0.11", "passed": False}]}
        self.check(100, "采样价差 0.11 bp", diagnostic=first_diagnostic)
        first = deepcopy(self.account_events()[0])
        first_diagnostic["checks"][0]["actual"] = "mutated after write"
        self.assertEqual(self.account_events()[0]["cycle_check"]["diagnostic"]["checks"][0]["actual"], "0.11")
        self.check(105, "账户余额不足", diagnostic={"code": "cash", "checks": [{"actual": "50"}]})
        latest = {"symbol": SYMBOL, "phase": "open", "checks": [{"actual": "0.12", "passed": False}]}
        self.check(110, "采样价差 0.12 bp", diagnostic=latest)
        events = self.account_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["id"], first["id"])
        self.assertEqual((events[0]["kind"], events[0]["message"], events[0]["created_at"]),
                         ("cycle_check", "采样价差 0.12 bp", 110))
        self.assertEqual(events[0]["cycle_check"], {"symbol": SYMBOL, "phase": "open", "count": 3,
                                                 "first_at": 100, "last_at": 110, "diagnostic": latest})

    def test_latest_unstructured_check_clears_earlier_diagnostic(self):
        self.check(100, diagnostic={"checks": [{"actual": "old"}]})
        self.check(101, "等待新的价差核对")
        metadata = self.account_events()[0]["cycle_check"]
        self.assertEqual(metadata["count"], 2)
        self.assertNotIn("diagnostic", metadata)

    def test_every_same_account_noncheck_event_starts_a_new_segment(self):
        for kind in ("order", "fill", "cycle_fill", "cycle", "repair", "error", "wait", "control", "leverage"):
            account = "account_" + kind
            with self.subTest(kind=kind):
                self.check(100, "第一段", account=account)
                first = deepcopy(self.account_events(account)[0])
                self.event(101, account, kind, "必须独立保留")
                self.check(102, "第二段", account=account)
                events = self.account_events(account)
                self.assertEqual([row["kind"] for row in events], ["cycle_check", kind, "cycle_check"])
                self.assertEqual(events[2], first)
                self.assertEqual(events[0]["cycle_check"]["count"], 1)
                self.assertEqual(events[1]["message"], "必须独立保留")
                self.assertEqual(set(events[1]), EVENT_FIELDS)

    def test_other_account_events_neither_split_nor_join_a_check(self):
        self.check(100)
        first_id = self.account_events()[0]["id"]
        self.check(101, "另一个账户检查", account="second")
        self.event(102, "second", "order")
        self.check(103, "本账户最新结果")
        self.assertEqual(len(self.account_events()), 1)
        first = self.account_events()[0]
        self.assertEqual((first["id"], first["cycle_check"]["count"]), (first_id, 2))
        self.assertEqual(len(self.account_events("second")), 2)
        second_check = next(row for row in self.account_events("second") if row["kind"] == "cycle_check")
        self.assertEqual(second_check["cycle_check"]["count"], 1)

    def test_stage_and_symbol_changes_start_segments_even_when_returning(self):
        sequences = {
            "stage": [(SYMBOL, "open"), (SYMBOL, "close"), (SYMBOL, "open")],
            "market": [(SYMBOL, "open"), ("SPCXUSD1", "open"), (SYMBOL, "open")],
        }
        for account, sequence in sequences.items():
            with self.subTest(account=account):
                for offset, (symbol, phase) in enumerate(sequence):
                    self.check(100 + offset, account=account, symbol=symbol, phase=phase)
                rows = self.account_events(account)
                self.assertEqual(len(rows), 3)
                self.assertTrue(all(row["cycle_check"]["count"] == 1 for row in rows))

    def test_restart_continues_latest_valid_segment(self):
        self.check(100)
        first_id = self.account_events()[0]["id"]
        reopened = Store(self.path)
        self.check(101, "重启后最新检查", store=reopened)
        rows = reopened.events()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], first_id)
        self.assertEqual(rows[0]["cycle_check"], {"symbol": SYMBOL, "phase": "open", "count": 2,
                                               "first_at": 100, "last_at": 101})

    def test_legacy_database_migration_is_idempotent_and_preserves_old_rows(self):
        path = Path(self.directory.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,account_id TEXT NOT NULL,kind TEXT NOT NULL,message TEXT NOT NULL,created_at REAL NOT NULL)")
            db.executemany("INSERT INTO events(account_id,kind,message,created_at) VALUES (?,?,?,?)",
                           [("first", "wait", "旧循环检查", 90), ("first", "error", "旧错误", 95)])
            original = db.execute("SELECT * FROM events ORDER BY id").fetchall()
        upgraded = Store(path)
        Store(path)
        with upgraded.connect() as db:
            columns = list(db.execute("PRAGMA table_info(events)"))
            added = [row for row in columns if row["name"] == "cycle_check"]
            self.assertEqual(len(added), 1)
            self.assertEqual((added[0]["type"], added[0]["notnull"]), ("TEXT", 0))
            self.assertEqual([tuple(row) for row in db.execute("SELECT id,account_id,kind,message,created_at FROM events ORDER BY id")], original)
            self.assertEqual([row[0] for row in db.execute("SELECT cycle_check FROM events")], [None, None])
            # Older code's explicit-column insertion still works after upgrade.
            db.execute("INSERT INTO events(account_id,kind,message,created_at) VALUES (?,?,?,?)", ("second", "order", "旧代码订单", 96))
        self.check(100, store=upgraded)
        rows = upgraded.events()
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["cycle_check"]["count"], 1)
        self.assertTrue(all(set(row) == EVENT_FIELDS for row in rows[1:]))

    def test_multiple_store_instances_never_lose_concurrent_counts(self):
        workers, checks_per_worker = 4, 20
        stores = [Store(self.path) for _ in range(workers)]
        ready = threading.Barrier(workers)

        def record(index):
            ready.wait(timeout=10)
            for sequence in range(checks_per_worker):
                stores[index].record_cycle_check("first", SYMBOL, "open", f"检查 {index}:{sequence}")

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(record, index) for index in range(workers)]
            for future in futures:
                future.result(timeout=30)
        rows = self.account_events()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cycle_check"]["count"], workers * checks_per_worker)
        self.assertLessEqual(rows[0]["cycle_check"]["first_at"], rows[0]["cycle_check"]["last_at"])
        self.assertEqual(rows[0]["created_at"], rows[0]["cycle_check"]["last_at"])

    def test_updated_old_id_stays_in_latest_window_after_many_other_account_events(self):
        self.check(100)
        first_id = self.account_events()[0]["id"]
        with self.store.connect() as db:
            db.executemany("INSERT INTO events(account_id,kind,message,created_at) VALUES (?,?,?,?)",
                           [("second", "order", f"真实订单 {index}", 200 + index) for index in range(110)])
        self.check(500, "最新循环检查")
        rows = self.store.events()
        self.assertEqual(len(rows), 100)
        self.assertEqual(rows[0]["id"], first_id)
        self.assertEqual((rows[0]["created_at"], rows[0]["cycle_check"]["count"]), (500, 2))
        self.assertEqual([row["created_at"] for row in rows[1:]], list(range(309, 210, -1)))

    def test_equal_timestamps_keep_descending_id_as_tie_breaker(self):
        self.check(100)
        first_id = self.account_events()[0]["id"]
        self.event(100, "second", "order")
        self.check(100)
        rows = self.store.events()
        self.assertGreater(rows[0]["id"], first_id)
        self.assertEqual(rows[1]["id"], first_id)
        self.assertEqual(rows[1]["cycle_check"]["count"], 2)

    def test_invalid_metadata_is_preserved_and_never_hides_the_event_or_merges(self):
        valid = {"symbol": SYMBOL, "phase": "open", "count": 3, "first_at": 90, "last_at": 100}
        cases = [None, "{broken", "null", "[]", json.dumps({**valid, "count": True}),
                 json.dumps({**valid, "count": 0}), json.dumps({**valid, "phase": "holding"}),
                 json.dumps({**valid, "symbol": "OTHER"}), json.dumps({**valid, "first_at": 101}),
                 json.dumps({**valid, "last_at": float("nan")}), json.dumps({**valid, "diagnostic": []}),
                 json.dumps({**valid, "diagnostic": {"symbol": "CLUSD1"}}),
                 json.dumps({**valid, "unexpected": True})]
        for index, raw in enumerate(cases):
            account = "invalid_" + str(index)
            with self.subTest(raw=raw):
                with self.store.connect() as db:
                    old_id = db.execute("INSERT INTO events(account_id,kind,message,created_at,cycle_check) VALUES (?,?,?,?,?)",
                                        (account, "cycle_check", "必须保留的损坏旧记录", 100, raw)).lastrowid
                before = self.account_events(account)[0]
                self.assertEqual(set(before), EVENT_FIELDS)
                self.assertEqual(before["message"], "必须保留的损坏旧记录")
                self.check(101, account=account)
                rows = self.account_events(account)
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0]["cycle_check"]["count"], 1)
                self.assertEqual(rows[1], before)
                with self.store.connect() as db:
                    self.assertEqual(db.execute("SELECT cycle_check FROM events WHERE id=?", (old_id,)).fetchone()[0], raw)

    def test_regular_event_with_forged_metadata_is_not_treated_as_a_cycle_check(self):
        metadata = {"symbol": SYMBOL, "phase": "open", "count": 3, "first_at": 90, "last_at": 100}
        with self.store.connect() as db:
            db.execute("INSERT INTO events(account_id,kind,message,created_at,cycle_check) VALUES (?,?,?,?,?)",
                       ("first", "error", "真实错误必须保留", 100, json.dumps(metadata)))
        self.assertEqual(set(self.account_events()[0]), EVENT_FIELDS)
        self.check(101)
        rows = self.account_events()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["message"], "真实错误必须保留")
        self.assertEqual(rows[0]["cycle_check"]["count"], 1)

    def test_invalid_new_inputs_fail_without_writing_events(self):
        defaults = {"account_id": "first", "symbol": SYMBOL, "phase": "open", "message": "检查"}
        for change in ({"account_id": "../first"}, {"symbol": "OTHER"}, {"phase": "repair"}, {"message": ""},
                       {"diagnostic": []}, {"diagnostic": {"phase": "close"}}, {"diagnostic": {"value": float("nan")}}):
            with self.subTest(change=change), self.assertRaises(TradingError):
                self.store.record_cycle_check(**{**defaults, **change})
        self.assertEqual(self.store.events(), [])

    def test_clock_reversal_starts_a_valid_new_segment_without_rewriting_history(self):
        self.check(100)
        old = deepcopy(self.account_events()[0])
        self.check(90)
        rows = self.account_events()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], old)
        self.assertEqual(rows[1]["cycle_check"], {"symbol": SYMBOL, "phase": "open", "count": 1,
                                                "first_at": 90, "last_at": 90})

    def test_latest_queries_use_indexes_without_sorting_all_history(self):
        self.check(100)
        with self.store.connect() as db:
            latest_account = " ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN SELECT id,kind,cycle_check FROM events WHERE account_id=? ORDER BY id DESC LIMIT 1", ("first",)))
            latest_events = " ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN SELECT * FROM events ORDER BY created_at DESC,id DESC LIMIT ?", (100,)))
        self.assertIn("USING INDEX idx_events_account (account_id=?)", latest_account)
        self.assertIn("USING INDEX idx_events_created", latest_events)
        self.assertNotIn("USE TEMP B-TREE", latest_account + latest_events)
