"""HTTP status stays responsive while historical reporting or writes are busy."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import sqlite3
import threading
import time
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.helpers import Fixture, account
from trading.engine import Engine
from trading.report_cache import ReportCache
from trading.server import create_app
from trading.store import dumps


class DashboardReportTests(TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.engine = Engine(self.f.store, market=self.f.market)
        self.addCleanup(self.engine.dashboard_reports.close)

    def finish_report(self):
        self.engine.dashboard_reports.worker.join(timeout=5)
        self.assertFalse(self.engine.dashboard_reports.worker.is_alive())

    def test_authenticated_status_does_not_wait_or_queue_when_report_is_blocked(self):
        entered, release = threading.Event(), threading.Event()
        original = self.engine.dashboard_reports.load
        calls = []
        def blocked(a, now):
            calls.append(a["id"])
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError("test report was not released")
            return original(a, now)
        self.engine.dashboard_reports.load = blocked
        with patch.dict("os.environ", {"ASTER_DASHBOARD_PASSWORD": "test-only-report-password"}), \
             TestClient(create_app(self.engine, start_engine=False)) as client:
            client.headers["origin"] = "http://testserver"
            self.assertEqual(client.post("/api/login", json={"password": "test-only-report-password"}).status_code, 200)
            try:
                with ThreadPoolExecutor(max_workers=1) as requests:
                    first = requests.submit(client.get, "/api/state").result(timeout=1)
                    self.assertEqual(first.status_code, 200)
                    current = first.json()["accounts"][0]
                    self.assertEqual(current["cycle_state"]["report_status"]["status"], "loading")
                    self.assertNotIn("daily_volume", current["cycle_state"])
                    self.assertNotIn("cycle_trades", current)
                    self.assertTrue(entered.wait(timeout=1))
                    self.engine.view("test", snapshot={"timestamp": time.time(), "wallet": "54321"},
                                     status="running", reason="new current reading")
                    for _ in range(12):
                        response = requests.submit(client.get, "/api/state").result(timeout=1)
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.json()["accounts"][0]["snapshot"]["wallet"], "54321")
                    self.assertEqual(calls, ["test"])
                    self.assertIsNone(self.f.store.intent("test"))
            finally:
                release.set()
                self.finish_report()
            ready = client.get("/api/state").json()["accounts"][0]
            self.assertEqual(ready["cycle_state"]["report_status"]["status"], "ready")
            self.assertEqual(ready["cycle_state"]["daily_volume"]["cost"]["total_cost"], "0")
            self.assertEqual(ready["cycle_trades"], [])

    def test_status_reads_committed_state_without_waiting_for_store_writer(self):
        entered, release = threading.Event(), threading.Event()
        def writer():
            with self.f.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                modified = self.f.store.account("test")
                modified["name"] = "uncommitted"
                db.execute("UPDATE accounts SET data=? WHERE id='test'", (dumps(modified),))
                entered.set()
                release.wait(timeout=5)
        with ThreadPoolExecutor(max_workers=2) as pool:
            writing = pool.submit(writer)
            try:
                self.assertTrue(entered.wait(timeout=1))
                result = pool.submit(self.engine.state, background_reports=True).result(timeout=1)
                self.assertEqual(result["accounts"][0]["name"], self.f.account["name"])
            finally:
                release.set()
                writing.result(timeout=2)
        self.finish_report()
        self.assertEqual(self.engine.state(background_reports=True)["accounts"][0]["name"], "uncommitted")

    def test_report_snapshot_is_consistent_read_only_and_does_not_block_writes(self):
        with self.f.store.read_snapshot() as reader:
            before = reader.account("test")
            changed = {**before, "name": "committed later"}
            self.f.store.save_account(changed)
            self.assertEqual(reader.account("test"), before)
            self.assertEqual(self.f.store.account("test")["name"], "committed later")
            with self.assertRaises(sqlite3.OperationalError):
                reader.put("must-not-write", True)
        self.assertIsNone(self.f.store.get("must-not-write"))

    def test_background_totals_match_full_report_without_changing_execution_view(self):
        from tests.test_cycle_cost_engine import CycleCostStateTests
        case = CycleCostStateTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.addCleanup(case.engine.dashboard_reports.close)
        case.select(daily_volume_limit="1000", hold_seconds=1)
        case.engine.enable("test", True)
        case.run_until_holding()
        # Freeze only the report as-of time; executing the paper cycle stays real.
        now = time.time()
        with patch("trading.engine.time.time", return_value=now):
            expected = case.engine.state()["accounts"][0]
            before = deepcopy(case.engine.views)
            case.engine.state(background_reports=True)
            case.engine.dashboard_reports.worker.join(timeout=5)
            self.assertFalse(case.engine.dashboard_reports.worker.is_alive())
            actual = case.engine.state(background_reports=True)["accounts"][0]
        for key in ("daily_volume", "rolling_volume", "volume_by_symbol"):
            self.assertEqual(expected["cycle_state"][key], actual["cycle_state"][key])
        self.assertEqual(expected["cycle_trades"], actual["cycle_trades"])
        self.assertEqual(case.engine.views, before)

    def test_execution_reads_new_fills_even_while_dashboard_still_has_old_totals(self):
        from tests.test_cycle_volume import CycleVolumeTests
        from trading.cycle import DailyVolumeLimitError
        saved = self.f.store.account("test")
        saved["cycle"].update(enabled=True, daily_volume_limit="100")
        self.f.store.save_account(saved)
        self.engine.state(background_reports=True)
        self.finish_report()
        case = CycleVolumeTests()
        case.store = self.f.store
        intent = case.intent(account_id="test", quantity="1", filled="1", short_filled="1")
        self.f.store.record_cycle_fills(intent, [
            case.fill(intent, "buy", executed_at=time.time() - 2),
            case.fill(intent, "sell", position_side="SHORT", executed_at=time.time() - 1),
        ])
        self.f.store.mark_cycle_volume_synced(intent["id"])
        # The display still describes its earlier, empty ledger snapshot.
        displayed = self.engine.state(background_reports=True)["accounts"][0]["cycle_state"]
        self.assertEqual(displayed["daily_volume"]["volume"], "0")
        self.assertEqual(displayed["phase"], "waiting_open")
        with self.assertRaises(DailyVolumeLimitError):
            self.engine.cycle_open_allowances(saved)
        # Nor can an old exhausted display report rewrite current execution phase.
        self.engine.dashboard_reports.entries["test"]["data"]["volumes"]["XAUUSD1"]["daily_volume"].update(reached=True)
        self.assertEqual(self.engine.state(background_reports=True)["accounts"][0]["cycle_state"]["phase"], "waiting_open")


class ReportCacheTests(TestCase):
    def test_failure_keeps_last_result_and_timestamp_without_crossing_account_or_config(self):
        accounts = [account("test"), account("second")]
        cache = ReportCache(lambda a, now: {"owner": a["id"], "value": "123"})
        self.addCleanup(cache.close)
        cache.read(accounts, time.time())
        cache.worker.join(timeout=2)
        first = cache.read(accounts, time.time())
        stamp = first["test"][1]["as_of"]
        def fail_one(a, now):
            if a["id"] == "test":
                raise ValueError("not logged content")
            return {"owner": "second", "value": "456"}
        cache.load, cache.due = fail_one, 0
        with self.assertLogs("aster.trading", level="WARNING"):
            cache.read(accounts, time.time())
            cache.worker.join(timeout=2)
        later = cache.read(accounts, time.time())
        self.assertEqual(later["test"][0], first["test"][0])
        self.assertEqual(later["test"][1]["as_of"], stamp)
        self.assertEqual(later["test"][1]["status"], "stale")
        self.assertEqual(later["second"][0]["value"], "456")
        accounts[0]["cycle"] = {"symbol": "CLUSD1", "daily_volume_limit": "999"}
        self.assertIsNone(cache.read(accounts, time.time())["test"][0])
        self.assertEqual(cache.read(accounts, time.time() + 20)["second"][1]["status"], "stale")
        cache.close()
        cache.due = 0
        worker = cache.worker
        cache.read(accounts, time.time())
        self.assertIs(cache.worker, worker)
