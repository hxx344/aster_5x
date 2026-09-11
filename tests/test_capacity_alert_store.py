"""Durable public-capacity gates and outbox races; no HTTP or Feishu delivery."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from trading.models import SYMBOLS, TIERS, TradingError
from trading.store import Store


IDENTITY = "a" * 64


class CapacityAlertStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.sqlite3"
        self.store = Store(self.path)
        self.now = 1000.0
        clock = patch("trading.store.time.time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def observe(self, value="11000", *, symbol="XAUUSD1", leverage=5, checked_at=None, identity=IDENTITY, cooldown=300):
        return self.store.observe_capacity_alert(symbol, leverage, value, threshold="10000", cooldown=cooldown,
                                                 identity=identity, checked_at=self.now if checked_at is None else checked_at)

    def gate(self, symbol="XAUUSD1", leverage=5):
        return self.store.get(self.store.capacity_alert_key(symbol, leverage))

    def row(self, notification_id):
        with self.store.connect() as db:
            result = db.execute("SELECT * FROM outbox WHERE id=?", (notification_id,)).fetchone()
            return dict(result) if result else None

    def all_rows(self):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM outbox ORDER BY id")]

    def test_strict_threshold_and_first_high_do_not_require_waiting_for_cooldown(self):
        self.now = 1
        self.assertFalse(self.observe("9999"))
        self.assertFalse(self.observe("10000"))
        self.assertEqual(self.store.pending_notifications(), 0)
        self.assertTrue(self.observe("10000.0000001"))
        self.assertFalse(self.gate()["notified"])
        self.assertIsNone(self.gate()["last_alert"])
        self.assertEqual(self.store.pending_notifications(), 1)

    def test_continuous_high_is_sent_once_across_restart(self):
        self.assertTrue(self.observe())
        item = self.store.due_notifications()[0]
        self.store.notification_result(item, True)
        self.assertTrue(self.gate()["notified"])
        self.store = Store(self.path)
        self.now += 1000
        self.assertFalse(self.observe("20000"))
        self.assertEqual(self.store.pending_notifications(), 0)
        self.assertEqual(len(self.all_rows()), 1)

    def test_restart_refreshes_existing_pending_id_without_duplicate_queue_entry(self):
        self.observe()
        pending_id = self.gate()["pending_id"]
        self.store = Store(self.path)
        self.now += 5
        self.assertFalse(self.observe("12000"))
        self.assertEqual(self.gate()["pending_id"], pending_id)
        self.assertEqual(len(self.all_rows()), 1)
        self.assertIn("12,000.00", self.store.due_notifications()[0]["message"])

    def test_failed_delivery_does_not_start_cooldown_or_reset_retry_on_refresh(self):
        self.observe()
        item = self.store.due_notifications()[0]
        self.store.notification_result(item, False)
        failed = self.row(item["id"])
        self.assertEqual((failed["attempts"], failed["due_at"]), (1, 1010))
        self.assertFalse(self.gate()["notified"])
        self.assertIsNone(self.gate()["last_alert"])
        self.now = 1006
        self.assertFalse(self.observe("12500"))
        refreshed = self.row(item["id"])
        self.assertEqual((refreshed["attempts"], refreshed["due_at"]), (1, 1010))
        self.assertIsNone(self.store.notification_for_delivery(item["id"]))
        self.now = 1010
        self.assertIn("12,500.00", self.store.notification_for_delivery(item["id"])["message"])
        # A stale callback item must not overwrite the database's newer attempt count.
        self.store.notification_result(item, False)
        self.assertEqual(self.row(item["id"])["attempts"], 2)
        self.assertEqual(self.row(item["id"])["due_at"], 1030)

    def test_only_actual_success_starts_cooldown_and_high_waits_until_it_expires(self):
        self.observe()
        item = self.store.notification_for_delivery(self.gate()["pending_id"])
        self.now = 1010  # The request was already in flight when its source expired.
        self.store.notification_result(item, True)
        self.assertEqual(self.gate()["last_alert"], 1010)
        self.now = 1011
        self.observe("10000")
        self.assertFalse(self.gate()["notified"])
        self.now = 1012
        self.assertFalse(self.observe())
        self.now = 1309
        self.assertFalse(self.observe())
        self.now = 1310
        self.assertTrue(self.observe())
        self.assertEqual(self.store.pending_notifications(), 1)

    def test_pending_low_is_paused_and_rising_again_uses_the_same_id(self):
        self.observe()
        pending_id = self.gate()["pending_id"]
        self.now += 1
        self.observe("9999")
        self.assertFalse(self.gate()["above"])
        self.assertEqual(self.gate()["fall_generation"], 1)
        self.observe("10000")
        self.assertEqual(self.gate()["fall_generation"], 1)
        self.assertEqual(self.row(pending_id)["expires_at"], 0)
        self.assertEqual(self.store.due_notifications(), [])
        self.assertIsNone(self.store.notification_for_delivery(pending_id))
        self.assertEqual(self.store.pending_notifications(), 0)
        self.store = Store(self.path)
        self.now += 1
        self.assertFalse(self.observe())
        self.assertEqual(self.gate()["pending_id"], pending_id)
        self.assertEqual(self.store.pending_notifications(), 1)
        refreshed = self.store.notification_for_delivery(pending_id)
        self.assertEqual(refreshed["capacity_generation"], 1)
        self.store.notification_result(refreshed, True)
        self.assertTrue(self.gate()["notified"])
        self.now += 500
        self.assertFalse(self.observe())
        self.assertEqual(len(self.all_rows()), 1)

    def test_in_flight_success_after_fall_and_rise_preserves_new_crossing_after_cooldown(self):
        self.observe()
        item = self.store.notification_for_delivery(self.gate()["pending_id"])
        self.assertEqual(item["capacity_generation"], 0)
        self.now += 1
        self.observe("0")
        self.now += 1
        self.assertFalse(self.observe())
        self.assertEqual(self.gate()["pending_id"], item["id"])
        self.now += 1
        self.store.notification_result(item, True)
        self.assertFalse(self.gate()["notified"])
        self.assertIsNone(self.gate()["pending_id"])
        self.assertEqual(self.gate()["last_alert"], self.now)
        self.assertEqual(self.gate()["fall_generation"], 1)
        self.store = Store(self.path)
        self.now += 299
        self.assertFalse(self.observe())
        self.assertEqual(len(self.all_rows()), 1)
        self.now += 1
        self.assertTrue(self.observe())
        self.assertNotEqual(self.gate()["pending_id"], item["id"])
        self.assertEqual(len(self.all_rows()), 2)

    def test_in_flight_success_while_low_records_success_but_keeps_gate_rearmed(self):
        self.observe()
        item = self.store.notification_for_delivery(self.gate()["pending_id"])
        self.now += 1
        self.observe("10000")
        self.now += 1
        self.store.notification_result(item, True)
        self.assertFalse(self.gate()["notified"])
        self.assertEqual(self.gate()["last_alert"], self.now)
        self.now += 1
        self.assertFalse(self.observe())
        self.now += 300
        self.assertTrue(self.observe())

    def test_identity_change_cancels_old_pending_and_old_success_cannot_update_new_gate(self):
        self.observe()
        old = self.store.notification_for_delivery(self.gate()["pending_id"])
        self.now += 1
        self.assertTrue(self.observe(identity="b" * 64))
        new_gate = self.gate()
        self.assertNotEqual(old["id"], new_gate["pending_id"])
        self.assertEqual(self.row(old["id"])["expires_at"], 0)
        self.assertIsNone(self.store.notification_for_delivery(old["id"]))
        self.store.notification_result(old, True)
        self.assertEqual(self.gate(), new_gate)
        self.assertEqual(self.store.events(), [])
        fresh = self.store.notification_for_delivery(new_gate["pending_id"])
        self.assertEqual(fresh["capacity_identity"], "b" * 64)
        self.store.notification_result(fresh, True)
        self.assertTrue(self.gate()["notified"])
        event = self.store.events()[0]
        self.assertEqual((event["account_id"], event["kind"], event["message"]),
                         ("", "capacity", "XAUUSD1 5x 额度达标提醒已发送飞书"))

    def test_all_symbols_and_tiers_have_independent_gates(self):
        for symbol in SYMBOLS:
            for leverage in TIERS:
                self.assertTrue(self.observe(symbol=symbol, leverage=leverage))
        self.assertEqual(self.store.pending_notifications(), 9)
        ids = {self.gate(symbol, leverage)["pending_id"] for symbol in SYMBOLS for leverage in TIERS}
        self.assertEqual(len(ids), 9)
        self.observe("0", symbol="XAUUSD1", leverage=5)
        self.assertEqual(self.store.pending_notifications(), 8)
        self.assertTrue(self.gate("XAUUSD1", 10)["above"])
        self.assertTrue(self.gate("CLUSD1", 5)["above"])

    def test_expiration_is_based_on_sample_time_and_send_rechecks_it(self):
        self.observe(checked_at=995)
        item = self.store.due_notifications()[0]
        self.assertEqual(item["expires_at"], 1003)
        self.now = 1002.999
        self.assertIsNotNone(self.store.notification_for_delivery(item["id"]))
        self.now = 1003
        self.assertEqual(self.store.due_notifications(), [])
        self.assertIsNone(self.store.notification_for_delivery(item["id"]))
        self.assertEqual(self.store.pending_notifications(), 1)

    def test_delivery_reloads_latest_message_after_batch_selection(self):
        self.observe("11000")
        original = self.store.due_notifications()[0]
        self.now += 5
        self.assertFalse(self.observe("12345.67"))
        refreshed = self.store.notification_for_delivery(original["id"])
        self.assertNotEqual(refreshed["message"], original["message"])
        self.assertIn("12,345.67", refreshed["message"])
        self.assertIn("公开剩余可开额度（估算）", refreshed["message"])
        self.assertIn("未扣除个人持仓和挂单占用", refreshed["message"])
        self.assertIn("触发条件：> 10,000.00", refreshed["message"])
        self.assertIn("1970-01-01T00:16:45+00:00", refreshed["message"])
        self.assertEqual(refreshed["expires_at"], 1013)

    def test_stale_future_and_out_of_order_observations_cannot_rearm_gate(self):
        self.observe()
        before = self.gate()
        self.now = 1002
        self.assertFalse(self.observe("0", checked_at=999))
        self.assertFalse(self.observe("0", checked_at=994))
        self.assertFalse(self.observe("0", checked_at=1004))
        self.assertEqual(self.gate(), before)
        self.assertEqual(self.store.pending_notifications(), 1)

    def test_invalidating_failed_source_pauses_pending_without_claiming_a_fall(self):
        self.observe()
        self.observe(symbol="XAUUSD1", leverage=10)
        self.observe(symbol="CLUSD1", leverage=5)
        gate = self.gate()
        self.store.invalidate_capacity_alert("XAUUSD1")
        self.assertEqual(self.gate(), gate)
        self.assertEqual(self.store.pending_notifications(), 1)
        self.assertIsNone(self.store.notification_for_delivery(gate["pending_id"]))
        self.now += 1
        self.assertFalse(self.observe())
        self.assertEqual(self.gate()["pending_id"], gate["pending_id"])
        self.assertIsNotNone(self.store.notification_for_delivery(gate["pending_id"]))

    def test_invalidating_does_not_rearm_an_already_sent_high(self):
        self.observe()
        self.store.notification_result(self.store.due_notifications()[0], True)
        before = self.gate()
        self.store.invalidate_capacity_alert("XAUUSD1", 5)
        self.assertEqual(self.gate(), before)
        self.now += 1000
        self.assertFalse(self.observe())
        self.assertEqual(self.store.pending_notifications(), 0)

    def test_concurrent_store_instances_atomically_enqueue_only_one_message(self):
        stores = [Store(self.path) for _ in range(6)]
        barrier = threading.Barrier(len(stores))
        def observe(store):
            barrier.wait()
            return store.observe_capacity_alert("XAUUSD1", 5, "11000", threshold="10000", cooldown=300,
                                                identity=IDENTITY, checked_at=self.now, now=self.now)
        with ThreadPoolExecutor(max_workers=len(stores)) as pool:
            self.assertEqual(sum(pool.map(observe, stores)), 1)
        self.assertEqual(self.store.pending_notifications(), 1)
        self.assertEqual(len(self.all_rows()), 1)

    def test_gate_write_failure_rolls_back_enqueued_message(self):
        with self.store.connect() as db:
            db.execute("""CREATE TRIGGER fail_capacity_gate BEFORE INSERT ON kv
                WHEN NEW.key LIKE 'capacity_alert:%' BEGIN SELECT RAISE(ABORT, 'simulated crash'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.observe()
        self.assertIsNone(self.gate())
        self.assertEqual(self.all_rows(), [])
        with self.store.connect() as db:
            db.execute("DROP TRIGGER fail_capacity_gate")
        self.assertTrue(self.observe())

    def test_success_and_gate_update_roll_back_together_on_failure(self):
        self.observe()
        item = self.store.due_notifications()[0]
        with self.store.connect() as db:
            db.execute("""CREATE TRIGGER fail_capacity_update BEFORE UPDATE ON kv
                WHEN NEW.key LIKE 'capacity_alert:%' BEGIN SELECT RAISE(ABORT, 'simulated crash'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.notification_result(item, True)
        self.assertIsNone(self.row(item["id"])["delivered_at"])
        self.assertEqual(self.gate()["pending_id"], item["id"])
        with self.store.connect() as db:
            db.execute("DROP TRIGGER fail_capacity_update")
        self.store.notification_result(item, True)
        self.assertIsNotNone(self.row(item["id"])["delivered_at"])
        self.assertTrue(self.gate()["notified"])

    def test_trade_notifications_are_kept_prioritized_and_never_expire(self):
        for leverage in TIERS:
            self.observe(leverage=leverage)
        self.observe(symbol="CLUSD1")
        with self.store.connect() as db:
            db.execute("INSERT INTO outbox(id,message,due_at) VALUES ('trade-completion','成交汇总',?)", (self.now + 1,))
        self.now += 1
        rows = self.store.due_notifications()
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["id"], "trade-completion")
        self.now += 1000
        self.assertEqual([row["id"] for row in self.store.due_notifications()], ["trade-completion"])
        item = self.store.notification_for_delivery("trade-completion")
        self.store.notification_result(item, False)
        self.assertIsNone(self.store.notification_for_delivery("trade-completion"))
        self.store.notification_result(item, True)
        self.assertIsNone(self.store.notification_for_delivery("trade-completion"))

    def test_late_failure_or_duplicate_success_cannot_change_delivered_gate(self):
        self.observe()
        item = self.store.due_notifications()[0]
        self.store.notification_result(item, True)
        before = self.gate()
        delivered = self.row(item["id"])
        self.now += 100
        self.store.notification_result(item, False)
        self.store.notification_result(item, True)
        self.assertEqual(self.gate(), before)
        self.assertEqual(self.row(item["id"]), delivered)

    def test_legacy_outbox_migration_preserves_original_trade_rows(self):
        path = Path(self.directory.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE outbox(id TEXT PRIMARY KEY,message TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,due_at REAL NOT NULL,delivered_at REAL)")
            db.execute("INSERT INTO outbox VALUES ('old-trade','legacy completed trade',2,100,NULL)")
        legacy = Store(path)
        item = legacy.due_notifications()[0]
        self.assertEqual((item["id"], item["message"], item["attempts"]), ("old-trade", "legacy completed trade", 2))
        self.assertIsNone(item["expires_at"])
        self.assertIsNone(item["capacity_key"])
        legacy.notification_result(item, True)
        self.assertEqual(Store(path).pending_notifications(), 0)

    def test_retired_pending_alert_is_not_delivered_and_restart_preserves_sent_history(self):
        self.observe()
        supported_id = self.gate()["pending_id"]
        retired_key = "capacity_alert:XAUUSD1:4"
        historical_gate = {"pending_id": "retired-pending", "above": True, "identity": IDENTITY,
                           "last_alert": 900, "notified": False}
        self.store.put(retired_key, historical_gate)
        with self.store.connect() as db:
            db.executemany("""INSERT INTO outbox(id,message,due_at,delivered_at,expires_at,capacity_key)
                VALUES (?,?,?,?,?,?)""", [
                    ("retired-pending", "old pending 4x alert", self.now, None, self.now + 8, retired_key),
                    ("retired-sent", "old sent 4x alert", 900, 900, 908, retired_key),
                ])
        sent_before = self.row("retired-sent")
        self.assertIsNone(self.store.notification_for_delivery("retired-pending"))
        self.store = Store(self.path)
        self.assertEqual(self.row("retired-pending")["expires_at"], 0)
        self.assertIsNone(self.row("retired-pending")["delivered_at"])
        self.assertEqual(self.row("retired-sent"), sent_before)
        self.assertEqual(self.store.get(retired_key), historical_gate)
        self.assertEqual(self.store.pending_notifications(), 1)
        self.assertEqual([item["id"] for item in self.store.due_notifications()], [supported_id])
        self.assertIsNone(self.store.notification_for_delivery("retired-pending"))

    def test_concurrent_legacy_migration_adds_columns_once_without_losing_trade_rows(self):
        path = Path(self.directory.name) / "legacy-concurrent.sqlite3"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE outbox(id TEXT PRIMARY KEY,message TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,due_at REAL NOT NULL,delivered_at REAL)")
            db.execute("INSERT INTO outbox VALUES ('old-trade','legacy completed trade',2,100,NULL)")
        barrier = threading.Barrier(6)
        def migrate(_):
            barrier.wait()
            return Store(path).pending_notifications()
        with ThreadPoolExecutor(max_workers=6) as pool:
            self.assertEqual(list(pool.map(migrate, range(6))), [1] * 6)
        migrated = Store(path)
        item = migrated.notification_for_delivery("old-trade")
        self.assertEqual(item["attempts"], 2)
        self.assertIsNone(item["expires_at"])
        self.assertIsNone(item["capacity_key"])

    def test_invalid_market_tier_identity_and_values_do_not_create_state(self):
        for changes in ({"symbol": "BTCUSDT"}, {"leverage": 2}, {"leverage": 4}, {"leverage": True}, {"identity": "secret-webhook"},
                        {"value": "NaN"}, {"cooldown": -1}):
            with self.subTest(changes=changes), self.assertRaises(TradingError):
                self.observe(**changes)
        self.assertIsNone(self.gate())
        self.assertEqual(self.all_rows(), [])


if __name__ == "__main__":
    unittest.main()
