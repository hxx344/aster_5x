"""Cycle preparation stays durable while avoiding redundant display work."""
from contextlib import contextmanager
import copy
from dataclasses import replace
import json
import sqlite3
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from trading.cycle import CyclePlan
from trading.cycle_execution import CycleExecutor
from trading.cycle_quality import ObservedBroker
from trading.models import TradingError, dec
from trading.paper import PaperBroker
from trading.store import Store
from tests import test_cycle_execution_quality as quality_cases


class CyclePreSubmitOptimizationTests(unittest.TestCase):
    setUp = quality_cases.CycleExecutionQualityTests.setUp
    start = quality_cases.CycleExecutionQualityTests.start
    quality = quality_cases.CycleExecutionQualityTests.quality

    def final_observation(self, phase="open", **changes):
        depth = self.f.market.depth("XAUUSD1")
        long, short = ("8824.04", "8824.02") if phase == "open" else ("8824.02", "8824.04")
        # This reference-amount spread deliberately differs from the batch's.
        plan = CyclePlan(phase, "XAUUSD1", dec(2), 2, dec(long), dec(short), dec("99"))
        return {"depth": depth, "quality_plan": replace(plan, **changes),
                "checked_at": time.time(), "checked_monotonic": time.monotonic()}

    def test_intent_index_and_event_commit_once_before_broker_submit(self):
        statements = []
        connect, original = self.f.store.connect, self.f.broker.submit
        @contextmanager
        def observed_connection():
            with connect() as db:
                db.set_trace_callback(statements.append)
                yield db
        def submit(orders):
            statements.append("BROKER_SUBMIT")
            with connect() as db:
                pending = db.execute("SELECT id,account_id,status,data FROM intents").fetchone()
                self.assertEqual((pending["account_id"], pending["status"]), ("test", "pending"))
                durable = json.loads(pending["data"])
                self.assertEqual(durable["orders"], orders)
                self.assertEqual(durable["receipts"], {})
                indexed = db.execute("SELECT * FROM cycle_volume_sync WHERE intent_id=?", (pending["id"],)).fetchone()
                self.assertEqual((indexed["account_id"], indexed["status"]), ("test", "pending"))
                self.assertIsNone(indexed["synced_at"])
                events = db.execute("SELECT account_id,kind,message FROM events").fetchall()
                self.assertEqual([tuple(event) for event in events],
                                 [("test", "cycle", "XAUUSD1 独立循环同时开仓多空，每边 2，2x")])
                self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0], 2)
            return original(orders)
        with patch.object(self.f.store, "connect", side_effect=observed_connection), \
             patch.object(self.f.broker, "submit", side_effect=submit) as sent:
            self.start()
        prefix = statements[:statements.index("BROKER_SUBMIT")]
        self.assertEqual(sum(sql.startswith("BEGIN") for sql in prefix), 1)
        self.assertEqual(prefix.count("COMMIT"), 1)
        self.assertEqual(sum(sql.startswith("INSERT INTO intents") for sql in prefix), 1)
        self.assertEqual(sum(sql.startswith("INSERT INTO cycle_volume_sync") for sql in prefix), 1)
        self.assertEqual(sum(sql.startswith("INSERT INTO events") for sql in prefix), 1)
        self.assertEqual(sent.call_count, 1)
        self.assertEqual(self.quality()["actual"]["status"], "filled")

    def test_index_or_event_failure_rolls_back_the_whole_batch_and_never_sends(self):
        for table in ("cycle_volume_sync", "events"):
            with self.subTest(table=table):
                with self.f.store.connect() as db:
                    db.execute(f"CREATE TRIGGER stop_creation BEFORE INSERT ON {table} "
                               "BEGIN SELECT RAISE(ABORT, 'creation interrupted'); END")
                try:
                    with patch.object(self.f.broker, "submit") as submit, \
                         self.assertRaisesRegex(sqlite3.IntegrityError, "creation interrupted"):
                        self.start()
                    submit.assert_not_called()
                    with self.f.store.connect() as db:
                        for name in ("intents", "cycle_volume_sync", "events"):
                            self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0], 0)
                finally:
                    with self.f.store.connect() as db:
                        db.execute("DROP TRIGGER stop_creation")
        self.assertEqual(self.f.store.get("cycle:test")["phase"], "waiting_open")

    def test_exception_before_commit_rolls_back_and_never_sends(self):
        connect = self.f.store.connect
        @contextmanager
        def interrupted_connection():
            with connect() as db:
                touched = []
                db.set_trace_callback(lambda sql: touched.append(sql) if sql.startswith("INSERT INTO intents") else None)
                yield db
                if touched:
                    raise sqlite3.OperationalError("commit interrupted")
        with patch.object(self.f.store, "connect", side_effect=interrupted_connection), \
             patch.object(self.f.broker, "submit") as submit, self.assertRaisesRegex(sqlite3.OperationalError, "commit interrupted"):
            self.start()
        submit.assert_not_called()
        with connect() as db:
            for name in ("intents", "cycle_volume_sync", "events"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0], 0)

    def test_created_pending_survives_restart_without_resubmitting_or_duplicating_event(self):
        with patch.object(self.executor, "send", side_effect=RuntimeError("crash before send")), \
             self.assertRaisesRegex(RuntimeError, "crash before send"):
            self.start()
        pending = self.f.store.intent("test")
        self.assertEqual(pending["status"], "pending")
        store = Store(self.f.store.path)
        broker = PaperBroker("test", self.f.market, store)
        restored = CycleExecutor(store, broker, self.f.market)
        with patch.object(broker, "submit", side_effect=AssertionError("original order must not be resent")):
            restored.reconcile(self.f.account)
        self.assertIsNone(store.intent("test"))
        self.assertEqual(store.get("cycle:test")["phase"], "waiting_open")
        with store.connect() as db:
            intent = db.execute("SELECT status FROM intents WHERE id=?", (pending["id"],)).fetchone()
            indexed = db.execute("SELECT status FROM cycle_volume_sync WHERE intent_id=?", (pending["id"],)).fetchone()
            self.assertEqual(intent["status"], indexed["status"])
            self.assertIn(intent["status"], ("complete", "aborted"))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE message LIKE '%同时开仓%'").fetchone()[0], 1)

    def test_creation_rejects_an_existing_id_without_updating_it(self):
        with patch.object(self.executor, "send", side_effect=RuntimeError("before send")), self.assertRaises(RuntimeError):
            self.start()
        original = self.f.store.intent("test")
        changed = copy.deepcopy(original)
        changed["quantity"] = "999"
        with self.assertRaises(sqlite3.IntegrityError):
            self.f.store.create_cycle_intent(changed, "must not be inserted")
        self.assertEqual(self.f.store.intent("test"), original)
        with self.f.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)

    def test_final_plan_reuses_exact_batch_amounts_without_rescanning(self):
        final = self.final_observation()
        with patch("trading.cycle_quality._depth_sweeps", side_effect=AssertionError("no display rescan")) as sweep:
            self.start(before_submit=lambda snapshot: final)
        sweep.assert_not_called()
        estimate = self.quality()["final_estimate"]
        self.assertEqual(estimate["status"], "available")
        self.assertEqual((estimate["buy_vwap"], estimate["sell_vwap"]), ("4412.02", "4412.01"))
        self.assertEqual(estimate["spread_bp"], self.quality()["actual"]["spread_bp"])
        self.assertNotEqual(estimate["spread_bp"], "99")
        self.assertEqual(estimate["sampled_at"], final["depth"].timestamp)

    def test_final_close_plan_maps_short_to_buy_and_long_to_sell(self):
        self.start()
        final = self.final_observation("close")
        with patch("trading.cycle_quality._depth_sweeps", side_effect=AssertionError("no display rescan")):
            self.start("close", before_submit=lambda snapshot: final)
        estimate = self.quality()["final_estimate"]
        self.assertEqual((estimate["buy_vwap"], estimate["sell_vwap"]), ("4412.02", "4412.01"))
        self.assertEqual(estimate["spread_bp"], self.quality()["actual"]["spread_bp"])

    def test_mismatched_observation_is_unknown_and_cannot_change_order_quantity(self):
        final = self.final_observation(qty=dec(999))
        with patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            self.start(before_submit=lambda snapshot: final)
        self.assertEqual(self.quality()["final_estimate"]["status"], "unavailable")
        self.assertEqual([order["quantity"] for order in submit.call_args.args[0]], ["2", "2"])
        self.assertEqual(self.quality()["actual"]["status"], "filled")

    def test_plan_identity_and_stale_depth_are_not_reported_as_valid_observations(self):
        from trading.cycle_quality import estimate_from_plan
        for changes in ({"symbol": "CLUSD1"}, {"phase": "close"}):
            final = self.final_observation(**changes)
            result = estimate_from_plan("2", final["quality_plan"], final["depth"], final["checked_at"],
                                        symbol="XAUUSD1", phase="open")
            self.assertEqual(result["status"], "unavailable")
        final = self.final_observation()
        depth = replace(final["depth"], timestamp=time.time() - 4)
        result = estimate_from_plan("2", final["quality_plan"], depth, final["checked_at"], symbol="XAUUSD1", phase="open")
        self.assertEqual(result["status"], "unavailable")

    def test_observation_clock_failure_does_not_swallow_trading_failure(self):
        with patch("trading.cycle_quality.time", SimpleNamespace(monotonic=Mock(side_effect=RuntimeError("no clock")), time=time.time)):
            with patch.object(self.f.broker, "cycle_snapshot", side_effect=TradingError("account check failed")), \
                 patch.object(self.f.broker, "submit") as submit, self.assertRaisesRegex(TradingError, "account check failed"):
                book = self.f.market.book("XAUUSD1")
                plan = CyclePlan("open", "XAUUSD1", dec(2), 2, dec(2) * book.ask, dec(2) * book.bid, dec("0.02"))
                snapshot = PaperBroker.cycle_snapshot(self.f.broker, ["XAUUSD1"])
                self.executor.start(self.f.account, snapshot, plan, self.progress)
            submit.assert_not_called()
            self.start()
        self.assertEqual(self.quality()["actual"]["status"], "filled")
        self.assertTrue(all(value is None for value in self.quality()["timing"]["pre_submit"].values()))

    def test_nonoverlapping_stage_timings_and_remainder_are_persisted(self):
        final = self.final_observation()
        final["checked_monotonic"] = 100.239
        ticks = [100.200, 100.220, 100.230, 100.240, 100.250, 100.280, 100.400, 100.450]
        timer = SimpleNamespace(monotonic=Mock(side_effect=ticks), time=time.time)
        trigger = {"source": "bbo", "received_at": time.time(), "received_monotonic": 100,
                   "pre_submit": {"queue_ms": 30, "initial_account_ms": 80, "planning_ms": 40}}
        with patch("trading.cycle_quality.time", timer):
            self.start(trigger=trigger, before_submit=lambda snapshot: final)
        parts = self.quality()["timing"]["pre_submit"]
        expected = {"queue_ms": 30, "initial_account_ms": 80, "planning_ms": 40,
                    "final_account_ms": 20, "final_check_ms": 10, "persist_ms": 30, "other_ms": 190}
        for key, value in expected.items():
            self.assertAlmostEqual(parts[key], value)
        self.assertAlmostEqual(self.quality()["timing"]["trigger_to_request_ms"], 400)
        self.assertEqual(self.executor.last_completed_intent["execution_quality"], self.quality())
        with self.f.store.connect() as db:
            stored = json.loads(db.execute("SELECT data FROM intents WHERE id=?", (self.quality()["intent_id"],)).fetchone()[0])
        self.assertEqual(stored["execution_quality"]["timing"]["pre_submit"], parts)

    def test_missing_or_overlapping_measurements_leave_remainder_unknown(self):
        normal = {"queue_ms": 10, "initial_account_ms": 20, "planning_ms": 30,
                  "final_account_ms": 10, "final_check_ms": 10, "persist_ms": 20, "other_ms": 123}
        for change, expected in (({"queue_ms": None}, None), ({"persist_ms": 50}, None), ({}, 0)):
            with self.subTest(change=change):
                quality = {"timing": {"pre_submit": {**normal, **change}}}
                timer = SimpleNamespace(monotonic=Mock(side_effect=[100.1, 100.15]), time=time.time)
                with patch("trading.cycle_quality.time", timer):
                    ObservedBroker(SimpleNamespace(submit=lambda orders: []), quality, 100).submit([])
                self.assertEqual(quality["timing"]["pre_submit"]["other_ms"], expected)


if __name__ == "__main__":
    unittest.main()
