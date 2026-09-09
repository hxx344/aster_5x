"""Account display metrics retain gross exposure and separate risk meanings."""
from dataclasses import replace
from decimal import localcontext
from fractions import Fraction
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from trading.engine import Engine, snapshot_json
from trading.models import Position, TradingError, dec, plan_pair
from trading.server import create_app
from .helpers import Fixture, account


SYMBOL = "XAUUSD1"


class AccountMetricsTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.base = self.f.broker.snapshot([SYMBOL])

    def positions(self):
        # Entries deliberately differ from marks. BTC is outside this strategy.
        return [Position(SYMBOL, "LONG", dec(2), dec(1000), dec(5000), 4),
                Position(SYMBOL, "SHORT", dec(2), dec(9000), dec(5000), 4),
                Position("SPCXUSD1", "LONG", dec(1), dec(6000), dec(5000), 5),
                Position("BTCUSD1", "SHORT", dec(".01"), dec(80000), dec(100000), 10)]

    def test_total_notional_sums_gross_mark_values_across_all_snapshot_positions(self):
        snapshot = replace(self.base, positions=self.positions())
        result = snapshot_json(snapshot, [SYMBOL])
        self.assertEqual(snapshot.total_notional, dec(26000))
        self.assertEqual(result["total_notional"], "26000")
        self.assertEqual([dec(row["notional"]) for row in result["positions"]],
                         [10000, 10000, 5000, 1000])

    def test_leverage_changes_occupancy_but_neither_total_notional_nor_margin_ratio(self):
        snapshot = replace(self.base, equity=dec(20000), maintenance=dec(250), positions=self.positions())
        higher = replace(snapshot, positions=[replace(p, leverage=20) for p in snapshot.positions])
        self.assertEqual((snapshot.total_notional, higher.total_notional), (26000, 26000))
        self.assertEqual((snapshot.margin_ratio, higher.margin_ratio), (dec(".0125"), dec(".0125")))
        self.assertGreater(snapshot.ratio, higher.ratio)

    def test_flat_positions_have_zero_notional_but_preserve_account_maintenance(self):
        for positions in ([], self.base.positions):
            with self.subTest(empty_list=not positions):
                snapshot = replace(self.base, equity=dec(2000), maintenance=dec(50), positions=positions)
                result = snapshot_json(snapshot, [SYMBOL])
                self.assertEqual(result["total_notional"], "0")
                self.assertEqual(dec(result["ratio"]), 0)
                self.assertEqual(dec(result["margin_ratio"]), dec(".025"))

    def test_nonpositive_equity_returns_null_ratios_through_snapshot_and_state_api(self):
        engine = Engine(self.f.store, market=self.f.market)
        password = "test-only-account-metrics-password"
        with patch.dict(os.environ, {"ASTER_DASHBOARD_PASSWORD": password}, clear=True):
            app = create_app(engine, start_engine=False)
        with TestClient(app) as client:
            response = client.post("/api/login", json={"password": password}, headers={"origin": "http://testserver"})
            self.assertEqual(response.status_code, 200, response.text)
            for equity in (dec(0), dec(-100)):
                with self.subTest(equity=equity):
                    snapshot = replace(self.base, equity=equity, maintenance=dec(250), positions=self.positions())
                    with self.assertRaises(TradingError):
                        _ = snapshot.margin_ratio
                    result = snapshot_json(snapshot, [SYMBOL])
                    self.assertIsNone(result["margin_ratio"])
                    self.assertIsNone(result["ratio"])
                    self.assertEqual(result["total_notional"], "26000")
                    engine.view("test", snapshot=result)
                    response = client.get("/api/state")
                    self.assertEqual(response.status_code, 200, response.text)
                    returned = response.json()["accounts"][0]["snapshot"]
                    self.assertEqual(returned, result)

    def test_total_notional_keeps_small_exposure_beside_more_than_twenty_eight_digits(self):
        large = Position(SYMBOL, "LONG", dec(10), dec(0), dec("1000000000000000000000000000"), 4)
        tiny = Position("BTCUSD1", "SHORT", dec(".1234567890123456789012345678"), dec(0), dec(1), 4)
        snapshot = replace(self.base, positions=[large, tiny])
        expected = "10000000000000000000000000000.1234567890123456789012345678"
        with localcontext() as context:
            context.prec = 12
            self.assertEqual(Fraction(snapshot.total_notional), Fraction(expected))
            self.assertEqual(snapshot_json(snapshot, [SYMBOL])["total_notional"], expected)

    def test_maintenance_ratio_and_occupied_ratio_are_distinct_decimal_strings(self):
        positions = [Position(SYMBOL, side, dec(10), dec(90), dec(100), 4) for side in ("LONG", "SHORT")]
        snapshot = replace(self.base, equity=dec(1000), maintenance=dec(125), positions=positions)
        result = snapshot_json(snapshot, [SYMBOL])
        self.assertEqual(result["total_notional"], "2000")
        self.assertEqual(result["occupied_margin"], "500")
        self.assertIsInstance(result["margin_ratio"], str)
        self.assertEqual(dec(result["margin_ratio"]), dec(".125"))
        self.assertEqual(dec(result["ratio"]), dec(".5"))

    def test_display_metrics_leave_existing_occupancy_based_planning_unchanged(self):
        book = self.f.market.book(SYMBOL)
        rules, policy = self.f.market.rules[SYMBOL], self.f.account["policy"]
        capacities = {4: dec(500000)}
        baseline = plan_pair(self.base, book, rules, capacities, policy)
        self.assertGreater(baseline.qty, 0)
        expensive_maintenance = replace(self.base, maintenance=self.base.equity)
        self.assertEqual(expensive_maintenance.margin_ratio, 1)
        snapshot_json(expensive_maintenance, [SYMBOL])
        self.assertEqual(plan_pair(expensive_maintenance, book, rules, capacities, policy), baseline)
        occupied = replace(self.base, equity=dec(1000), maintenance=dec(1),
                           positions=[Position(SYMBOL, side, dec(1), book.mark, book.mark, 4)
                                      for side in ("LONG", "SHORT")])
        self.assertLess(occupied.margin_ratio, dec(policy["margin_limit"]))
        self.assertGreater(occupied.ratio, dec(policy["margin_limit"]))
        self.assertEqual(plan_pair(occupied, book, rules, capacities, policy).qty, 0)

    def test_account_refresh_and_repeated_state_reads_keep_metrics_isolated_without_extra_requests(self):
        engine = Engine(self.f.store, market=self.f.market)
        first = replace(self.base, equity=dec(20000), maintenance=dec(250), positions=self.positions())
        second = replace(self.base, equity=dec(1000), maintenance=dec(25))
        for account_id in ("test", "second"):
            saved = account(account_id)
            saved["enabled"] = False
            self.f.store.save_account(saved)
            engine.brokers[account_id] = self.f.broker
        with patch.object(self.f.broker, "snapshot", side_effect=[first, second]) as reads, \
             patch.object(self.f.broker, "submit", side_effect=AssertionError("display refresh must not trade")), \
             patch.object(self.f.market, "book", side_effect=AssertionError("metrics must use the existing snapshot")), \
             patch.object(self.f.market, "capacities", side_effect=AssertionError("metrics must not request capacity")):
            engine.tick_account("test")
            engine.tick_account("second")
            for _ in range(3):
                by_id = {row["id"]: row["snapshot"] for row in engine.state()["accounts"]}
                self.assertEqual((by_id["test"]["total_notional"], by_id["test"]["margin_ratio"]), ("26000", "0.0125"))
                self.assertEqual((by_id["second"]["total_notional"], by_id["second"]["margin_ratio"]), ("0", "0.025"))
            self.assertEqual(reads.call_count, 2)


if __name__ == "__main__":
    unittest.main()
