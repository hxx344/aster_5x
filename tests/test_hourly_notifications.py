"""Persistent hourly buckets and delivery races; no external requests."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from trading.store import Store


class HourlyNotificationStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / "state.sqlite3")
        self.now = 0.0
        clock = patch("trading.store.time.time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def due(self, available=True):
        return self.store.hourly_summary_due(available=available)

    def status(self, available=True):
        return self.store.hourly_summary_status(available=available)

    def rows(self):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM outbox ORDER BY due_at,id")]

    def queue(self):
        self.due()
        self.now = self.status()["next_due_at"]
        token = self.due()
        self.assertTrue(self.store.enqueue_hourly_summary(token, "current summary"))
        return self.store.due_notifications()[0]

    def test_first_start_waits_for_next_boundary_even_when_started_on_the_hour(self):
        self.now = 3600
        self.assertIsNone(self.due())
        self.assertEqual(self.status(), {"interval_seconds": 3600, "next_due_at": 7200,
                                        "last_sent_at": None, "pending": False})
        self.now = 7199.999
        self.assertIsNone(self.due())
        self.assertEqual(self.rows(), [])
        self.now = 7200
        self.assertEqual(self.due()["period_at"], 7200)

    def test_bucket_is_unique_and_restart_preserves_due_and_last_success(self):
        self.assertIsNone(self.due())
        self.now = 3590
        self.store = Store(self.store.path)
        self.assertEqual(self.status()["next_due_at"], 3600)
        self.now = 3600
        token = self.due()
        self.assertTrue(self.store.enqueue_hourly_summary(token, "one"))
        self.assertFalse(self.store.enqueue_hourly_summary(token, "duplicate"))
        row = self.rows()[0]
        self.assertEqual((row["id"], row["category"], row["symbols"], row["expires_at"]),
                         ("scheduled-summary:1:3600000", "hourly_summary", "[]", 7200))
        self.assertTrue(self.status()["pending"])
        self.store.notification_result(row, True)
        self.now += 10
        self.store = Store(self.store.path)
        self.assertIsNone(self.due())
        self.assertEqual(self.status(), {"interval_seconds": 3600, "next_due_at": 7200,
                                        "last_sent_at": 3600, "pending": False})

    def test_late_restart_only_enqueues_the_current_hour_and_expires_old_summary(self):
        old = self.queue()
        self.now = 5 * 3600 + 75
        self.store = Store(self.store.path)
        token = self.due()
        self.assertEqual(token["period_at"], 5 * 3600)
        self.assertTrue(self.store.enqueue_hourly_summary(token, "latest"))
        self.assertEqual([row["id"] for row in self.store.due_notifications()], ["scheduled-summary:1:18000000"])
        self.assertIsNone(self.store.notification_for_delivery(old["id"]))
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.status()["next_due_at"], 6 * 3600)

    def test_expired_summary_is_absent_from_pending_counts_and_delivery(self):
        item = self.queue()
        self.now = item["expires_at"]
        self.assertEqual(self.store.pending_notifications(), 0)
        self.assertFalse(self.status()["pending"])
        self.assertEqual(self.store.due_notifications(), [])
        self.assertIsNone(self.store.notification_for_delivery(item["id"]))

    def test_retry_backoff_is_persisted_without_extending_expiry(self):
        item = self.queue()
        self.store.notification_result(item, False)
        self.assertEqual(self.rows()[0]["due_at"], 3610)
        self.assertTrue(self.status()["pending"])
        self.assertEqual(self.store.due_notifications(), [])
        self.now = 3610
        self.store = Store(self.store.path)
        self.assertEqual(self.store.due_notifications()[0]["attempts"], 1)
        self.store.notification_result(item, False)
        self.assertEqual(self.rows()[0]["due_at"], 3630)
        self.assertEqual(self.rows()[0]["expires_at"], 7200)
        self.now = 7200
        self.assertEqual(self.store.due_notifications(), [])

    def test_both_switches_cancel_retries_and_reset_to_next_hour(self):
        for setting in ("feishu_enabled", "hourly_summary_alerts"):
            with self.subTest(setting=setting):
                item = self.queue()
                self.store.notification_result(item, False)
                self.now += 20
                self.store.edit_monitoring({setting: False})
                self.assertIsNone(self.status()["next_due_at"])
                self.assertFalse(self.status()["pending"])
                self.store.edit_monitoring({setting: True})
                self.assertEqual(self.status()["next_due_at"], self.now + 3600)
                self.assertIsNone(self.due())
                self.assertIsNone(self.store.notification_for_delivery(item["id"]))
                self.assertEqual(next(row["expires_at"] for row in self.rows() if row["id"] == item["id"]), 0)

    def test_missing_webhook_cancels_pending_and_recovery_waits_for_next_hour(self):
        item = self.queue()
        self.now = 4000
        self.assertIsNone(self.due(available=False))
        self.assertEqual(self.rows()[0]["expires_at"], 0)
        self.assertFalse(self.status(available=False)["pending"])
        self.assertIsNone(self.status(available=False)["next_due_at"])
        self.now = 9000
        self.assertIsNone(self.due())
        self.assertEqual(self.status()["next_due_at"], 12600)
        self.assertIsNone(self.store.notification_for_delivery(item["id"]))

    def test_global_summary_survives_symbol_mute_and_public_monitor_switches(self):
        item = self.queue()
        self.store.edit_monitoring({"monitor": False, "alerts": False}, symbol="XAUUSD1")
        self.store.edit_monitoring({"monitoring_enabled": False, "discovery_enabled": False})
        self.assertTrue(self.status()["pending"])
        self.assertIsNotNone(self.store.notification_for_delivery(item["id"]))

    def test_snapshot_is_discarded_after_settings_change_or_new_hour(self):
        self.due()
        self.now = 3600
        token = self.due()
        self.store.edit_monitoring({"alerts": False}, symbol="XAUUSD1")
        self.assertFalse(self.store.enqueue_hourly_summary(token, "old settings"))
        current = self.due()
        self.now = 7200
        self.assertFalse(self.store.enqueue_hourly_summary(current, "old hour"))
        self.assertEqual(self.rows(), [])
        self.assertTrue(self.store.enqueue_hourly_summary(self.due(), "latest"))

    def test_disable_then_enable_during_generation_does_not_revive_old_snapshot(self):
        self.due()
        self.now = 3600
        token = self.due()
        self.store.edit_monitoring({"feishu_enabled": False})
        self.store.edit_monitoring({"feishu_enabled": True})
        self.assertFalse(self.store.enqueue_hourly_summary(token, "stale"))
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.status()["next_due_at"], 7200)

    def test_concurrent_enqueues_publish_only_one_bucket(self):
        self.due()
        self.now = 3600
        stores = [Store(self.store.path) for _ in range(4)]
        tokens = [store.hourly_summary_due(available=True) for store in stores]
        with ThreadPoolExecutor(4) as pool:
            tasks = [pool.submit(store.enqueue_hourly_summary, token, f"summary {index}")
                     for index, (store, token) in enumerate(zip(stores, tokens))]
            results = [task.result() for task in tasks]
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.status()["next_due_at"], 7200)

    def test_timer_write_failure_rolls_back_enqueue(self):
        self.due()
        self.now = 3600
        token = self.due()
        with self.store.connect() as db:
            db.execute("""CREATE TRIGGER reject_schedule BEFORE UPDATE ON kv
                WHEN NEW.key='hourly_summary_schedule' BEGIN SELECT RAISE(ABORT,'test failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.enqueue_hourly_summary(token, "must roll back")
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.status()["next_due_at"], 3600)

    def test_existing_hourly_schedule_survives_upgrade(self):
        self.now = 3500
        self.store.put("hourly_summary_schedule", {"next_due_at": 3600, "last_sent_at": 1})
        self.assertIsNone(self.due())
        self.assertEqual(self.status()["next_due_at"], 3600)
        self.now = 3600
        self.assertTrue(self.store.enqueue_hourly_summary(self.due(), "upgraded"))
        self.assertEqual(self.status()["next_due_at"], 7200)
        self.assertEqual(self.status()["last_sent_at"], 1)

    def test_legacy_pending_message_survives_upgrade_and_can_be_acknowledged(self):
        self.now = 3605
        self.store.put("hourly_summary_schedule", {"next_due_at": 7200, "last_sent_at": None})
        with self.store.connect() as db:
            db.execute("""INSERT INTO outbox(id,message,due_at,expires_at,category,symbols)
                VALUES ('hourly-summary:1','legacy summary',3600,7200,'hourly_summary','[]')""")
        self.assertIsNone(self.due())
        self.assertTrue(self.status()["pending"])
        self.store.notification_result(self.store.due_notifications()[0], True)
        self.assertEqual(self.status(), {"interval_seconds": 3600, "next_due_at": 7200,
                                        "last_sent_at": 3605, "pending": False})

    def test_first_enable_between_hours_waits_for_full_interval(self):
        self.now = 3500
        self.assertIsNone(self.due())
        self.assertEqual(self.status()["next_due_at"], 7100)
        self.now = 3600
        self.assertIsNone(self.due())

    def test_interval_change_cancels_only_summary_and_preserves_last_sent(self):
        old = self.queue()
        with self.store.connect() as db:
            db.execute("INSERT INTO outbox(id,message,due_at,category,symbols) VALUES ('event','event',0,'trade_summary','[]')")
        self.store.put("hourly_summary_schedule", {"next_due_at": 7200, "last_sent_at": 100})
        self.now = 3617
        self.store.edit_monitoring({"hourly_summary_interval_minutes": 15})
        self.assertEqual(self.status(), {"interval_seconds": 900, "next_due_at": 4517,
                                        "last_sent_at": 100, "pending": False})
        self.assertIsNone(self.store.notification_for_delivery(old["id"]))
        self.assertIsNotNone(self.store.notification_for_delivery("event"))
        self.store = Store(self.store.path)
        self.now = 4516.99
        self.assertIsNone(self.due())
        self.now = 4517
        self.assertTrue(self.store.enqueue_hourly_summary(self.due(), "custom interval"))
        self.assertEqual(self.status()["next_due_at"], 5417)

    def test_same_interval_save_keeps_schedule_revision_and_pending_retry(self):
        self.store.edit_monitoring({"hourly_summary_interval_minutes": 1})
        self.now = 60
        self.assertTrue(self.store.enqueue_hourly_summary(self.due(), "summary"))
        before = self.store.monitoring_settings()
        schedule = self.status()
        self.now = 65
        self.assertEqual(self.store.edit_monitoring({"hourly_summary_interval_minutes": 1}), before)
        self.assertEqual(self.status(), schedule)
        self.assertIsNotNone(self.store.notification_for_delivery(self.rows()[0]["id"]))

    def test_late_custom_interval_keeps_phase_without_catchup_or_delivery_drift(self):
        self.now = 17
        self.store.edit_monitoring({"hourly_summary_interval_minutes": 7})
        self.now = 17 + 420 * 5 + 30
        self.assertTrue(self.store.enqueue_hourly_summary(self.due(), "latest"))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.status()["next_due_at"], 17 + 420 * 6)
        self.now += 15
        self.store.notification_result(self.rows()[0], True)
        self.assertEqual(self.status()["next_due_at"], 17 + 420 * 6)
        self.assertIsNone(self.due())

    def test_interval_change_during_generation_rejects_old_snapshot(self):
        self.due()
        self.now = 3600
        token = self.due()
        self.store.edit_monitoring({"hourly_summary_interval_minutes": 30})
        self.assertFalse(self.store.enqueue_hourly_summary(token, "old cadence"))
        self.assertEqual(self.status()["next_due_at"], 5400)

    def test_changing_back_to_same_interval_and_period_does_not_reuse_cancelled_id(self):
        self.store.edit_monitoring({"hourly_summary_interval_minutes": 1})
        self.now = 60
        self.assertTrue(self.store.enqueue_hourly_summary(self.due(), "cancel me"))
        first_id = self.rows()[0]["id"]
        # Include a clock correction: a new schedule generation must remain distinct.
        self.now = 0
        self.store.edit_monitoring({"hourly_summary_interval_minutes": 2})
        self.store.edit_monitoring({"hourly_summary_interval_minutes": 1})
        self.now = 60
        self.assertTrue(self.store.enqueue_hourly_summary(self.due(), "new generation"))
        self.assertIsNone(self.store.notification_for_delivery(first_id))
        self.assertNotEqual(self.store.due_notifications()[0]["id"], first_id)

    def test_interval_can_be_saved_while_disabled_and_reenable_waits_full_period(self):
        self.store.edit_monitoring({"hourly_summary_alerts": False, "hourly_summary_interval_minutes": 1440})
        self.assertIsNone(self.status()["next_due_at"])
        self.assertEqual(self.status()["interval_seconds"], 86400)
        self.now = 123
        self.store.edit_monitoring({"hourly_summary_alerts": True})
        self.assertEqual(self.status()["next_due_at"], 123 + 86400)


if __name__ == "__main__":
    unittest.main()
