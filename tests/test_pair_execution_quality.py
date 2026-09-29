"""Offline paired telemetry regressions: persistent scope, original calls, no retries."""
from copy import deepcopy
import json
import threading
from unittest import TestCase
from unittest.mock import patch

from tests import test_pair_trading as fixtures
from trading import pair_quality
from trading.exchange import AmbiguousOrder, ExchangeError, RequestNotSent
from trading.store import Store


class PairExecutionQualityTests(TestCase):
    def setUp(self):
        self.fixture = fixtures.PairTradingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store

    def latest(self, reader=None):
        return pair_quality.read(reader or self.store, "gold", self.store.get("pair_runtime:gold", {}))

    def batch(self, state):
        return self.store.get("pair_batch:" + state["last_batch"]["id"])

    def test_parallel_open_close_persist_under_pair_and_survive_restart(self):
        f = self.fixture
        barrier = threading.Barrier(2, timeout=3)
        originals = {key: broker.submit for key, broker in f.brokers.items()}
        def send(key, rows):
            barrier.wait()
            return originals[key](rows)
        with patch.object(f.brokers["long"], "submit", side_effect=lambda rows: send("long", rows)) as long, \
                patch.object(f.brokers["short"], "submit", side_effect=lambda rows: send("short", rows)) as short:
            state = f.tick()
        self.assertEqual(state["phase"], "holding")
        self.assertEqual((long.call_count, short.call_count), (1, 1))
        opened = self.latest()["execution_quality"]
        self.assertEqual((opened["scope"], opened["pair_id"], opened["actual"]["status"]), ("pair", "gold", "filled"))
        self.assertEqual(opened["final_estimate"]["quantity"], opened["actual"]["quantity"])
        self.assertEqual(opened["final_estimate"]["status"], "available")
        timing = opened["timing"]
        self.assertEqual(timing["request_status"], "returned")
        for key in ("long", "short"):
            self.assertEqual(timing["legs"][key]["account_id"], key)
            self.assertGreaterEqual(timing["request_to_response_ms"], timing["legs"][key]["request_to_response_ms"])
        f.expire()
        state = f.tick()
        self.assertEqual(state["progress"]["completed_cycles"], 1)
        f.assert_flat()
        result = self.latest(Store(self.store.path))
        closed = result["execution_quality"]
        self.assertEqual((closed["phase"], closed["actual"]["status"]), ("close", "filled"))
        legs = self.batch(state)["legs"]
        self.assertEqual(closed["actual"]["buy_vwap"], legs[1]["receipt"]["avgPrice"])
        self.assertEqual(closed["actual"]["sell_vwap"], legs[0]["receipt"]["avgPrice"])
        self.assertEqual({row["phase"]: row["count"] for row in result["execution_quality_history"]["groups"]},
                         {"open": 1, "close": 1})
        for key in ("gold", "long", "short", "pair:other"):
            self.assertEqual(self.store.cycle_execution_quality_history(key)["groups"], [])
        with self.store.read_snapshot() as reader:
            pair = f.engine.pairs.states(reader)[0]
        self.assertEqual(pair["state"]["execution_quality"], closed)
        self.assertNotIn("identities", json.dumps(closed))

    def test_envelope_is_parallel_span_not_sum(self):
        batch = {"execution_quality": {"timing": {}}, "legs": [
            {"key": "long", "execution_timing": {"request_status": "returned", "request_started_at": 100,
                "response_received_at": 100.1, "request_to_response_ms": 100}},
            {"key": "short", "execution_timing": {"request_status": "returned", "request_started_at": 100.02,
                "response_received_at": 100.17, "request_to_response_ms": 150}}]}
        pair_quality.complete(batch, {"long": (10, 10.1), "short": (10.02, 10.17)})
        self.assertAlmostEqual(batch["execution_quality"]["timing"]["request_to_response_ms"], 170)

    def test_timeout_recovery_preserves_original_failed_clock_without_resubmission(self):
        broker = self.fixture.brokers["long"]
        original = broker.submit
        def timeout(rows):
            original(rows)
            raise AmbiguousOrder("lost response")
        with patch.object(broker, "submit", side_effect=timeout) as send:
            state = self.fixture.tick()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(state["phase"], "holding")
        result = self.latest()
        quality = result["execution_quality"]
        self.assertEqual(quality["actual"]["status"], "filled")
        self.assertEqual(quality["timing"]["request_status"], "failed")
        self.assertIsNone(quality["timing"]["request_to_response_ms"])
        group = result["execution_quality_history"]["groups"][0]
        self.assertEqual((group["count"], group["comparable_count"], group["best"]), (1, 0, None))
        batch = self.batch(state)
        batch["execution_quality"]["timing"].update(request_status="returned", request_to_response_ms=1)
        self.store.put("pair_batch:" + batch["id"], batch)
        pair_quality.record(self.store, "gold", batch)
        self.assertEqual(self.latest()["execution_quality"]["timing"], quality["timing"])

    def test_repair_does_not_rank_or_turn_partial_original_into_filled(self):
        with patch.object(self.fixture.brokers["short"], "submit", side_effect=ExchangeError("rejected", code=-2019)):
            state = self.fixture.tick()
        self.assertEqual(state["phase"], "repairing")
        self.fixture.tick()
        self.fixture.assert_flat()
        quality = self.latest()["execution_quality"]
        self.assertEqual(quality["actual"]["status"], "partial")
        self.assertTrue(quality["actual"]["repairs_present"])
        self.assertEqual(self.latest()["execution_quality_history"]["groups"][0]["count"], 1)
        self.assertEqual(len(self.fixture.brokers["long"].state["orders"]), 2)

    def test_local_not_sent_never_enters_history(self):
        with patch.object(self.fixture.brokers["long"], "submit", side_effect=RequestNotSent("local gate")), \
                patch.object(self.fixture.brokers["short"], "submit", side_effect=RequestNotSent("local gate")):
            self.fixture.tick()
        result = self.latest()
        self.assertEqual(result["execution_quality_history"]["groups"], [])
        self.assertEqual(result["execution_quality"]["timing"]["request_status"], "not_sent")
        self.fixture.assert_flat()

    def test_missing_clocks_and_observation_write_failure_do_not_change_trades(self):
        with self.store.connection_scope(), patch("trading.pair_quality.quality.clock_tick", return_value=None), \
                patch("trading.pair_quality.history.record", side_effect=RuntimeError("display write failed")):
            state = self.fixture.tick()
        self.assertEqual(state["phase"], "holding")
        self.assertIsNone(state["pending"])
        self.assertTrue(self.batch(state)["completed"])
        for broker in self.fixture.brokers.values():
            self.assertEqual(len(broker.state["orders"]), 1)
        self.assertEqual(self.latest()["execution_quality"]["actual"]["status"], "filled")
        self.assertEqual(self.latest()["execution_quality_history"]["groups"], [])

    def test_legacy_receipts_restore_prices_without_fabricating_timings_or_queries(self):
        state = self.fixture.tick()
        batch = self.batch(state)
        del batch["execution_quality"]
        for leg in batch["legs"]:
            del leg["execution_timing"]
        self.store.put("pair_batch:" + batch["id"], batch)
        with self.store.connect() as db:
            db.execute("DELETE FROM cycle_quality_history")
            db.execute("DELETE FROM kv WHERE key='pair_execution:gold'")
        with patch.object(self.fixture.brokers["long"], "query", side_effect=AssertionError("no exchange reads")), \
                patch.object(self.fixture.brokers["short"], "query", side_effect=AssertionError("no exchange reads")):
            result = self.latest()
        quality = result["execution_quality"]
        self.assertEqual(quality["actual"]["status"], "filled")
        self.assertEqual(quality["timing"]["request_status"], "unknown")
        self.assertIsNone(quality["timing"]["request_to_response_ms"])
        self.assertEqual(quality["final_estimate"]["status"], "unavailable")
        self.assertEqual(result["execution_quality_history"]["groups"], [])

    def test_deduplicated_history_is_bounded_and_late_updates_cannot_reenter_window(self):
        batch = self.batch(self.fixture.tick())
        for i in range(101):
            row = deepcopy(batch)
            row["id"] = f"quality-{i:03}"
            q = row["execution_quality"]
            q["intent_id"] = "pair:" + row["id"]
            q["created_at"] = row["created_at"] = 1000 + i
            q["timing"].update(request_started_at=1000 + i, response_received_at=1000.01 + i,
                               request_to_response_ms=10 + i)
            self.store.put("pair_batch:" + row["id"], row)
            pair_quality.record(self.store, "gold", row)
            pair_quality.record(self.store, "gold", row)
        old = self.store.get("pair_batch:quality-000")
        pair_quality.record(self.store, "gold", old)
        pair_quality.record(self.store, "other", old)
        group = self.store.cycle_execution_quality_history("pair:gold")["groups"][0]
        # The real, newer fixture request also belongs in the 100-entry window.
        self.assertEqual(group["count"], 100)
        self.assertEqual(self.store.cycle_execution_quality_history("pair:other")["groups"], [])
        with self.store.connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM cycle_quality_history WHERE intent_id='pair:quality-000'").fetchone())
