"""Connection reuse keeps transactions, threads and fresh reads independent."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from trading.store import Store


class StoreConnectionScopeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name) / "ledger.db")

    def test_one_handle_retains_independent_commit_rollback_and_fresh_external_reads(self):
        other = Store(self.store.path)
        connect = sqlite3.connect
        with patch("trading.store.sqlite3.connect", wraps=connect) as opened:
            with self.store.connection_scope():
                self.store.put("value", 1)
                self.assertEqual(other.get("value"), 1)  # Already committed.
                with self.assertRaises(RuntimeError):
                    with self.store.connect() as db:
                        handle = db
                        db.execute("UPDATE kv SET data='2' WHERE key='value'")
                        raise RuntimeError("rollback only this operation")
                self.assertEqual(self.store.get("value"), 1)
                other.put("value", 3)
                with self.store.connection_scope():
                    self.assertEqual(self.store.get("value"), 3)
            self.assertEqual(opened.call_count, 3)  # One scoped handle, two external handles.
        with self.assertRaises(sqlite3.ProgrammingError):
            handle.execute("SELECT 1")

    def test_idle_scope_does_not_hold_writer_lock_and_other_threads_get_own_handles(self):
        entered, release = threading.Event(), threading.Event()
        def first():
            with self.store.connection_scope():
                with self.store.connect() as db:
                    handle = db
                entered.set()
                self.assertTrue(release.wait(3))
                self.store.put("first", True)
                return handle
        def second():
            with self.store.connection_scope(), self.store.connect() as db:
                db.execute("INSERT INTO kv VALUES ('second','true')")
                return db
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_result = pool.submit(first)
            self.assertTrue(entered.wait(3))
            try:
                second_handle = pool.submit(second).result(timeout=1)
            finally:
                release.set()
            self.assertIsNot(first_result.result(timeout=3), second_handle)
        self.assertTrue(self.store.get("first"))
        self.assertTrue(self.store.get("second"))

    def test_scoped_events_use_account_time_index_and_preserve_global_order(self):
        with self.store.connect() as db:
            db.executemany("INSERT INTO events(account_id,kind,message,created_at) VALUES (?,?,?,?)",
                           [("other", "info", "other", 100 + i) for i in range(100)]
                           + [("mine", "info", "mine", 1), ("", "info", "global", 2)])
            plan = db.execute("EXPLAIN QUERY PLAN SELECT * FROM events WHERE account_id IN (?, '') "
                              "ORDER BY created_at DESC,id DESC LIMIT 100", ("mine",)).fetchall()
        self.assertTrue(any("idx_events_account_created" in row[3] for row in plan))
        self.assertEqual([row["message"] for row in self.store.events(account_id="mine")], ["global", "mine"])
