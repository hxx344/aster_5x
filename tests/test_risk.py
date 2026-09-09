from dataclasses import replace
import time
import unittest

from trading.models import Book, TradingError, dec, leverage_cap, maintenance_for, next_leverage, plan_pair, validate_brackets
from .helpers import Fixture


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.snapshot = self.f.broker.snapshot(["XAUUSD1"])
        self.book = self.f.market.book("XAUUSD1")
        self.rules = self.f.market.rules["XAUUSD1"]
        self.capacities = {4: dec(500000), 5: dec(500000), 10: dec(500000), 20: dec(500000)}

    def plan(self, **changes):
        return plan_pair(changes.get("snapshot", self.snapshot), changes.get("book", self.book), self.rules,
                         changes.get("capacities", self.capacities), changes.get("policy", self.f.account["policy"]))

    def test_current_tier_drives_additions_after_upgrade(self):
        for p in self.snapshot.positions:
            if p.symbol == "XAUUSD1":
                p.leverage = 5
        self.capacities[4] = dec(0)
        self.assertGreater(self.plan().qty, 0)
        self.capacities[5] = dec(10000)
        self.assertEqual(self.plan().qty, 0)
        self.assertIn("5x", self.plan().reason)

    def test_threshold_is_strict_and_missing_tier_is_not_zero(self):
        self.capacities[4] = dec(10000)
        self.assertEqual(self.plan().qty, 0)
        del self.capacities[4]
        with self.assertRaises(TradingError):
            self.plan()

    def test_spread_uses_midprice_and_allows_exact_five_bps(self):
        at_limit = replace(self.book, bid=dec("99.975"), ask=dec("100.025"), mark=dec(100))
        self.assertEqual(at_limit.spread, dec("0.0005"))
        self.assertGreater(self.plan(book=at_limit).qty, 0)
        self.assertEqual(self.plan(book=replace(at_limit, ask=dec("100.026"))).qty, 0)

    def test_risk_at_fifty_percent_blocks(self):
        snapshot = replace(self.snapshot, maintenance=self.snapshot.equity / 2)
        self.assertEqual(self.plan(snapshot=snapshot).qty, 0)

    def test_sizing_shrinks_and_stays_strictly_below_limit(self):
        snapshot = replace(self.snapshot, equity=dec(1000), wallet=dec(1000), available=dec(1000), maintenance=dec("499"))
        plan = self.plan(snapshot=snapshot)
        self.assertGreater(plan.qty, 0)
        self.assertLess(plan.projected_ratio, dec("0.5"))
        self.assertLess(plan.qty * self.book.ask, dec(1000))
        self.assertEqual(plan.qty % self.rules.step, 0)

    def test_cash_includes_both_legs_and_costs(self):
        snapshot = replace(self.snapshot, available=dec("20"))
        plan = self.plan(snapshot=snapshot)
        cost = plan.qty * (2 * max(self.book.ask, self.book.mark) / 4 + (self.book.ask + self.book.bid) * dec("0.0004") + self.book.ask - self.book.bid)
        self.assertLessEqual(cost, snapshot.available)

    def test_book_depth_and_minimum_order_are_respected(self):
        shallow = replace(self.book, ask_qty=dec("0.005"))
        self.assertLessEqual(self.plan(book=shallow).qty, dec("0.005"))
        dust = replace(self.book, ask_qty=dec("0.00001"))
        self.assertEqual(self.plan(book=dust).qty, 0)

    def test_stale_account_and_quotes_fail_closed(self):
        with self.assertRaises(TradingError):
            self.plan(book=replace(self.book, timestamp=time.time() - 10))
        with self.assertRaises(TradingError):
            self.plan(snapshot=replace(self.snapshot, timestamp=time.time() - 10))

    def test_wrong_modes_open_orders_and_imbalance_block(self):
        for change in ({"hedge_mode": False}, {"multi_assets": True}, {"can_trade": False}, {"open_orders": [{}]}, {"equity": dec(0)}):
            with self.subTest(change=change), self.assertRaises(TradingError):
                self.plan(snapshot=replace(self.snapshot, **change))
        self.snapshot.pair("XAUUSD1")[0].qty = dec(1)
        self.assertEqual(self.plan().qty, 0)

    def test_upgrade_requires_more_than_gross_notional_and_one_rung(self):
        for p in self.snapshot.pair("XAUUSD1"):
            p.qty, p.mark = dec(1), dec(100)
        self.capacities[5] = dec(200)
        self.assertIsNone(next_leverage(self.snapshot, "XAUUSD1", self.capacities))
        self.capacities[5] = dec("200.01")
        self.assertEqual(next_leverage(self.snapshot, "XAUUSD1", self.capacities), 5)
        self.assertIsNone(next_leverage(self.snapshot, "XAUUSD1", self.capacities, mark=dec(101)))

    def test_account_brackets_and_tiered_maintenance(self):
        brackets = [{"notionalFloor": 0, "notionalCap": 1000, "maintMarginRatio": ".01", "cum": 0, "initialLeverage": 20},
                    {"notionalFloor": 1000, "notionalCap": 5000, "maintMarginRatio": ".02", "cum": 10, "initialLeverage": 5}]
        self.assertEqual(maintenance_for(dec(1500), brackets), 20)
        self.assertEqual(leverage_cap(brackets, 20), 1000)
        with self.assertRaises(TradingError):
            maintenance_for(dec(6000), brackets)

    def test_no_credit_for_one_leg_unrealized_gain_during_execution(self):
        far_mark = replace(self.book, mark=self.book.ask + dec(1000))
        tight = replace(self.snapshot, equity=dec(1000), maintenance=dec(480), available=dec(1000))
        normal = self.plan(snapshot=tight)
        conservative = self.plan(snapshot=tight, book=far_mark)
        self.assertLessEqual(conservative.qty, normal.qty)

    def test_non_finite_or_boolean_amounts_rejected(self):
        for value in (None, True, "NaN", "Infinity"):
            with self.subTest(value=value), self.assertRaises(TradingError):
                dec(value)

    def test_malformed_risk_tiers_fail_closed(self):
        valid = [{"notionalFloor": 0, "notionalCap": 1000, "maintMarginRatio": ".01", "cum": 0, "initialLeverage": 20},
                 {"notionalFloor": 1000, "notionalCap": 5000, "maintMarginRatio": ".02", "cum": 10, "initialLeverage": 5}]
        self.assertEqual(validate_brackets(valid), valid)
        for change in ({"notionalFloor": 1001}, {"cum": 11}, {"initialLeverage": 0}, {"maintMarginRatio": -1}):
            with self.subTest(change=change), self.assertRaises(TradingError):
                validate_brackets([valid[0], {**valid[1], **change}])
