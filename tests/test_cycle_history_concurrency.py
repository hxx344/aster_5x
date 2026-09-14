"""Late history writes cannot replace completed execution or newer accounting."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import threading
import unittest
from unittest.mock import patch

from trading.cycle_execution import CycleExecutor
from trading.models import TradingError
from trading.paper import PaperBroker
from tests import test_cycle_execution as execution_cases


class CycleHistoryConcurrencyTests(unittest.TestCase):
    setUp = execution_cases.CycleExecutionTests.setUp
    plan = execution_cases.CycleExecutionTests.plan
    progress_now = execution_cases.CycleExecutionTests.progress_now
    open = execution_cases.CycleExecutionTests.open

    def completed_without_history(self):
        with patch.object(self.executor, "sync_volume", return_value=False):
            self.open()
        return self.f.store.cycle_volume_backlog("test")[0]

    def saved(self, intent_id):
        with self.f.store.connect() as db:
            row = db.execute("SELECT status,data FROM intents WHERE id=?", (intent_id,)).fetchone()
        return row["status"], json.loads(row["data"])

    def test_late_history_failure_cannot_undo_new_accounting_or_final_quality(self):
        stale = self.completed_without_history()
        history_broker = PaperBroker("test", self.f.market, self.f.store)
        history = CycleExecutor(self.f.store, history_broker, self.f.market)
        entered, release = threading.Event(), threading.Event()
        def slow_failure(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("foreground accounting did not finish")
            raise TradingError("old history request failed")
        with ThreadPoolExecutor(max_workers=1) as pool, \
             patch.object(history_broker, "cycle_trades", side_effect=slow_failure):
            future = pool.submit(history.sync_volume, self.f.account, copy.deepcopy(stale))
            try:
                self.assertTrue(entered.wait(5))
                current = self.saved(stale["id"])[1]
                self.assertTrue(self.executor.sync_volume(self.f.account, current))
                current["execution_quality"]["timing"]["request_to_response_ms"] = 123.5
                self.executor._observe_quality(current)
                expected = self.saved(stale["id"])[1]
                volume = self.f.store.cycle_daily_volume("test")
            finally:
                release.set()
            self.assertTrue(future.result(timeout=5))
        status, saved = self.saved(stale["id"])
        self.assertEqual(status, "complete")
        for key in ("status", "orders", "repairs", "baseline", "progress", "completed_at", "execution_quality", "volume_receipts"):
            self.assertEqual(saved.get(key), expected.get(key), key)
        self.assertNotIn("volume_error", saved)
        self.assertTrue(saved["volume_synced"])
        self.assertEqual(len(saved["volume_receipts"]), 2)
        self.assertEqual(self.f.store.cycle_daily_volume("test"), volume)
        self.assertEqual(volume["trade_count"], 2)
        self.assertEqual(self.f.store.cycle_volume_backlog("test"), [])
        self.assertIsNone(self.f.store.intent("test"))

    def test_a_precompletion_copy_cannot_resurrect_or_replace_the_final_intent(self):
        with patch.object(self.executor, "reconcile", return_value="not yet reconciled"):
            self.open()
        stale = self.f.store.intent("test")
        with patch.object(self.executor, "sync_volume", return_value=False):
            self.executor.reconcile(self.f.account)
        expected = self.saved(stale["id"])[1]
        stale["baseline"]["LONG"] = "999"
        stale["execution_quality"] = {"stale": True}
        stale["volume_error"] = "old failure"
        self.assertFalse(self.f.store.save_cycle_volume_state(stale, {}))
        status, saved = self.saved(stale["id"])
        self.assertEqual(status, "complete")
        for key in ("status", "orders", "repairs", "baseline", "progress", "completed_at", "execution_quality"):
            self.assertEqual(saved.get(key), expected.get(key), key)
        self.assertIsNone(self.f.store.intent("test"))
        self.assertEqual(self.progress_now()["phase"], "holding")

    def test_concurrent_pagination_keeps_newer_cursor_and_all_observed_fills(self):
        intent = self.completed_without_history()
        cid = intent["orders"][0]["newClientOrderId"]
        query = {"identity": [str(intent["receipts"][cid]["orderId"]), "2", "XAUUSD1", "LONG", "BUY"],
                 "from_id": 10, "fills": {"1": {"trade_id": "1"}}}
        intent["volume_queries"] = {cid: query}
        self.f.store.save_intent(intent)
        baseline = {"volume_queries": copy.deepcopy(intent["volume_queries"]), "volume_receipts": {}}
        newer, stale = copy.deepcopy(intent), copy.deepcopy(intent)
        newer["volume_queries"][cid].update(from_id=20, fills={"1": {"trade_id": "1"}, "2": {"trade_id": "2"}})
        stale["volume_queries"][cid].update(from_id=15, fills={"1": {"trade_id": "1"}, "3": {"trade_id": "3"}})
        self.f.store.save_cycle_volume_state(newer, baseline)
        self.f.store.save_cycle_volume_state(stale, baseline)
        saved = self.saved(intent["id"])[1]
        self.assertEqual(saved["volume_queries"][cid]["from_id"], 20)
        self.assertEqual(set(saved["volume_queries"][cid]["fills"]), {"1", "2", "3"})
        self.assertEqual(saved["execution_quality"], intent["execution_quality"])

    def test_completed_receipt_enrichment_cannot_replace_existing_execution_values(self):
        intent = self.completed_without_history()
        cid = intent["orders"][0]["newClientOrderId"]
        expected = copy.deepcopy(intent["receipts"][cid])
        incoming = copy.deepcopy(intent)
        incoming["receipts"][cid].update(avgPrice="9999", query_detail="new optional metadata")
        self.f.store.save_cycle_volume_state(incoming, {})
        receipt = self.saved(intent["id"])[1]["receipts"][cid]
        self.assertEqual(receipt["avgPrice"], expected["avgPrice"])
        self.assertEqual(receipt["query_detail"], "new optional metadata")
        incoming["receipts"][cid]["orderId"] = "different-order"
        with self.assertRaisesRegex(TradingError, "订单编号.*冲突"):
            self.f.store.save_cycle_volume_state(incoming, {})


if __name__ == "__main__":
    unittest.main()
