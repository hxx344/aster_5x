"""Local position evidence and durable outbox lifecycle; no exchange or delivery."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from trading import monitoring
from trading import position_imbalance_notifications as imbalance
from trading.store import Store


class PositionImbalanceNotificationsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / "state.sqlite3")
        self.now = 1000.0
        self.instance = "instance-a"
        clock = patch("trading.position_imbalance_notifications.time.time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def observation(self, **changes):
        return {"key": "account:a:XAUUSD1", "label": "主账户", "symbol": "XAUUSD1",
                "long_qty": "10", "short_qty": "9", "sample_times": [self.now, self.now],
                "activity": "idle:0", "max_gap": 90, "suppressed": False, "attention": False, **changes}

    def observe(self, observations=None, *, available=True, revision=None, **changes):
        if observations is None:
            observations = [self.observation(**changes)]
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return imbalance.observe(db, observations, available=available, instance_id=self.instance,
                revision=monitoring.read(db)["revision"] if revision is None else revision, now=self.now)

    def rows(self):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM outbox ORDER BY due_at,id")]

    def pending(self):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM outbox WHERE delivered_at IS NULL AND due_at<=? "
                    "AND (expires_at IS NULL OR expires_at>?) ORDER BY due_at,id", (self.now, self.now))
                    if imbalance.deliverable(db, dict(row), self.now)]

    def state(self):
        with self.store.connect() as db:
            return imbalance.read(db)

    def target(self):
        return self.state()["targets"]["account:a:XAUUSD1"]

    def result(self, item, success):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if success:
                db.execute("UPDATE outbox SET delivered_at=? WHERE id=?", (self.now, item["id"]))
                imbalance.delivered(db, item)
            else:
                db.execute("UPDATE outbox SET attempts=attempts+1,due_at=? WHERE id=?", (self.now + 10, item["id"]))

    def fault(self, *, sent=False, **changes):
        self.observe(**changes)
        self.now += 60
        self.observe(**changes)
        item = self.pending()[0]
        if sent:
            self.result(item, True)
        return item

    def policy(self, *, symbol=None, **changes):
        with self.store.connect() as db:
            config = monitoring.read(db)
            if symbol:
                config["symbols"].setdefault(symbol, {}).update(changes)
            else:
                config.update(changes)
            config["revision"] += 1
            monitoring.write(db, config)

    def test_fault_needs_two_new_samples_and_sixty_source_seconds(self):
        self.observe()
        self.now += 59.999
        self.observe()
        self.assertEqual(self.pending(), [])
        self.now += .001
        self.observe()
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(self.target()["candidate"]["count"], 3)

    def test_balanced_tolerance_boundary_and_zero_are_not_faults(self):
        for long_qty, short_qty in (("1000", "999"), ("999", "1000"), ("0", "0")):
            with self.subTest(long_qty=long_qty, short_qty=short_qty):
                with self.store.connect() as db:
                    imbalance.reset(db)
                self.observe(long_qty=long_qty, short_qty=short_qty)
                self.now += 60
                self.observe(long_qty=long_qty, short_qty=short_qty)
                self.assertEqual(self.rows(), [])
        self.now += 1
        self.fault(long_qty="1000", short_qty="998.999")
        self.assertEqual(len(self.pending()), 1)

    def test_source_timestamps_both_advance_and_duplicates_do_not_count(self):
        self.observe()
        self.now += 5
        self.observe(sample_times=[self.now, 1000])
        self.assertEqual(self.target()["candidate"]["count"], 1)
        self.observe(sample_times=[1000, 1000])
        self.assertEqual(self.target()["candidate"]["count"], 1)
        self.now = 1060
        self.observe(sample_times=[1000, 1000])
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.target()["candidate"]["count"], 1)
        self.observe()
        self.assertEqual(self.target()["candidate"]["count"], 2)
        self.assertEqual(len(self.pending()), 1)

    def test_duplicate_fresh_snapshot_keeps_confirmed_queue_before_send(self):
        item = self.fault()
        self.observe()
        self.assertEqual(self.pending()[0]["id"], item["id"])
        self.assertEqual(self.target()["candidate"]["count"], 2)
        self.now += 8
        with self.store.connect() as db:
            self.assertTrue(imbalance.deliverable(db, item))
        self.now += .001
        with self.store.connect() as db:
            self.assertFalse(imbalance.deliverable(db, item))

    def test_invalid_quantities_and_stale_future_or_incomplete_times_never_queue(self):
        for changes in ({"long_qty": None}, {"short_qty": "NaN"}, {"long_qty": "-1"},
                        {"short_qty": True}, {"sample_times": []}, {"sample_times": [1000]},
                        {"sample_times": [991.999, 1000]}, {"sample_times": [1000, 1001.001]},
                        {"sample_times": [1000, float("nan")]}, {"sample_times": [True, 1000]}):
            with self.subTest(changes=changes):
                self.observe(**changes)
                self.assertIsNone(self.target()["candidate"])
        self.assertEqual(self.rows(), [])

    def test_freshness_boundaries_and_earliest_side_confirmation(self):
        self.observe(sample_times=[992, 1001])
        self.now = 1061
        self.observe(sample_times=[1053, 1062])
        self.assertEqual(self.pending(), [])
        self.now = 1069
        self.observe(sample_times=[1061, 1070])
        self.assertEqual(len(self.pending()), 1)

    def test_older_fresh_pair_cannot_reauthorize_a_pending_fault(self):
        self.fault()
        self.now += 1
        self.observe(sample_times=[1059, 1060])
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.rows()[0]["expires_at"], 0)
        self.observe()
        self.assertEqual(len(self.pending()), 1)

    def test_brief_missing_snapshot_preserves_candidate_but_long_gap_restarts_it(self):
        self.observe(max_gap=20)
        self.now += 10
        self.observe(sample_times=[], max_gap=20)
        self.assertEqual(self.target()["candidate"]["count"], 1)
        self.now += 10
        self.observe(sample_times=[], max_gap=20)
        self.assertIsNotNone(self.target()["candidate"])
        self.now += .001
        self.observe(sample_times=[], max_gap=20)
        self.assertIsNone(self.target()["candidate"])
        self.now = 1060
        self.observe(max_gap=20)
        self.assertEqual(self.target()["candidate"]["count"], 1)
        self.assertEqual(self.rows(), [])

    def test_max_gap_is_bounded_and_outage_cancels_pending_without_recovery(self):
        item = self.fault()
        self.now += 1
        self.observe(sample_times=[])
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.rows()[0]["expires_at"], 0)
        self.now += 10
        self.observe()
        self.assertEqual(self.pending()[0]["id"], item["id"])
        self.result(item, True)
        self.now += 181
        self.observe(sample_times=[], max_gap=10000)
        self.assertIsNone(self.target()["candidate"])
        self.assertTrue(self.target()["incident"]["fault_sent"])
        self.observe()
        self.now += 60
        self.observe()
        self.assertEqual(self.pending(), [])
        self.assertEqual(len(self.rows()), 1)

    def test_unconfirmed_candidate_has_no_new_samples_timeout_at_twenty_second_floor(self):
        self.observe(max_gap=0)
        self.now += 19
        self.observe(sample_times=[], max_gap=0)
        self.assertIsNotNone(self.target()["candidate"])
        self.now += 2
        self.observe(sample_times=[], max_gap=0)
        self.assertIsNone(self.target()["candidate"])

    def test_activity_generation_change_cancels_pending_even_when_trade_already_finished(self):
        item = self.fault()
        self.now += 1
        self.observe(activity="idle:2")
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.target()["candidate"]["count"], 1)
        self.now += 59
        self.observe(activity="idle:2")
        self.assertEqual(self.pending(), [])
        self.now += 1
        self.observe(activity="idle:2")
        self.assertNotEqual(self.pending()[0]["id"], item["id"])

    def test_suppression_never_counts_as_recovery_and_keeps_sent_incident(self):
        item = self.fault(sent=True)
        self.now += 1
        self.observe(short_qty="10", suppressed=True)
        self.now += 60
        self.observe(short_qty="10", suppressed=True)
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.target()["incident"]["fault_id"], item["id"])
        self.observe(short_qty="10")
        self.now += 29.999
        self.observe(short_qty="10")
        self.assertEqual(self.pending(), [])
        self.now += .001
        self.observe(short_qty="10")
        self.assertIn("recovery", self.pending()[0]["id"])

    def test_first_balanced_snapshot_cancels_unsent_fault_without_recovery(self):
        item = self.fault()
        self.result(item, False)
        self.now += 1
        self.observe(short_qty="10")
        self.assertIsNone(self.target()["incident"])
        self.now += 60
        self.observe(short_qty="10")
        self.assertEqual(self.pending(), [])
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["expires_at"], 0)

    def test_sent_incident_has_one_fault_one_stable_recovery_and_can_recur(self):
        self.fault(sent=True)
        self.now += 60
        self.observe()
        self.assertEqual(len(self.rows()), 1)
        self.now += 1
        self.observe(short_qty="10")
        self.now += 29.999
        self.observe(short_qty="10")
        self.assertEqual(self.pending(), [])
        self.now += .001
        self.observe(short_qty="10")
        recovery = self.pending()[0]
        self.observe(short_qty="10")
        self.assertEqual(self.pending()[0]["id"], recovery["id"])
        self.result(recovery, True)
        self.observe(short_qty="10")
        self.assertEqual(self.pending(), [])
        self.now += 1
        self.fault()
        self.assertEqual(len(self.rows()), 3)

    def test_repeated_imbalance_cancels_recovery_and_reuses_its_retry_id(self):
        self.fault(sent=True)
        self.now += 1
        self.observe(short_qty="10")
        self.now += 30
        self.observe(short_qty="10")
        recovery = self.pending()[0]
        self.result(recovery, False)
        self.now += 1
        self.observe()
        self.assertEqual(self.pending(), [])
        self.now += 1
        self.observe(short_qty="10")
        self.now += 29.999
        self.observe(short_qty="10")
        self.assertEqual(self.pending(), [])
        self.now += .001
        self.observe(short_qty="10")
        self.assertEqual(self.pending()[0]["id"], recovery["id"])
        self.assertEqual(len(self.rows()), 2)

    def test_restart_keeps_sent_incident_but_restarts_recovery_window(self):
        item = self.fault(sent=True)
        self.now += 1
        self.observe(short_qty="10")
        self.now += 29
        self.observe(short_qty="10")
        self.store = Store(self.store.path)
        self.instance = "instance-b"
        self.now += 1
        self.observe(short_qty="10")
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.target()["incident"]["fault_id"], item["id"])
        self.now += 30
        self.observe(short_qty="10")
        self.assertIn("recovery", self.pending()[0]["id"])

    def test_restart_during_ongoing_imbalance_does_not_send_another_fault(self):
        original = self.fault(sent=True)
        self.store = Store(self.store.path)
        self.instance = "instance-b"
        self.now += 1
        self.observe()
        self.now += 60
        self.observe()
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.target()["incident"]["fault_id"], original["id"])
        self.assertEqual(len(self.rows()), 1)

    def test_activity_change_revokes_stable_recovery_until_new_thirty_second_window(self):
        self.fault(sent=True)
        self.now += 1
        self.observe(short_qty="10")
        self.now += 30
        self.observe(short_qty="10")
        recovery = self.pending()[0]
        self.now += 1
        self.observe(short_qty="10", activity="idle:2")
        self.assertEqual(self.pending(), [])
        self.now += 30
        self.observe(short_qty="10", activity="idle:2")
        self.assertEqual(self.pending()[0]["id"], recovery["id"])

    def test_instance_change_and_clock_rollback_cannot_reuse_pending_confirmation(self):
        self.fault()
        self.instance = "instance-b"
        self.now += 1
        self.observe()
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.target()["candidate"]["count"], 1)
        self.now = 500
        self.observe()
        self.now = 559.999
        self.observe()
        self.assertEqual(self.pending(), [])
        self.now = 560
        self.observe()
        self.assertEqual(len(self.pending()), 1)

    def test_backoff_and_fault_identity_survive_reobservation(self):
        item = self.fault()
        self.result(item, False)
        self.observe()
        self.assertEqual(self.rows()[0]["due_at"], 1070)
        self.assertEqual(self.rows()[0]["attempts"], 1)
        self.assertEqual(self.pending(), [])
        self.now += 10
        self.observe()
        self.assertEqual(self.pending()[0]["id"], item["id"])

    def test_unavailable_or_disabled_then_enabled_needs_new_confirmation(self):
        for mode in ("available", "feishu_enabled", "position_imbalance_alerts"):
            with self.subTest(mode=mode):
                self.fault()
                if mode == "available":
                    self.assertFalse(self.observe(available=False))
                else:
                    self.policy(**{mode: False})
                    self.assertFalse(self.observe())
                    self.policy(**{mode: True})
                self.observe()
                self.assertEqual(self.pending(), [])
                self.assertEqual(self.target()["candidate"]["count"], 1)
                self.now += 60

    def test_symbol_policy_and_reset_isolate_other_targets(self):
        def both():
            return [self.observation(), self.observation(key="account:b:CLUSD1", symbol="CLUSD1", label="另一账户")]
        self.observe(both())
        self.now += 60
        self.observe(both())
        self.assertEqual(len(self.pending()), 2)
        with self.store.connect() as db:
            imbalance.reset(db, "XAUUSD1")
        self.assertEqual(json.loads(self.pending()[0]["symbols"]), ["CLUSD1"])
        self.assertEqual(len(self.pending()), 1)
        self.policy(symbol="XAUUSD1", alerts=False)
        self.observe(both())
        self.assertNotIn("account:a:XAUUSD1", self.state()["targets"])
        self.policy(symbol="XAUUSD1", alerts=True)
        self.observe(both())
        self.assertEqual(self.target()["candidate"]["count"], 1)
        self.assertEqual(len(self.pending()), 1)

    def test_position_alerts_ignore_monitor_selection_but_honor_symbol_alert_switch(self):
        self.policy(monitoring_enabled=False)
        self.policy(symbol="XAUUSD1", monitor=False)
        self.fault()
        self.assertEqual(len(self.pending()), 1)
        self.policy(symbol="XAUUSD1", alerts=False)
        self.assertEqual(self.pending(), [])
        self.observe()
        self.assertEqual(self.state()["targets"], {})

    def test_removed_target_is_cancelled_and_forgotten(self):
        self.fault()
        self.observe([])
        self.assertEqual(self.state()["targets"], {})
        self.assertEqual(self.rows()[0]["expires_at"], 0)
        self.observe()
        self.assertEqual(self.target()["candidate"]["count"], 1)

    def test_stale_revision_is_a_strict_no_op_even_when_unavailable(self):
        self.fault()
        state, rows = self.state(), self.rows()
        self.assertFalse(self.observe(available=False, revision=-1))
        self.assertEqual(self.state(), state)
        self.assertEqual(self.rows(), rows)

    def test_late_delivery_cannot_consume_a_replacement_incident(self):
        old = self.fault()
        self.now += 1
        self.observe(activity="idle:1")
        self.now += 60
        self.observe(activity="idle:1")
        current = self.pending()[0]
        self.result(old, True)
        self.assertFalse(self.target()["incident"]["fault_sent"])
        self.assertEqual(self.pending()[0]["id"], current["id"])

    def test_failed_state_write_rolls_back_outbox_mutation(self):
        self.observe()
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER reject_position BEFORE INSERT ON kv "
                "WHEN NEW.key='position_imbalance_notifications' BEGIN SELECT RAISE(ABORT,'failed'); END")
        self.now += 60
        with self.assertRaises(sqlite3.IntegrityError):
            self.observe()
        self.assertEqual(self.rows(), [])

    def test_message_contains_target_quantities_difference_sample_span_and_attention(self):
        item = self.fault(attention=True)
        for text in ("主账户", "XAUUSD1", "总多仓：10", "总空仓：9", "差额：1", "10.0000%",
                     "样本时间（UTC）", "1970-01-01", "60.0 秒", "自动处理已暂停", "ASTER 仓位不平衡"):
            self.assertIn(text, item["message"])
        self.assertEqual(json.loads(item["symbols"]), ["XAUUSD1"])

    def test_delivery_capture_tracks_exact_current_identity_and_expires_with_permission(self):
        bindings = [{"id": "a", "mode": "live", "env_prefix": "ASTER_A"}]
        item = self.fault(scope="account:a", bindings=bindings)
        with self.store.connect() as db:
            self.assertEqual(imbalance.delivery_observation(db, item),
                {"key": "account:a:XAUUSD1", "scope": "account:a", "activity": "idle:0", "bindings": bindings})
        self.now += 1
        self.observe(scope="account:a", bindings=bindings, activity="idle:2")
        with self.store.connect() as db:
            self.assertIsNone(imbalance.delivery_observation(db, item))


if __name__ == "__main__":
    unittest.main()
