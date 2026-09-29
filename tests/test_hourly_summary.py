"""Card values remain accurate and explicit when published caches are partial."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import unittest

from monitor import feishu_payload
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


def card(state):
    return feishu_payload(format_hourly_summary(state, NOW), "", int(NOW))["card"]


def contents(value):
    if isinstance(value, dict):
        if value.get("tag") == "plain_text":
            yield value["content"]
        for child in value.values():
            yield from contents(child)
    elif isinstance(value, list):
        for child in value:
            yield from contents(child)


def rendered(state):
    return "\n".join(contents(card(state)))


class HourlySummaryTests(unittest.TestCase):
    def test_custom_interval_and_both_time_zones_remain_visible(self):
        text = rendered({"notification": {"hourly_summary": {"interval_seconds": 900}}})
        self.assertIn("每 15 分钟", text)
        self.assertIn("09:00:00 UTC+8", text)
        self.assertIn("UTC 日 2026-09-30", text)
        self.assertNotIn("每小时", text)

    def test_pair_sides_use_separate_targets_without_duplicate_accounts(self):
        text = rendered(paired_state())
        self.assertIn("距交易量目标  已达标", text)
        self.assertIn("距交易量目标  还差 500.00", text)
        self.assertEqual(text.count("A 多 · long（实盘）"), 1)
        self.assertEqual(text.count("B 空 · short（实盘）"), 1)
        self.assertNotIn("账户 · long", text)
        self.assertNotIn("账户 · short", text)

    def test_backgrounds_use_feishu_color_references_without_white_inner_columns(self):
        result = card(paired_state())
        registered = set(result.get("config", {}).get("style", {}).get("color", {}))

        def check(node):
            if isinstance(node, dict):
                if "background_style" in node:
                    color = node["background_style"]
                    self.assertTrue(color in registered or color in {"default", "blue-50", "red-50"},
                                    "Feishu needs a color token or registered custom color, not inline CSS")
                    if node.get("tag") == "column_set" and color != "default":
                        for column in node["columns"]:
                            self.assertEqual(column.get("background_style", "default"), color)
                for value in node.values():
                    check(value)
            elif isinstance(node, list):
                for value in node:
                    check(value)

        check(result)

    def test_screenshot_values_use_occupied_over_equity_and_exact_remaining(self):
        state = paired_state()
        long, short = state["accounts"]
        long["snapshot"].update(equity="72854.90", available="6573.15", occupied_margin="66281.76",
                                margin_ratio=".114", unrealized="2563.10")
        short["snapshot"].update(equity="72737.44", available="6455.69", occupied_margin="66281.76",
                                 margin_ratio=".114", unrealized="-2577.81")
        state["pairs"][0]["cycle"]["daily_volume_limit"] = "450000"
        state["pairs"][0]["state"]["daily_volume"][DAY] = {"long": "449993.08", "short": "444998.15"}
        text = rendered(state)
        for expected in ("-14.71", "6,573.15", "6,455.69", "90.98%", "91.12%", "还差 6.92", "还差 5,001.85"):
            self.assertIn(expected, text)
        self.assertNotIn("11.4%", text)
        self.assertNotIn("已达标", text)
        state["pairs"][0]["state"]["daily_volume"][DAY]["long"] = "449999.999"
        self.assertIn("还差 0.01", rendered(state))

    def test_missing_pair_day_is_not_zero_or_a_previous_day_total(self):
        state = paired_state()
        state["pairs"][0]["state"]["daily_volume"] = {"2026-09-29": {"long": "999", "short": "999"}}
        text = rendered(state)
        self.assertEqual(text.count("待同步 / 待核实"), 2)
        self.assertNotIn("999.00", text)
        self.assertNotIn("还差", text)

    def test_unknown_pending_or_stale_pair_volume_never_claims_target_reached(self):
        for changes in ({"volume_unknown": True}, {"pending": {"id": "order"}}, {"updated_at": NOW - 121}):
            with self.subTest(changes=changes):
                state = paired_state()
                state["pairs"][0]["state"].update(changes)
                text = rendered(state)
                self.assertEqual(text.count("待同步 / 待核实"), 2)
                self.assertNotIn("已达标", text)

    def test_single_account_reports_require_current_complete_cache(self):
        row = account()
        self.assertIn("还差 750.00", rendered({"accounts": [row]}))
        for target, change in (("daily_volume", {"utc_date": "2026-09-29"}),
                               ("daily_volume", {"sync_pending": True}),
                               ("report_status", {"status": "loading"}),
                               ("report_status", {"as_of": NOW - 16}),
                               ("report_status", {"as_of": NOW + 1})):
            with self.subTest(change=change):
                sample = deepcopy(row)
                sample["cycle_state"][target].update(change)
                text = rendered({"accounts": [sample]})
                self.assertIn("待同步 / 待核实", text)
                self.assertNotIn("还差 750.00", text)

    def test_stale_snapshot_and_missing_values_stay_visible(self):
        row = account(snapshot={"timestamp": NOW - 60, "positions": [{"symbol": "XAUUSD1", "side": "LONG", "qty": "2"}]})
        text = rendered({"accounts": [row]})
        self.assertIn("账户数据 60 秒前 · 待刷新", text)
        self.assertIn("可用保证金\n待同步", text)
        self.assertIn("保证金占用率\n待同步", text)
        self.assertIn("XAUUSD1 多 2.000", text)
        self.assertNotIn("空仓", text)

    def test_latest_pair_snapshot_is_also_used_in_the_total(self):
        state = paired_state()
        state["accounts"][0]["snapshot"].update(timestamp=NOW - 60, available="1", unrealized="-999")
        state["pairs"][0]["state"]["snapshots"] = {"long": {
            "timestamp": NOW, "equity": "100", "available": "80", "occupied_margin": "20", "unrealized": "5", "positions": []}}
        text = rendered(state)
        self.assertIn("实盘合计浮盈亏 · USD1\n3.00", text)
        self.assertIn("可用保证金\n80.00", text)
        self.assertNotIn("未刷新", text)
        state["accounts"][0]["snapshot"].update(timestamp=NOW + 1, available="90", unrealized="6")
        text = rendered(state)
        self.assertIn("实盘合计浮盈亏 · USD1\n4.00", text)
        self.assertIn("账户数据待同步", text)
        self.assertIn("浮盈亏含未刷新快照", text)

    def test_partial_live_total_never_hides_a_missing_account_or_pnl(self):
        for change in ("bound_account", "unrealized"):
            with self.subTest(change=change):
                state = paired_state()
                if change == "bound_account":
                    state["accounts"].pop()
                else:
                    state["accounts"][1]["snapshot"].pop("unrealized")
                self.assertIn("实盘合计浮盈亏 · USD1\n待同步", rendered(state))

    def test_transfer_unknown_is_high_priority_and_reports_direction_amount(self):
        state = paired_state()
        state["pairs"][0]["state"]["margin"] = {"status": "unknown", "reason": "等待核对", "pending": {
            "source": "long", "destination": "short", "amount": "50"}}
        state["accounts"].insert(0, account("normal"))
        text = rendered(state)
        self.assertIn("待核对划转 A 多 → B 空：50.00 USD1", text)
        self.assertLess(text.index("黄金配对"), text.index("账户 · normal"))

    def test_only_whitelisted_fields_are_formatted_as_plain_text_without_mutation(self):
        state = paired_state()
        name = '<at id=all></at> **名字** [链接](https://invalid.example)'
        state["accounts"][0].update(name=name, env_prefix="SECRET_PREFIX", api_key="SECRET_KEY")
        state["notification"] = {"webhook": "SECRET_WEBHOOK"}
        state["events"] = [{"message": "RAW_EVENT_SECRET"}]
        original = deepcopy(state)
        result = card(state)
        text = json.dumps(result, ensure_ascii=False)
        for sentinel in ("SECRET_PREFIX", "SECRET_KEY", "SECRET_WEBHOOK", "RAW_EVENT_SECRET"):
            self.assertNotIn(sentinel, text)
        self.assertIn(name, "\n".join(contents(result)))
        self.assertNotIn('"tag": "markdown"', text)
        self.assertNotIn('"tag": "lark_md"', text)
        self.assertEqual(state, original)

    def test_paper_is_labelled_and_does_not_join_live_totals(self):
        row = account("paper", mode="paper")
        row["snapshot"]["unrealized"] = "999"
        text = rendered({"accounts": [account("live"), row]})
        self.assertIn("账户 · paper（模拟）", text)
        self.assertIn("实盘合计浮盈亏 · USD1\n-2.00", text)

    def test_non_finite_missing_and_zero_limit_are_not_fabricated(self):
        row = account()
        row["cycle"]["daily_volume_limit"] = "0"
        row["snapshot"].update(equity="NaN", unrealized="Infinity")
        text = rendered({"accounts": [row]})
        self.assertIn("未设目标上限", text)
        self.assertIn("保证金占用率\n待同步", text)
        self.assertIn("实盘合计浮盈亏 · USD1\n待同步", text)
        for changes in ({"equity": "0"}, {"equity": "-1"}, {"equity": None},
                        {"equity": "100", "occupied_margin": None}, {"equity": "100", "occupied_margin": "-1"}):
            with self.subTest(changes=changes):
                row["snapshot"].update(changes)
                self.assertIn("保证金占用率\n待同步", rendered({"accounts": [row]}))

    def test_budget_and_ws_are_short_but_waits_and_errors_remain_visible(self):
        state = {"request_budget": {"used": 900, "limit": 2400, "local_used": 700, "aster_ip_used": 850,
                                   "ordinary_remaining": 500, "retry_after": 12, "reset_after": 30},
                 "capacity_relay": {"connected": False, "last_error": "连接超时"}}
        text = rendered(state)
        self.assertIn("WS 未连接 · API 900 / 2,400", text)
        self.assertIn("API 预算 / 冷却等待 12 秒", text)
        self.assertIn("WS：连接超时", text)
        self.assertNotIn("本进程", text)

    def test_capacity_uses_matching_tier_and_does_not_turn_stale_into_zero(self):
        state = paired_state()
        for row, side in zip(state["accounts"], ("LONG", "SHORT")):
            row["snapshot"]["positions"] = [{"symbol": "XAUUSD1", "side": side, "qty": "79.363", "leverage": 5}]
        state["markets"] = {"XAUUSD1": {"capacities": {"5": 0, "10": 999}, "capacity_checked_at": {"5": NOW, "10": NOW}}}
        state["pairs"][0]["state"]["reason"] = "循环公开可用额度低于本轮开仓检查要求"
        text = rendered(state)
        self.assertIn("等待公开额度", text)
        self.assertIn("公开额度 5x  0.00", text)
        self.assertIn("持仓  多 79.363 / 空 79.363", text)
        state["markets"]["XAUUSD1"]["capacity_checked_at"]["5"] = NOW - 8
        self.assertIn("公开额度 5x 待同步", rendered(state))

    def test_unknown_positions_never_claim_empty(self):
        row = account(snapshot={})
        self.assertIn("持仓 待同步", rendered({"accounts": [row]}))
        self.assertNotIn("空仓", rendered({"accounts": [row]}))

    def test_wire_size_limit_keeps_an_urgent_account_even_if_it_is_last(self):
        accounts = [account(f"normal{i}", name="普通账户" * 30, reason="等待" * 100) for i in range(80)]
        accounts.append(account("urgent", status="attention", reason="立即核对账户"))
        envelope = format_hourly_summary({"accounts": accounts}, NOW)
        payload = feishu_payload(envelope, "example-secret", int(NOW))
        # Match request_json's actual escaped JSON, not only Chinese UTF-8 text.
        self.assertLessEqual(len(json.dumps(payload).encode("utf-8")), MAX_MESSAGE_BYTES)
        text = "\n".join(contents(payload["card"]))
        self.assertIn("账户 · urgent", text)
        self.assertIn("未展开", text)
        self.assertLess(text.index("urgent"), text.index("普通账户"))

    def test_empty_state_is_a_valid_partial_summary(self):
        text = rendered({})
        self.assertIn("未就绪", text)
        self.assertIn("暂无采样", text)
        self.assertIn("尚未配置账户或配对组", text)


if __name__ == "__main__":
    unittest.main()
