"""Portal summaries stay small and never initiate exchange/history work."""
from copy import deepcopy
from datetime import datetime, timezone
import time
import threading
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.helpers import Fixture, account
from tests import test_pair_order_recovery as recovery_tests
from trading.engine import Engine
from trading.exchange import ExchangeError
from trading.hub_summary import hub_summary
from trading.server import create_app
from trading.models import TradingError
from trading.pair_execution import runtime_default
from trading.pairing import validate_pair
from trading.pair_recovery import require_archived_orders_clear
from trading.store import _StoreSnapshot


class HubSummaryTests(TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.engine = Engine(self.f.store, market=self.f.market)
        self.addCleanup(self.engine.dashboard_reports.close)
        self.engine.ready, self.engine.error = True, None
        self.now = time.time()

    def live(self, aid="test", *, enabled=True, margin="12.5", stamp=None, volume="200"):
        saved = account(aid, "live")
        saved["enabled"] = enabled
        self.f.store.save_account(saved)
        saved = self.f.store.account(aid)
        self.engine.view(aid, snapshot={"timestamp": self.now if stamp is None else stamp,
                                       "occupied_margin": margin})
        self.engine.dashboard_reports.entries[aid] = {
            "key": self.engine.dashboard_reports.key(saved), "as_of": self.now, "error": None,
            "data": {"volumes": {saved["cycle"]["symbol"]: {"daily_volume": {
                "volume": volume, "utc_date": datetime.fromtimestamp(self.now, timezone.utc).date().isoformat()}}}},
        }

    def metrics(self, result):
        return {item["key"]: item["value"] for item in result["data"]["metrics"]}

    def pair(self, *, enabled=True, mode="live"):
        for aid in ("test", "second"):
            self.f.store.save_account({**account(aid, mode), "enabled": False})
        self.f.store.save_pair(validate_pair({
            "id": "gold", "name": "黄金配对", "long_account_id": "test", "short_account_id": "second",
            "enabled": enabled, "ordinary": {"enabled": True},
        }), create=True)
        day = datetime.fromtimestamp(self.now, timezone.utc).date().isoformat()
        runtime = {**runtime_default(), "updated_at": self.now,
                   "snapshots": {"long": {"timestamp": self.now - 3, "occupied_margin": "12.5"},
                                 "short": {"timestamp": self.now - 8, "occupied_margin": "17.5"}},
                   "daily_volume": {day: {"long": "200", "short": "350"}}}
        self.f.store.put("pair_runtime:gold", runtime)
        return runtime

    def test_enabled_pair_counts_members_and_reads_only_published_runtime(self):
        self.pair()
        with patch.object(self.engine, "state", side_effect=AssertionError("full state")), \
             patch.object(self.engine.pairs, "states", side_effect=AssertionError("full pair state")), \
             patch.object(self.engine, "broker", side_effect=AssertionError("exchange")), \
             patch.object(self.engine.dashboard_reports, "read", side_effect=AssertionError("start reports")), \
             patch.object(self.engine, "_load_dashboard_report", side_effect=AssertionError("history")), \
             patch.object(self.f.market, "book", side_effect=AssertionError("market request")), \
             patch.object(self.f.store, "get", side_effect=AssertionError("outside snapshot")), \
             patch.object(self.f.store, "accounts", side_effect=AssertionError("outside snapshot")), \
             patch.object(self.f.store, "pairs", side_effect=AssertionError("outside snapshot")), \
             patch.object(self.f.store, "read_snapshot", wraps=self.f.store.read_snapshot) as read_snapshot, \
             patch("trading.pair_quality.read", side_effect=AssertionError("quality history")), \
             patch("trading.pair_cost.read", side_effect=AssertionError("cost history")):
            result = hub_summary(self.engine, now=self.now, diagnostics=True)
        read_snapshot.assert_called_once_with()
        self.assertEqual(self.metrics(result), {"accounts": 2, "live_accounts": 2,
                                              "occupied_margin": 30, "daily_volume": 550})
        expected = datetime.fromtimestamp(self.now - 8, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        self.assertEqual(result["data"]["updatedAt"], expected)
        self.assertEqual(result["data"]["health"]["state"], "online")
        self.assertIsNone(self.engine.dashboard_reports.worker)

    def test_diagnostics_are_opt_in_and_keep_legacy_values_and_health(self):
        self.pair()
        with patch.object(_StoreSnapshot, "intent", side_effect=AssertionError("opt-in only")):
            legacy = hub_summary(self.engine, now=self.now)
        self.assertNotIn("diagnostics", legacy["data"])
        self.assertEqual(hub_summary(self.engine, now=self.now, diagnostics=False), legacy)
        extended = hub_summary(self.engine, now=self.now, diagnostics=True)
        self.assertEqual(extended["data"].pop("diagnostics"), [])
        self.assertEqual(extended, legacy)

    def test_report_notice_and_snapshot_fault_leave_null_and_stale_values_unchanged(self):
        self.live()
        self.engine.dashboard_reports.entries.clear()
        result = hub_summary(self.engine, now=self.now, diagnostics=True)
        self.assertEqual([(row["id"], row["kind"]) for row in result["data"]["diagnostics"]],
                         [("account:test:report", "notice")])
        self.assertIsNone(self.metrics(result)["daily_volume"])
        self.assertEqual(result["data"]["health"]["state"], "partial")
        self.engine.views["test"]["snapshot"]["timestamp"] = self.now - 120
        result = hub_summary(self.engine, now=self.now, diagnostics=True)
        self.assertEqual([(row["id"], row["kind"]) for row in result["data"]["diagnostics"]],
                         [("account:test:snapshot", "fault"), ("account:test:report", "notice")])
        self.assertEqual(result["data"]["health"]["state"], "stale")
        legacy = hub_summary(self.engine, now=self.now)
        result["data"].pop("diagnostics")
        self.assertEqual(result, legacy)
        self.live()
        self.engine.dashboard_reports.entries["test"]["error"] = "读取失败，等待重试"
        result = hub_summary(self.engine, now=self.now, diagnostics=True)
        self.assertEqual([(row["id"], row["kind"]) for row in result["data"]["diagnostics"]],
                         [("account:test:report", "fault")])
        self.assertIsNone(self.metrics(result)["daily_volume"])
        self.assertEqual(result["data"]["health"]["state"], "partial")

    def test_pair_diagnostics_merge_both_members_and_keep_highest_kind(self):
        runtime = self.pair()
        runtime.update(phase="attention", attention="两侧仓位不一致，请人工核对")
        runtime["snapshots"]["short"] = {}
        self.f.store.put("pair_runtime:gold", runtime)
        result = hub_summary(self.engine, now=self.now, diagnostics=True)
        rows = result["data"]["diagnostics"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["id"], rows[0]["kind"]), ("pair:gold", "action"))
        for reason in ("两侧仓位不一致", "缺少账户快照", "今日成交量暂不可用"):
            self.assertIn(reason, rows[0]["message"])
        self.assertIsNone(result["data"]["updatedAt"])
        self.assertIsNone(self.metrics(result)["occupied_margin"])
        self.assertIsNone(self.metrics(result)["daily_volume"])

    def test_pair_automatic_recovery_is_fault_but_stats_only_delay_is_notice(self):
        runtime = self.pair()
        for changes, kind in (({"phase": "reconciling"}, "fault"),
                              ({"phase": "repairing"}, "fault"),
                              ({"daily_volume": None}, "notice"),
                              ({"updated_at": self.now - 120}, "notice"),
                              ({"phase": "attention", "reason": "需要人工核对"}, "action")):
            with self.subTest(changes=changes):
                self.f.store.put("pair_runtime:gold", {**runtime, **changes})
                result = hub_summary(self.engine, now=self.now, diagnostics=True)
                self.assertEqual([(row["id"], row["kind"]) for row in result["data"]["diagnostics"]],
                                 [("pair:gold", kind)])
                self.assertIsNone(self.metrics(result)["daily_volume"])
                self.assertEqual(result["data"]["health"]["state"], "partial")

    def test_paused_attention_remains_visible_without_counting_paused_accounts(self):
        runtime = self.pair(enabled=False)
        runtime.update(phase="attention", attention="账户设置需要人工核对")
        runtime["snapshots"] = {}
        self.f.store.put("pair_runtime:gold", runtime)
        legacy = hub_summary(self.engine, now=self.now)
        result = hub_summary(self.engine, now=self.now, diagnostics=True)
        rows = result["data"].pop("diagnostics")
        self.assertEqual([(row["id"], row["kind"]) for row in rows], [("pair:gold", "action")])
        self.assertNotIn("快照", rows[0]["message"])
        self.assertEqual(result, legacy)
        self.assertEqual(self.metrics(result)["accounts"], 0)
        self.assertIsNone(self.metrics(result)["occupied_margin"])
        runtime.update(phase="paused", attention=None, recovery_watch={"batches": []})
        self.f.store.put("pair_runtime:gold", runtime)
        self.assertEqual(hub_summary(self.engine, now=self.now, diagnostics=True)["data"]["diagnostics"], [])
        pair = self.f.store.pair("gold")
        self.f.store.save_pair({**pair, "pause_reason": "账户模式已变化，请确认"})
        rows = hub_summary(self.engine, now=self.now, diagnostics=True)["data"]["diagnostics"]
        self.assertEqual([(row["id"], row["kind"]) for row in rows], [("pair:gold", "action")])
        self.assertIn("账户模式已变化", rows[0]["message"])

    def test_disabled_single_pause_reason_and_attention_intent_are_action_without_metrics(self):
        self.live(enabled=False)
        legacy = hub_summary(self.engine, now=self.now)
        self.assertEqual(hub_summary(self.engine, now=self.now, diagnostics=True)["data"]["diagnostics"], [])
        saved = self.f.store.account("test")
        self.f.store.pause_account(saved, "仓位不一致，需要人工核对")
        self.f.store.save_intent({"id": "pending", "account_id": "test", "status": "attention",
                                  "kind": "cycle", "last_error": "未完成批次需要人工核对"})
        result = hub_summary(self.engine, now=self.now, diagnostics=True)
        rows = result["data"].pop("diagnostics")
        self.assertEqual([(row["id"], row["kind"]) for row in rows], [("account:test:state", "action")])
        self.assertIn("仓位不一致", rows[0]["message"])
        self.assertIn("未完成批次", rows[0]["message"])
        self.assertEqual(result, legacy)
        self.assertEqual(self.metrics(result)["accounts"], 0)
        self.assertIsNone(self.metrics(result)["occupied_margin"])

    def test_service_faults_merge_and_diagnostic_overflow_remains_visible(self):
        self.engine.ready, self.engine.error = False, "行情暂不可用"
        self.engine.shutdown.set()
        result = hub_summary(self.engine, now=self.now, diagnostics=True)
        self.assertEqual([(row["id"], row["kind"]) for row in result["data"]["diagnostics"]],
                         [("service:engine", "fault")])
        self.assertIn("交易服务已停止", result["data"]["diagnostics"][0]["message"])
        records = [self.f.store.account_defaults({**account(f"account-{index}", "live"), "name": "账户😀" * 35})
                   for index in range(70)]
        with patch.object(_StoreSnapshot, "accounts", return_value=records):
            rows = hub_summary(self.engine, now=self.now, diagnostics=True)["data"]["diagnostics"]
        self.assertEqual(len(rows), 64)
        self.assertEqual((rows[-1]["id"], rows[-1]["kind"]), ("summary:more", "fault"))
        self.assertTrue(all(len(row["message"].encode("utf-16-le")) // 2 <= 500 for row in rows))

    def test_pair_and_standalone_accounts_are_deduplicated_and_keep_separate_volume_sources(self):
        self.pair()
        self.live("standalone", margin="10", volume="25")
        self.live("test", enabled=False, margin="999", stamp=self.now - 60, volume="999")
        saved = self.f.store.accounts()
        # Even a stale account list retaining the old enabled flag must not
        # double count the member or use its independent strategy report.
        records = [{**row, "enabled": True} if row["id"] == "test" else row for row in saved]
        with patch.object(_StoreSnapshot, "accounts", return_value=records + records[:1]):
            result = hub_summary(self.engine, now=self.now)
        self.assertEqual(self.metrics(result), {"accounts": 3, "live_accounts": 3,
                                              "occupied_margin": 40, "daily_volume": 575})

    def test_paused_pairs_are_excluded_and_paper_members_only_count_as_enabled(self):
        self.pair(enabled=False)
        self.live("standalone", margin="10", volume="25")
        result = hub_summary(self.engine, now=self.now)
        self.assertEqual(self.metrics(result), {"accounts": 1, "live_accounts": 1,
                                              "occupied_margin": 10, "daily_volume": 25})
        pair = self.f.store.pair("gold")
        self.f.store.save_pair({**pair, "enabled": True})
        for aid in ("test", "second"):
            self.f.store.save_account({**account(aid), "enabled": False})
        result = hub_summary(self.engine, now=self.now)
        self.assertEqual(self.metrics(result), {"accounts": 3, "live_accounts": 1,
                                              "occupied_margin": 10, "daily_volume": 25})
        self.assertEqual(result["data"]["health"]["state"], "online")

    def test_pair_missing_or_old_side_never_borrows_runtime_freshness(self):
        runtime = self.pair()
        for snapshot, state, has_timestamp in (
            ({}, "partial", False),
            ({"occupied_margin": "17.5"}, "partial", False),
            ({"timestamp": self.now - 120, "occupied_margin": "17.5"}, "stale", True),
        ):
            with self.subTest(snapshot=snapshot):
                runtime["snapshots"]["short"] = snapshot
                self.f.store.put("pair_runtime:gold", runtime)
                result = hub_summary(self.engine, now=self.now)
                self.assertEqual(result["data"]["health"]["state"], state)
                self.assertEqual(result["data"]["updatedAt"] is not None, has_timestamp)
                self.assertEqual(self.metrics(result)["daily_volume"], 550)
                self.assertIn("second", result["data"]["health"]["message"])

    def test_pair_uses_newest_valid_snapshot_from_runtime_view_or_display(self):
        runtime = self.pair()
        self.engine.view("test", snapshot={"timestamp": self.now - 2, "occupied_margin": "20"})
        self.engine.display_snapshots["second"] = {"timestamp": self.now - 1, "occupied_margin": "25"}
        result = hub_summary(self.engine, now=self.now)
        self.assertEqual(self.metrics(result)["occupied_margin"], 45)
        expected = datetime.fromtimestamp(self.now - 2, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        self.assertEqual(result["data"]["updatedAt"], expected)
        # A corrupt future candidate must not hide another valid account read.
        runtime["snapshots"]["long"] = {"timestamp": self.now + 61, "occupied_margin": "999"}
        self.f.store.put("pair_runtime:gold", runtime)
        self.engine.display_snapshots["test"] = {"timestamp": "nan", "occupied_margin": "999"}
        self.assertEqual(hub_summary(self.engine, now=self.now), result)

    def test_pair_future_only_snapshot_is_unknown_but_clock_tolerance_is_preserved(self):
        runtime = self.pair()
        for ahead, state in ((61, "partial"), (60, "online")):
            with self.subTest(ahead=ahead):
                runtime["snapshots"]["short"]["timestamp"] = self.now + ahead
                self.f.store.put("pair_runtime:gold", runtime)
                result = hub_summary(self.engine, now=self.now)
                self.assertEqual(result["data"]["health"]["state"], state)
                self.assertEqual(result["data"]["updatedAt"] is None, ahead == 61)

    def test_fresh_reconciled_pair_without_today_fills_has_true_zero_volume(self):
        runtime = self.pair()
        today = datetime.fromtimestamp(self.now, timezone.utc).date().isoformat()
        yesterday = datetime.fromtimestamp(self.now - 86400, timezone.utc).date().isoformat()
        for daily in ({}, {yesterday: {"long": "900", "short": "900"}},
                      {today: {"long": "0", "short": "0"}}):
            with self.subTest(daily=daily):
                runtime["daily_volume"] = daily
                self.f.store.put("pair_runtime:gold", runtime)
                result = hub_summary(self.engine, now=self.now)
                self.assertEqual(self.metrics(result)["daily_volume"], 0)
                self.assertEqual(result["data"]["health"]["state"], "online")

    def test_pair_volume_rejects_unknown_pending_recovery_bad_and_expired_runtime(self):
        runtime = self.pair()
        today = datetime.fromtimestamp(self.now, timezone.utc).date().isoformat()
        for changes, reason in (
            ({"updated_at": None}, "缺少有效更新时间"),
            ({"updated_at": self.now + 1}, "缺少有效更新时间"),
            ({"updated_at": self.now - 120}, "配对成交统计已过期"),
            ({"pending": {"kind": "cycle"}}, "配对订单尚待核对"),
            ({"recovery_watch": {"batches": []}}, "配对订单尚待核对"),
            ({"phase": "attention"}, "配对订单尚待核对"),
            ({"attention": "仓位待核对"}, "配对订单尚待核对"),
            ({"phase": "reconciling"}, "配对订单尚待核对"),
            ({"phase": "repairing"}, "配对订单尚待核对"),
            ({"volume_unknown": True}, "存在未知成交"),
            ({"volume_unknown": False, "volume_unknown_until_utc": today}, "存在未知成交"),
            ({"volume_unknown": True, "volume_unknown_until_utc": "bad-date"}, "未知日期无效"),
            ({"volume_unknown_until_utc": "2026-99-99"}, "未知日期无效"),
            ({"daily_volume": None}, "配对今日成交量缺失或无效"),
            ({"daily_volume": {today: {"long": "200"}}}, "配对今日成交量缺失或无效"),
            ({"daily_volume": {today: {"long": "nan", "short": "0"}}}, "配对今日成交量缺失或无效"),
            ({"daily_volume": {today: {"long": "-1", "short": "0"}}}, "配对今日成交量缺失或无效"),
        ):
            with self.subTest(changes=changes):
                self.f.store.put("pair_runtime:gold", {**runtime, **changes})
                result = hub_summary(self.engine, now=self.now)
                self.assertIsNone(self.metrics(result)["daily_volume"])
                self.assertEqual(result["data"]["health"]["state"], "partial")
                self.assertIn(reason, result["data"]["health"]["message"])
        for value in (None, {}, [], "invalid"):
            with self.subTest(runtime=value):
                self.f.store.put("pair_runtime:gold", value)
                result = hub_summary(self.engine, now=self.now)
                self.assertIsNone(self.metrics(result)["daily_volume"])
                self.assertIn("配对运行记录缺失或无效", result["data"]["health"]["message"])

    def test_normal_pair_submissions_and_leverage_keep_confirmed_volume_online(self):
        runtime = self.pair()
        normal = [{"phase": "submitting", "pending": {
            "kind": kind, "phase": phase, "repairs": [],
            "legs": [{"key": "long", "dispatch": "sending", "receipt": receipt},
                     {"key": "short", "dispatch": "prepared", "receipt": None}],
        }} for kind in ("cycle", "ordinary") for phase in ("open", "close")
           for receipt in (None, {"status": "NEW"}, {"status": "FILLED"})]
        normal.extend({"phase": "leverage", "pending": {"kind": "leverage", "results": results}}
                      for results in ({}, {"long": {"response": {}}, "short": {"rejected": True}}))
        for changes in normal:
            for daily, expected in ((runtime["daily_volume"], 550), ({}, 0)):
                with self.subTest(changes=changes, volume=expected):
                    self.f.store.put("pair_runtime:gold", {**runtime, **changes, "daily_volume": daily})
                    result = hub_summary(self.engine, now=self.now)
                    self.assertEqual(self.metrics(result)["daily_volume"], expected)
                    self.assertEqual(result["data"]["health"]["state"], "online")
                    self.assertIn("配对仅统计已核对成交", result["data"]["metrics"][-1]["detail"])

    def test_pair_unknown_submission_is_partial_before_phase_is_reconciled(self):
        runtime = self.pair()
        for leg in ({"error": "订单结果未知"}, {"submit_error": "提交超时"},
                    {"submit_evidence": {"kind": "ambiguous"}},
                    {"receipt": {"status": "UNKNOWN"}}, {"receipt": {"status": []}}, "invalid"):
            with self.subTest(leg=leg):
                self.f.store.put("pair_runtime:gold", {**runtime, "phase": "submitting", "pending": {
                    "kind": "cycle", "phase": "open", "repairs": [],
                    "legs": [leg, {"receipt": None}],
                }})
                result = hub_summary(self.engine, now=self.now)
                self.assertIsNone(self.metrics(result)["daily_volume"])
                self.assertEqual(result["data"]["health"]["state"], "partial")
        self.f.store.put("pair_runtime:gold", {**runtime, "phase": "leverage", "pending": {
            "kind": "leverage", "results": {"long": {"unknown": True}},
        }})
        result = hub_summary(self.engine, now=self.now)
        self.assertIsNone(self.metrics(result)["daily_volume"])
        self.assertEqual(result["data"]["health"]["state"], "partial")

    def test_pair_unknown_volume_expires_at_utc_midnight_without_refreshing_snapshots(self):
        self.now = (int(self.now // 86400) + 1) * 86400 - 1
        runtime = self.pair()
        yesterday = datetime.fromtimestamp(self.now, timezone.utc).date().isoformat()
        runtime.update(volume_unknown=True, volume_unknown_until_utc=yesterday)
        self.f.store.put("pair_runtime:gold", runtime)
        self.assertIsNone(self.metrics(hub_summary(self.engine, now=self.now))["daily_volume"])
        result = hub_summary(self.engine, now=self.now + 2)
        self.assertEqual(self.metrics(result)["daily_volume"], 0)
        self.assertEqual(result["data"]["health"]["state"], "online")
        runtime["updated_at"] = self.now + 122
        self.f.store.put("pair_runtime:gold", runtime)
        result = hub_summary(self.engine, now=self.now + 122)
        self.assertEqual(self.metrics(result)["daily_volume"], 0)
        self.assertEqual(result["data"]["health"]["state"], "stale")
        expected = datetime.fromtimestamp(self.now - 8, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        self.assertEqual(result["data"]["updatedAt"], expected)

    def test_engine_produces_reports_even_without_pages_or_market_readiness(self):
        self.live()
        self.engine.dashboard_reports.entries.clear()
        self.engine.ready = False
        generated = threading.Event()
        original = self.engine.dashboard_reports.load
        def load(saved, now):
            value = original(saved, now)
            generated.set()
            return value
        self.engine.dashboard_reports.load = load
        with patch.object(self.engine, "state", side_effect=AssertionError("page read")), \
             patch.object(self.engine.market, "load_rules", side_effect=TradingError("rules unavailable")), \
             patch("trading.report_cache.REPORT_INTERVAL", .03):
            self.engine.start()
            try:
                self.assertTrue(generated.wait(3))
                self.engine.dashboard_reports.worker.join(timeout=2)
                first = self.engine.dashboard_reports.entries["test"]["as_of"]
                generated.clear()
                self.assertTrue(generated.wait(2))
                self.engine.dashboard_reports.worker.join(timeout=2)
                self.assertGreater(self.engine.dashboard_reports.entries["test"]["as_of"], first)
                result = hub_summary(self.engine)
                self.assertIsNotNone(self.metrics(result)["daily_volume"])
                self.assertNotIn("成交统计尚未生成", result["data"]["health"]["message"])
                self.assertIn("交易服务尚未就绪", result["data"]["health"]["message"])
            finally:
                self.engine.stop()
        self.assertFalse(self.engine.dashboard_reports.producer.is_alive())

    def test_uses_only_enabled_live_accounts_and_matches_units_without_heavy_reads(self):
        self.live()
        self.live("disabled", enabled=False, margin="999999", volume="999999")
        self.f.store.save_account(account("paper"))
        with patch.object(self.engine, "state", side_effect=AssertionError("full state")), \
             patch.object(self.engine.dashboard_reports, "read", side_effect=AssertionError("start reports")), \
             patch.object(self.engine, "_load_dashboard_report", side_effect=AssertionError("history")), \
             patch.object(self.f.market, "book", side_effect=AssertionError("market request")):
            result = hub_summary(self.engine, now=self.now)
        self.assertEqual(self.metrics(result), {"accounts": 2, "live_accounts": 1, "occupied_margin": 12.5, "daily_volume": 200})
        self.assertEqual(result["data"]["health"]["state"], "online")
        self.assertEqual(result["data"]["health"]["staleAfterSeconds"], 120)
        self.assertEqual([m["unit"] for m in result["data"]["metrics"]][-2:], ["USD1", "USD1"])
        self.assertIsNone(self.engine.dashboard_reports.worker)

    def test_missing_snapshot_and_report_are_null_partial_not_zero(self):
        self.live()
        self.engine.views.clear()
        self.engine.dashboard_reports.entries.clear()
        result = hub_summary(self.engine, now=self.now)
        self.assertEqual(result["data"]["health"]["state"], "partial")
        self.assertIsNone(result["data"]["updatedAt"])
        self.assertIsNone(self.metrics(result)["occupied_margin"])
        self.assertIsNone(self.metrics(result)["daily_volume"])
        self.assertIn("测试子账户（test）：缺少账户快照", result["data"]["health"]["message"])
        self.assertIn("测试子账户（test）：成交统计尚未生成", result["data"]["health"]["message"])

    def test_reports_explain_why_volume_is_unavailable(self):
        for changes, reason in (
            ({"as_of": self.now - 16}, "成交统计已过期（超过 15 秒）"),
            ({"as_of": None}, "成交统计缺少有效更新时间"),
            ({"as_of": self.now + 1}, "成交统计缺少有效更新时间"),
            ({"error": "成交统计暂不可用，正在重新读取"}, "成交统计读取失败：成交统计暂不可用，正在重新读取"),
            ({"key": ("different", "0")}, "账户配置已变更，成交统计等待重新生成"),
            ({"data": {}}, "成交统计尚未生成"),
        ):
            with self.subTest(reason=reason):
                self.live()
                self.engine.dashboard_reports.entries["test"].update(changes)
                result = hub_summary(self.engine, now=self.now)
                self.assertIn(f"测试子账户（test）：{reason}", result["data"]["health"]["message"])
                self.assertEqual(result["data"]["health"]["state"], "partial")
        self.live(volume=None)
        self.assertIn("今日成交量缺失或无效", hub_summary(self.engine, now=self.now)["data"]["health"]["message"])

    def test_service_and_account_reasons_survive_health_priority(self):
        self.live(margin=None)
        self.engine.views["test"]["snapshot"]["timestamp"] = None
        self.engine.ready, self.engine.error = False, "交易规则加载失败"
        result = hub_summary(self.engine, now=self.now)
        message = result["data"]["health"]["message"]
        for reason in ("交易服务异常：交易规则加载失败", "交易服务尚未就绪", "测试子账户（test）：快照缺少有效更新时间", "测试子账户（test）：保证金数据缺失或无效"):
            self.assertIn(reason, message)
        self.engine.shutdown.set()
        result = hub_summary(self.engine, now=self.now)
        self.assertEqual(result["data"]["health"]["state"], "offline")
        self.assertIn("交易服务已停止", result["data"]["health"]["message"])
        self.assertIn("保证金数据缺失或无效", result["data"]["health"]["message"])
        self.assertNotIn("已连接", result["data"]["health"]["message"])

    def test_many_accounts_fit_the_portals_utf16_message_limit_without_heavy_reads(self):
        for index in range(15):
            aid = f"account-{index}"
            self.live(aid, margin=None)
            saved = self.f.store.account(aid)
            saved["name"] = "账户😀" * 10
            self.f.store.save_account(saved)
        self.live("disabled", enabled=False, margin=None)
        self.engine.error = {"secret": "never-serialize"}
        with patch.object(self.engine, "state", side_effect=AssertionError("full state")), \
             patch.object(self.engine.dashboard_reports, "read", side_effect=AssertionError("start reports")):
            message = hub_summary(self.engine, now=self.now)["data"]["health"]["message"]
        self.assertLessEqual(len(message.encode("utf-16-le")) // 2, 500)
        self.assertIn("另有", message)
        self.assertIn("account-0", message)
        self.assertIn("保证金数据缺失或无效", message)
        self.assertNotIn("disabled", message)
        self.assertNotIn("never-serialize", message)
        self.assertIsNone(self.engine.dashboard_reports.worker)

    def test_oldest_published_snapshot_controls_staleness(self):
        self.live(stamp=self.now - 121)
        self.live("second", stamp=self.now - 10)
        result = hub_summary(self.engine, now=self.now)
        expected = datetime.fromtimestamp(self.now - 121, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        self.assertEqual(result["data"]["updatedAt"], expected)
        self.assertEqual(result["data"]["health"]["state"], "stale")
        self.assertIn("测试子账户（test）：账户快照已过期", result["data"]["health"]["message"])
        self.engine.display_snapshots["test"] = {"timestamp": self.now, "occupied_margin": "20"}
        result = hub_summary(self.engine, now=self.now)
        self.assertEqual(result["data"]["health"]["state"], "online")
        self.assertEqual(self.metrics(result)["occupied_margin"], 32.5)

    def test_utc_rollover_expired_failed_and_config_changed_reports_are_not_ready(self):
        self.live()
        for changes in ({"as_of": self.now - 16}, {"error": "unavailable"}, {"key": ("different", "0")}):
            with self.subTest(changes=changes):
                self.live()
                self.engine.dashboard_reports.entries["test"].update(changes)
                result = hub_summary(self.engine, now=self.now)
                self.assertIsNone(self.metrics(result)["daily_volume"])
                self.assertEqual(result["data"]["health"]["state"], "partial")
        self.live()
        next_midnight = (int(self.now // 86400) + 1) * 86400
        result = hub_summary(self.engine, now=next_midnight)
        self.assertIsNone(self.metrics(result)["daily_volume"])

    def test_demo_no_live_accounts_does_not_claim_zero_private_balances(self):
        self.engine.demo = True
        result = hub_summary(self.engine, now=self.now)
        self.assertEqual(self.metrics(result)["live_accounts"], 0)
        self.assertIsNone(self.metrics(result)["occupied_margin"])
        self.assertIsNone(self.metrics(result)["daily_volume"])
        self.assertIn("演示", result["data"]["health"]["message"])
        self.engine.shutdown.set()
        self.assertEqual(hub_summary(self.engine)["data"]["health"]["state"], "offline")

    def test_previous_utc_day_is_rejected_even_with_a_two_second_old_report(self):
        self.now = (int(self.now // 86400) + 1) * 86400 - 1
        self.live()
        result = hub_summary(self.engine, now=self.now + 2)
        self.assertIsNone(self.metrics(result)["daily_volume"])
        self.assertEqual(result["data"]["health"]["state"], "partial")

        self.assertIn("成交统计缺少当日 UTC 数据", result["data"]["health"]["message"])

    def test_missing_one_account_timestamp_does_not_borrow_another_accounts_freshness(self):
        self.live()
        self.live("second")
        self.engine.views["second"]["snapshot"].pop("timestamp")
        result = hub_summary(self.engine, now=self.now)
        self.assertIsNone(result["data"]["updatedAt"])
        self.assertEqual(result["data"]["health"]["state"], "partial")

    def test_endpoint_shares_authentication_and_no_store_with_state(self):
        self.live()
        with patch.dict("os.environ", {"ASTER_DASHBOARD_PASSWORD": "test-only-summary-password"}), \
             TestClient(create_app(self.engine, start_engine=False)) as client:
            self.assertEqual(client.get("/api/hub/summary?schemaVersion=2").status_code, 401)
            client.headers["origin"] = "http://testserver"
            self.assertEqual(client.post("/api/login", json={"password": "test-only-summary-password"}).status_code, 200)
            with patch.object(self.engine, "state", side_effect=AssertionError("full state")):
                result = client.get("/api/hub/summary?schemaVersion=2")
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["schemaVersion"], 2)
            self.assertNotIn("diagnostics", result.json()["data"])
            self.assertEqual(result.headers["cache-control"], "no-store")
            extended = client.get("/api/hub/summary?schemaVersion=2&diagnostics=1")
            self.assertEqual(extended.status_code, 200)
            self.assertEqual(extended.headers["cache-control"], "no-store")
            self.assertEqual(extended.json()["data"]["diagnostics"], [])
            self.assertNotIn("diagnostics", client.get("/api/hub/summary?schemaVersion=2&diagnostics=0").json()["data"])
            self.assertEqual(client.get("/api/hub/summary?schemaVersion=3").status_code, 422)
            client.post("/api/logout")
            self.assertEqual(client.get("/api/hub/summary?schemaVersion=2").status_code, 401)
            self.assertEqual(client.get("/api/hub/summary?schemaVersion=2&diagnostics=1").status_code, 401)


class HubWatchDiagnosticTests(TestCase):
    """Observe actual existing guards through offline GET fixtures, never trade."""
    tearDown = recovery_tests.PairOrderRecoveryLiveReadTests.tearDown
    seed = recovery_tests.PairOrderRecoveryTests.seed
    state = recovery_tests.PairOrderRecoveryTests.state
    preview = recovery_tests.PairOrderRecoveryTests.preview
    confirm = recovery_tests.PairOrderRecoveryTests.confirm
    broker = recovery_tests.PairOrderRecoveryTests.broker
    no_writes = recovery_tests.PairOrderRecoveryTests.no_writes

    def setUp(self):
        recovery_tests.PairOrderRecoveryLiveReadTests.setUp(self)
        self.addCleanup(self.engine.dashboard_reports.close)
        self.engine.ready, self.engine.error = True, None

    def activate_watch(self):
        self.seed()
        with self.no_writes():
            self.confirm(self.preview()["token"])
            before = self.f.store.pair("gold")["revision"]
            self.engine.pairs.enable("gold", True)
        self.assertGreater(self.f.store.pair("gold")["revision"], before)

    def summary(self):
        before = deepcopy(self.state())
        calls = {aid: deepcopy(broker.api.calls) for aid, broker in self.live.items()}
        with self.no_writes(), patch.object(self.engine, "state", side_effect=AssertionError("full state")), \
             patch.object(self.engine.pairs, "states", side_effect=AssertionError("pair history")), \
             patch.object(self.engine, "broker", side_effect=AssertionError("new account read")), \
             patch.object(self.engine.dashboard_reports, "read", side_effect=AssertionError("report work")), \
             patch("trading.pair_recovery.require_archived_orders_clear", side_effect=AssertionError("new guard check")):
            result = hub_summary(self.engine, diagnostics=True)
        self.assertEqual(self.state(), before)
        self.assertEqual({aid: broker.api.calls for aid, broker in self.live.items()}, calls)
        return result

    def assert_kind(self, kind):
        result = self.summary()
        self.assertEqual([(row["id"], row["kind"]) for row in result["data"]["diagnostics"]], [("pair:gold", kind)])
        self.assertIsNone(next(row["value"] for row in result["data"]["metrics"] if row["key"] == "daily_volume"))
        return result

    def test_successful_enable_preserves_healthy_watch_notice_across_revision_and_time(self):
        self.activate_watch()
        self.assert_kind("notice")
        self.engine.pair_watch_diagnostics["gold"]["checked_at"] -= 3600
        self.assert_kind("notice")

    def test_failed_watch_check_is_fault_and_observed_new_activity_is_action(self):
        self.activate_watch()
        original = deepcopy(self.state())
        pair = self.f.store.pair("gold")
        with self.no_writes(), patch.object(self.broker(), "query", side_effect=ExchangeError("offline")):
            with self.assertRaises(TradingError):
                require_archived_orders_clear(self.engine, pair)
        self.assert_kind("fault")
        leg = original["recovery_watch"]["batches"][0]["legs"][0]
        for status, quantity in (("NEW", "0"), ("FILLED", "0.2")):
            with self.subTest(status=status), self.no_writes(), \
                 patch.object(self.broker(), "query", return_value=recovery_tests.receipt_for(leg, status=status, qty=quantity)):
                with self.assertRaisesRegex(TradingError, "出现成交或活动回执"):
                    require_archived_orders_clear(self.engine, pair)
            self.assert_kind("action")
        with self.no_writes(), patch.object(self.broker(), "query", side_effect=ExchangeError("offline")):
            with self.assertRaises(TradingError):
                require_archived_orders_clear(self.engine, pair)
        self.assert_kind("action")
        self.assertEqual(self.state(), original)

    def test_restarted_or_changed_watch_and_account_config_require_existing_guard_evidence(self):
        self.activate_watch()
        self.engine.pair_watch_diagnostics.clear()
        self.assert_kind("fault")
        with self.no_writes():
            counts = {aid: len(broker.api.calls) for aid, broker in self.live.items()}
            evidence = require_archived_orders_clear(self.engine, self.f.store.pair("gold"))
        self.assertEqual(len(evidence), 2)
        self.assertEqual({aid: len(broker.api.calls) - counts[aid] for aid, broker in self.live.items()},
                         {"test": 1, "second": 1})
        self.assert_kind("notice")
        runtime = self.state()
        original = deepcopy(runtime)
        runtime["recovery_watch"]["changed"] = True
        self.f.store.put("pair_runtime:gold", runtime)
        self.assert_kind("fault")
        self.f.store.put("pair_runtime:gold", original)
        saved = self.f.store.account("test")
        self.f.store.save_account({**saved, "env_prefix": "ASTER_REPLACEMENT"})
        self.assert_kind("fault")

    def test_diagnostic_publication_failure_does_not_change_guard_result(self):
        self.activate_watch()
        pair = self.f.store.pair("gold")
        with patch.object(self.engine, "pair_watch_diagnostics", None), self.no_writes():
            self.assertEqual(len(require_archived_orders_clear(self.engine, pair)), 2)
            leg = self.state()["recovery_watch"]["batches"][0]["legs"][0]
            with patch.object(self.broker(), "query", return_value=recovery_tests.receipt_for(leg, status="NEW")):
                with self.assertRaisesRegex(TradingError, "出现成交或活动回执"):
                    require_archived_orders_clear(self.engine, pair)
