"""Summary units, scope and freshness are explicit even when cache data is partial."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest

from trading.hourly_summary import MAX_MESSAGE_BYTES, format_hourly_summary


NOW = datetime(2026, 9, 30, 1, tzinfo=timezone.utc).timestamp()
DAY = "2026-09-30"


def account(aid="solo", **changes):
    return {"id": aid, "name": aid, "mode": "live", "enabled": True, "status": "waiting",
            "cycle": {"enabled": True, "daily_volume_limit": "1000"},
            "cycle_state": {"daily_volume": {"utc_date": DAY, "volume": "250", "sync_pending": False},
                            "report_status": {"status": "ready", "as_of": NOW}},
            "snapshot": {"timestamp": NOW, "equity": "100", "available": "70", "occupied_margin": "30",
                         "margin_ratio": ".3", "unrealized": "-2", "positions": []}, **changes}


def paired_state():
    return {"ready": True, "accounts": [account("long"), account("short")], "pairs": [{
        "id": "paired", "name": "黄金配对", "symbol": "XAUUSD1", "enabled": True,
        "long_account_id": "long", "short_account_id": "short",
        "cycle": {"enabled": True, "daily_volume_limit": "1000"},
        "state": {"phase": "waiting_open", "updated_at": NOW, "daily_volume": {DAY: {"long": "1000", "short": "500"}},
                  "margin": {"status": "waiting", "reason": "等待检查"}}}]}


class HourlySummaryTests(unittest.TestCase):
    def test_custom_interval_appears_in_summary_without_an_hourly_label(self):
        text = format_hourly_summary({"notification": {"hourly_summary": {"interval_seconds": 900}}}, NOW)
        self.assertIn("ASTER 定时运行摘要", text)
        self.assertIn("发送间隔：15 分钟", text)
        self.assertNotIn("每小时", text)

    def test_pair_sides_use_separate_targets_without_duplicate_accounts(self):
        text = format_hourly_summary(paired_state(), NOW)
        self.assertIn("UTC 日 2026-09-30", text)
        self.assertIn("09:00:00 UTC+8", text)
        self.assertIn("1,000.00 USD1 / 1,000.00 USD1（100.0%，已达标）", text)
        self.assertIn("500.00 USD1 / 1,000.00 USD1（50.0%）", text)
        self.assertNotIn("1,500.00", text)
        self.assertNotIn("账户 long（", text)
        self.assertNotIn("账户 short（", text)

    def test_missing_pair_day_is_not_zero_or_a_previous_day_total(self):
        state = paired_state()
        state["pairs"][0]["state"]["daily_volume"] = {"2026-09-29": {"long": "999", "short": "999"}}
        text = format_hourly_summary(state, NOW)
        self.assertEqual(text.count("待同步/待核实；目标 1,000.00 USD1"), 2)
        self.assertNotIn("999.00", text)
        self.assertNotIn("0.00 USD1 /", text)

    def test_unknown_pending_or_stale_pair_volume_never_claims_target_reached(self):
        for changes in ({"volume_unknown": True}, {"pending": {"id": "order"}}, {"updated_at": NOW - 121}):
            with self.subTest(changes=changes):
                state = paired_state()
                state["pairs"][0]["state"].update(changes)
                text = format_hourly_summary(state, NOW)
                self.assertIn("待同步/待核实（已记录 1,000.00 USD1）", text)
                self.assertNotIn("已达标", text)

    def test_single_account_reports_require_current_complete_cache(self):
        row = account()
        self.assertIn("250.00 USD1 / 1,000.00 USD1（25.0%）", format_hourly_summary({"accounts": [row]}, NOW))
        for target, change in (("daily_volume", {"utc_date": "2026-09-29"}),
                               ("daily_volume", {"sync_pending": True}),
                               ("report_status", {"status": "loading"}),
                               ("report_status", {"as_of": NOW - 16}),
                               ("report_status", {"as_of": NOW + 1})):
            with self.subTest(change=change):
                sample = deepcopy(row)
                sample["cycle_state"][target].update(change)
                text = format_hourly_summary({"accounts": [sample]}, NOW)
                self.assertIn("待同步/待核实", text)
                self.assertNotIn("25.0%", text)

    def test_stale_snapshot_and_missing_values_stay_visible(self):
        row = account(snapshot={"timestamp": NOW - 60, "positions": [{"symbol": "XAUUSD1", "side": "LONG", "qty": "2", "notional": "1000"}]})
        text = format_hourly_summary({"accounts": [row]}, NOW)
        self.assertIn("60 秒前，已超过 8 秒交易新鲜度", text)
        self.assertIn("权益 待同步", text)
        self.assertIn("XAUUSD1 多 2.000000", text)
        self.assertNotIn("快照记录空仓", text)

    def test_transfer_unknown_is_high_priority_and_reports_direction_amount(self):
        state = paired_state()
        state["pairs"][0]["state"]["margin"] = {"status": "unknown", "reason": "等待核对", "pending": {"source": "long", "destination": "short", "amount": "50"}}
        state["accounts"].insert(0, account("normal"))
        text = format_hourly_summary(state, NOW)
        self.assertIn("待核对划转 A 多 → B 空：50.00 USD1", text)
        self.assertLess(text.index("黄金配对"), text.index("账户 normal"))

    def test_only_whitelisted_fields_are_formatted_and_input_is_not_mutated(self):
        state = paired_state()
        state["accounts"][0].update(env_prefix="SECRET_PREFIX", api_key="SECRET_KEY")
        state["notification"] = {"webhook": "SECRET_WEBHOOK"}
        state["events"] = [{"message": "RAW_EVENT_SECRET"}]
        original = deepcopy(state)
        text = format_hourly_summary(state, NOW)
        for sentinel in ("SECRET_PREFIX", "SECRET_KEY", "SECRET_WEBHOOK", "RAW_EVENT_SECRET"):
            self.assertNotIn(sentinel, text)
        self.assertEqual(state, original)

    def test_paper_is_labelled_and_does_not_join_live_totals(self):
        text = format_hourly_summary({"accounts": [account("live"), account("paper", mode="paper")]}, NOW)
        self.assertIn("实盘 1 个账户，模拟 1 个", text)
        self.assertIn("账户 paper（模拟", text)
        self.assertNotIn("500.00 USD1 /", text)

    def test_non_finite_missing_and_zero_limit_are_not_fabricated(self):
        row = account()
        row["cycle"]["daily_volume_limit"] = "0"
        row["snapshot"].update(equity="NaN", unrealized="Infinity")
        text = format_hourly_summary({"accounts": [row]}, NOW)
        self.assertIn("250.00 USD1 / 未设上限", text)
        self.assertIn("权益 待同步", text)
        self.assertIn("浮盈亏 待同步", text)

    def test_budget_local_and_exchange_counters_are_distinct(self):
        text = format_hourly_summary({"request_budget": {"used": 900, "limit": 2400, "local_used": 700, "aster_ip_used": 850,
            "ordinary_remaining": 500, "retry_after": 12, "reset_after": 30}}, NOW)
        self.assertIn("估算 900/2,400；本进程 700；Aster 同 IP 回报 850", text)
        self.assertIn("预算/冷却等待 12 秒", text)

    def test_length_limit_keeps_an_urgent_account_even_if_it_is_last(self):
        accounts = [account(f"normal{i}", reason="等待" * 100) for i in range(80)]
        accounts.append(account("urgent", status="attention", reason="立即核对账户"))
        text = format_hourly_summary({"accounts": accounts}, NOW)
        self.assertLessEqual(len(text.encode("utf-8")), MAX_MESSAGE_BYTES)
        self.assertIn("账户 urgent", text)
        self.assertIn("未展开", text)

    def test_empty_state_is_a_valid_partial_summary(self):
        text = format_hourly_summary({}, NOW)
        self.assertIn("未就绪", text)
        self.assertIn("暂无采样", text)
        self.assertIn("尚未配置账户或配对组", text)


if __name__ == "__main__":
    unittest.main()
