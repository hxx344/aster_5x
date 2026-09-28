"""Opening diagnostics must explain the actual automatic upgrade constraints."""
from copy import deepcopy
from dataclasses import replace
from unittest import TestCase

from tests.helpers import Fixture
from trading.models import TradingError, dec, plan_pair
from trading.pair_planning import ordinary_upgrade, plan_ordinary
from trading.pairing import validate_pair


class OpeningMessagesTests(TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.symbol = "XAUUSD1"
        self.pair = validate_pair({"id": "gold", "name": "黄金配对", "long_account_id": "long",
                                   "short_account_id": "short", "ordinary": {"enabled": True}})
        self.rule = self.f.market.rules[self.symbol]
        self.book = self.f.market.book(self.symbol)

    def snapshots(self, leverage):
        snap = self.f.broker.snapshot([self.symbol])
        snap.positions = [replace(p, leverage=leverage) for p in snap.positions]
        return {"long": deepcopy(snap), "short": deepcopy(snap)}

    def reason(self, snapshots, capacities, book=None):
        with self.assertRaises(TradingError) as caught:
            plan_ordinary(self.pair, snapshots, book or self.book, self.rule, capacities)
        return str(caught.exception)

    def test_pair_floor_matches_strict_public_threshold_and_missing_tiers(self):
        snapshots = self.snapshots(2)
        capacity = {5: dec("10000"), 20: dec("10000.000000000000000000000001")}
        self.assertEqual(ordinary_upgrade(self.pair, snapshots, self.book, capacity), 20)
        reason = self.reason(snapshots, capacity)
        self.assertIn("当前均为 2x", reason)
        self.assertIn("最低 5x", reason)
        self.assertIn("5x：公开额度未严格超过 10000 USD1", reason)
        self.assertIn("10x：缺少公开额度数据", reason)
        self.assertIn("20x：公开额度及账户持仓上限已满足", reason)
        self.assertIn("两账户升档确认后再检查开仓条件", reason)

    def test_pair_targets_respect_minimum_without_suggesting_downgrade(self):
        for leverage, minimum, targets in [(2, 10, (10, 20)), (5, 20, (20,)), (7, 5, (10, 20))]:
            with self.subTest(leverage=leverage, minimum=minimum):
                self.pair["ordinary"]["min_open_leverage"] = minimum
                snapshots = self.snapshots(leverage)
                capacities = dict.fromkeys((5, 10, 20), dec("20000"))
                self.assertEqual(ordinary_upgrade(self.pair, snapshots, self.book, capacities), targets[0])
                reason = self.reason(snapshots, capacities)
                for tier in (5, 10, 20):
                    self.assertEqual(f"{tier}x：" in reason, tier in targets)
        reason = self.reason(self.snapshots(25), {})
        self.assertIn("不会自动降杠杆", reason)
        self.assertNotIn("会自动选择", reason)

    def test_public_capacity_does_not_hide_private_holding_cap_failure(self):
        snapshots = self.snapshots(2)
        snap = snapshots["short"]
        snap.positions = [replace(p, qty=dec("1")) if p.symbol == self.symbol and p.side == "SHORT" else p
                          for p in snap.positions]
        snap.brackets[self.symbol][0]["notionalCap"] = "1"
        capacity = {5: dec("20000")}
        self.assertIsNone(ordinary_upgrade(self.pair, snapshots, self.book, capacity))
        reason = self.reason(snapshots, capacity)
        self.assertIn("公开额度已满足，但做空账户的现有持仓超过该档账户持仓上限", reason)
        self.assertNotIn("5x：公开额度未", reason)

    def test_supported_tier_capacity_equal_to_threshold_still_waits(self):
        snapshots = self.snapshots(5)
        reason = self.reason(snapshots, {5: dec("10000")})
        self.assertIn("当前 5x 公开可用额度未严格超过普通开仓门槛 10000 USD1", reason)
        plan = plan_ordinary(self.pair, snapshots, self.book, self.rule, {5: dec("10000.000000000000000000000001")})
        self.assertGreater(plan.qty, 0)

    def test_spread_message_uses_configured_limit_in_both_planners(self):
        policy = {**self.f.account["policy"], "spread_limit": "0.0001"}
        self.pair["ordinary"].update(policy)
        book = replace(self.book, bid=dec("100"), ask=dec("100.02"), mark=dec("100.01"))
        snapshots = self.snapshots(5)
        reason = self.reason(snapshots, {5: dec("20000")}, book)
        single = plan_pair(snapshots["long"], book, self.rule, {5: dec("20000")}, policy)
        for text in (reason, single.reason):
            self.assertIn("1 bp", text)
            self.assertNotIn("万 5", text)

    def test_single_unsupported_high_leverage_does_not_promise_auto_recovery(self):
        plan = plan_pair(self.snapshots(25)["long"], self.book, self.rule, {}, self.f.account["policy"])
        self.assertEqual(plan.qty, 0)
        self.assertIn("不会自动降杠杆", plan.reason)
