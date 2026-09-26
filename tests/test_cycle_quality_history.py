"""The rolling request window is durable, bounded and independent of trading."""
import copy
import json
import unittest

from tests.helpers import account
from tests import test_cycle_execution_quality
from trading.cycle_quality_history import response_ms
from trading.engine import Engine
from trading.store import Store


class CycleQualityHistoryTests(unittest.TestCase):
    def setUp(self):
        self.case = test_cycle_execution_quality.CycleExecutionQualityTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.start()
        self.store = self.case.f.store
        self.template = copy.deepcopy(self.case.executor.last_completed_intent)
        with self.store.connect() as db:
            db.execute("DELETE FROM cycle_quality_history")
            db.execute("DELETE FROM intents")
            db.execute("DELETE FROM kv WHERE key LIKE 'cycle_execution:%'")

    def add(self, index, ms=100, *, account_id="test", symbol="XAUUSD1", phase="open", status="returned", persist=True):
        intent = copy.deepcopy(self.template)
        intent.update(id=f"{account_id}-{symbol}-{phase}-{index:04}", account_id=account_id,
                      symbol=symbol, phase=phase, created_at=1700000000 + index)
        for order in intent["orders"]:
            order["symbol"] = symbol
        for receipt in intent["receipts"].values():
            receipt["symbol"] = symbol
        quality = intent["execution_quality"]
        quality.update(intent_id=intent["id"], symbol=symbol, phase=phase, created_at=intent["created_at"],
                       updated_at=intent["created_at"] + 1)
        quality["timing"].update(request_started_at=intent["created_at"], response_received_at=intent["created_at"] + 1,
                                 request_status=status, request_to_response_ms=ms)
        if status != "returned":
            quality["timing"].update(response_received_at=None, request_to_response_ms=None)
        self.store.save_intent(intent)
        if persist:
            self.store.record_cycle_execution_quality(intent)
        return intent

    def groups(self, account_id="test"):
        return self.store.cycle_execution_quality_history(account_id)["groups"]

    def test_rolling_100_deduplicates_and_evicted_late_updates_stay_evicted(self):
        oldest = self.add(0, 0)
        for index in range(1, 101):
            self.add(index, index)
        self.assertEqual(self.groups()[0]["count"], 100)
        self.assertEqual(self.groups()[0]["best"]["intent_id"], "test-XAUUSD1-open-0001")
        self.assertEqual(self.groups()[0]["worst"]["timing"]["request_to_response_ms"], 100)
        oldest["execution_quality"]["updated_at"] += 100000
        for _ in range(3):
            self.store.record_cycle_execution_quality(oldest)
        self.assertEqual(self.groups()[0]["count"], 100)
        self.assertEqual(self.groups()[0]["best"]["intent_id"], "test-XAUUSD1-open-0001")
        self.assertEqual(self.store.get("cycle_execution:test")["intent_id"], "test-XAUUSD1-open-0100")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM cycle_quality_history").fetchone()[0], 100)

    def test_account_symbol_phase_isolation_and_restart_keep_full_details(self):
        self.store.save_account(account("other"))
        for account_id, symbol, phase, ms in (("test", "XAUUSD1", "open", 10), ("test", "XAUUSD1", "close", 20),
                                            ("test", "CLUSD1", "open", 30), ("other", "XAUUSD1", "open", 40)):
            self.add(1, ms, account_id=account_id, symbol=symbol, phase=phase)
        before = self.groups()
        self.assertEqual(len(before), 3)
        self.assertEqual(self.groups("other")[0]["best"]["timing"]["request_to_response_ms"], 40)
        self.assertEqual(self.groups("absent"), [])
        self.store = Store(self.store.path)
        self.assertEqual(self.groups(), before)
        for group in before:
            self.assertEqual(group["count"], 1)
            self.assertEqual(group["best"], group["worst"])
            self.assertEqual(group["best"]["final_estimate"], self.template["execution_quality"]["final_estimate"])
            self.assertEqual(group["best"]["actual"]["buy_vwap"], "4412.02")

    def test_missing_response_counts_but_not_unsent_and_zero_and_ties_rank(self):
        self.add(1, 0)
        latest = self.add(2, 0)
        self.add(3, status="failed")
        self.add(4, status="not_sent")
        self.add(5, status="unknown")
        self.add(6, None)
        group = self.groups()[0]
        self.assertEqual((group["count"], group["comparable_count"]), (4, 2))
        self.assertEqual(group["best"]["intent_id"], latest["id"])
        self.assertEqual(group["best"], group["worst"])
        for value in (None, "0", True, -1, float("nan"), float("inf")):
            quality = copy.deepcopy(latest["execution_quality"])
            quality["timing"]["request_to_response_ms"] = value
            self.assertIsNone(response_ms(quality))
        latest["execution_quality"]["timing"]["response_received_at"] = 0
        self.assertIsNone(response_ms(latest["execution_quality"]))

    def test_rejection_response_is_ranked_by_latency_and_keeps_rejected_status(self):
        intent = self.add(1, 1, persist=False)
        for receipt in intent["receipts"].values():
            receipt.update(status="REJECTED", executedQty="0", avgPrice="0")
        self.store.save_intent(intent)
        self.store.record_cycle_execution_quality(intent)
        self.assertEqual(self.groups()[0]["best"]["actual"]["status"], "rejected")
        self.assertEqual(self.groups()[0]["comparable_count"], 1)

    def test_old_reconciliation_updates_fills_without_replacing_request_or_latest(self):
        old = self.add(1, 10, persist=False)
        receipts = copy.deepcopy(old["receipts"])
        old["receipts"] = {}
        self.store.save_intent(old)
        self.store.record_cycle_execution_quality(old)
        self.assertFalse(self.groups()[0]["best"]["actual"]["confirmed"])
        latest = self.add(2, 20)
        old["receipts"] = receipts
        self.store.save_intent(old)
        stale = copy.deepcopy(old)
        stale["receipts"] = {}
        stale["execution_quality"]["timing"]["request_to_response_ms"] = 9999
        stale["execution_quality"]["trigger_estimate"] = None
        self.store.record_cycle_execution_quality(stale)
        group = self.groups()[0]
        self.assertEqual(group["count"], 2)
        self.assertTrue(group["best"]["actual"]["confirmed"])
        self.assertEqual(group["best"]["timing"]["request_to_response_ms"], 10)
        self.assertEqual(group["best"]["trigger_estimate"], old["execution_quality"]["trigger_estimate"])
        self.assertEqual(self.store.get("cycle_execution:test")["intent_id"], latest["id"])

    def test_upgrade_backfills_once_and_does_not_invent_missing_timing(self):
        for index in range(105):
            self.add(index, index, persist=False)
        self.add(106, status="unknown", persist=False)
        with self.store.connect() as db:
            db.execute("DROP TABLE cycle_quality_history")
        self.store = Store(self.store.path)
        group = self.groups()[0]
        self.assertEqual((group["count"], group["comparable_count"]), (100, 100))
        self.assertEqual(group["best"]["timing"]["request_to_response_ms"], 5)
        self.add(107, 1, persist=False)
        self.store = Store(self.store.path)
        self.assertEqual(self.groups()[0], group)

    def test_compact_state_exposes_only_selected_account_extremes(self):
        self.store.save_account(account("other"))
        for index in range(8):
            self.add(index, index)
        self.add(1, 200, account_id="other")
        engine = Engine(self.store, market=self.case.f.market)
        self.addCleanup(engine.dashboard_reports.close)
        state = engine.state(compact=True, history_account="test")
        accounts = {item["id"]: item for item in state["accounts"]}
        history = accounts["test"]["cycle_state"]["execution_quality_history"]
        self.assertEqual(history["limit"], 100)
        self.assertEqual(history["groups"][0]["count"], 8)
        self.assertEqual(history["groups"][0]["worst"]["timing"]["request_to_response_ms"], 7)
        self.assertIsNone(accounts["other"]["cycle_state"]["execution_quality_history"])
        self.assertNotIn('test-XAUUSD1-open-0004', json.dumps(history))
        with self.store.read_snapshot() as reader:
            self.assertEqual(reader.cycle_execution_quality_history("test"), history)


if __name__ == "__main__":
    unittest.main()
