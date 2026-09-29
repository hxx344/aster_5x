"""Explain the actual transfer limiter without changing amounts or doing I/O."""
from copy import deepcopy
from fractions import Fraction
import unittest

from trading.margin_balance import DEFAULT_MARGIN, MarginBalancer
from trading.models import Position, dec
from tests.test_pair_margin import snapshot


class PairMarginDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.pair = {"ordinary": {"margin_limit": "0.9"}}
        self.members = {side: {"policy": {"margin_limit": "0.9"}} for side in ("long", "short")}
        self.config = {**DEFAULT_MARGIN, "threshold": "1200", "min_transfer": "500", "max_transfer": "20000"}
        self.snapshots = {"long": snapshot("73951.89"), "short": snapshot("71721.78")}
        self.snapshots["long"].available = dec("8081.79")
        self.snapshots["short"].available = dec("5828.26")
        self.snapshots["long"].positions = [
            Position("XAUUSD1", "LONG", dec("3293.44742115"), dec(100), dec(100), 5)]
        self.withdrawable = {"long": dec(8000), "short": dec(5800)}

    def explain(self, **kwargs):
        diagnostics = {}
        before = deepcopy((self.pair, self.members, self.snapshots, self.config, self.withdrawable))
        result = MarginBalancer._plan(self.pair, self.members, self.snapshots, self.config,
            self.withdrawable, diagnostics=diagnostics, **kwargs)
        self.assertEqual(result, MarginBalancer._plan(self.pair, self.members, self.snapshots,
            self.config, self.withdrawable, **kwargs))
        self.assertEqual(before, (self.pair, self.members, self.snapshots, self.config, self.withdrawable))
        return result, MarginBalancer._no_transfer_reason(self.snapshots, self.config, diagnostics)

    def test_screenshot_names_risk_buffer_and_base_limits(self):
        result, reason = self.explain()
        self.assertIsNone(result)
        for text in ("转出侧 A 多侧占用率约 89.07%", "划转后占用上限 85.00%",
                     "编组基础 90.00%", "再扣 5.00 个百分点",
                     "不含普通高杠杆或循环额外的 5 个百分点", "限制项：风险缓冲（可划 0.00000000 USD1）",
                     "低于最小划转额 500 USD1"):
            self.assertIn(text, reason)
        self.assertNotIn("限制项：现金保留", reason)

    def test_group_limit_and_newer_exchange_occupation_are_reported(self):
        self.members["long"]["policy"]["margin_limit"] = "0.8"
        result, reason = self.explain(occupied_floors={"long": Fraction(70000)})
        self.assertIsNone(result)
        self.assertIn("编组基础 90.00%", reason)
        self.assertNotIn("账户基础", reason)
        self.assertIn("划转后占用上限 85.00%", reason)
        self.assertIn("占用率约 94.65%", reason)

    def test_screenshot_group_95_buffer_1_allows_balance_despite_account_50(self):
        self.pair["ordinary"]["margin_limit"] = "0.95"
        for member in self.members.values():
            member["policy"]["margin_limit"] = "0.5"
        self.config.update(buffer_ratio="0.01", threshold="1000", min_transfer="300")
        self.snapshots = {"long": snapshot("70601.50"), "short": snapshot("75079.21")}
        self.snapshots["long"].available = dec("4801.48")
        self.snapshots["short"].available = dec("9279.19")
        result, _ = self.explain(occupied_floors={"short": Fraction(dec("65799.42"))})
        self.assertEqual(result, {"source": "short", "destination": "long", "amount": "2238.85500000"})
        result, reason = self.explain(occupied_floors={"short": Fraction(dec("71000"))})
        self.assertIsNone(result)
        self.assertIn("划转后占用上限 94.00%（编组基础 95.00%，再扣 1.00 个百分点", reason)
        self.assertNotIn("账户基础", reason)

    def test_cash_and_withdrawal_constraints_are_distinguished(self):
        self.config.update(threshold="10", min_transfer="100")
        self.snapshots = {"long": snapshot(10000), "short": snapshot(10000)}
        self.snapshots["long"].available = dec(550)
        self.snapshots["short"].available = dec(0)
        result, reason = self.explain()
        self.assertIsNone(result)
        self.assertIn("限制项：现金保留（可划 50.00000000 USD1）", reason)
        self.assertNotIn("风险缓冲（可划", reason)
        self.snapshots["long"].available = dec(1000)
        self.withdrawable["long"] = dec(60)
        result, reason = self.explain()
        self.assertIsNone(result)
        self.assertIn("限制项：来源账户可划余额（可划 60.00000000 USD1）", reason)

    def test_successful_plan_unchanged_and_reverse_side_identified(self):
        self.snapshots["long"].positions = []
        result, _ = self.explain()
        self.assertEqual(result, {"source": "long", "destination": "short", "amount": "1126.76500000"})
        self.snapshots["long"], self.snapshots["short"] = self.snapshots["short"], self.snapshots["long"]
        self.withdrawable["short"] = dec(0)
        result, reason = self.explain()
        self.assertIsNone(result)
        self.assertIn("转出侧 B 空侧", reason)


if __name__ == "__main__":
    unittest.main()
