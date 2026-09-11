import copy
from dataclasses import replace
from fractions import Fraction
import random
import time
import unittest

from trading.engine import Engine
from trading.execution import Executor
from trading.models import Book, TradingError, dec, decimal_value, floor_step, hedge_balanced, plan_pair
from trading.paper import DemoMarket, PaperBroker
from .helpers import Fixture


class ScenarioMarket(DemoMarket):
    def __init__(self):
        super().__init__()
        self.prices = {symbol: dec(100) for symbol in self.rules}
        self.width = dec(0)
        self.depth = dec(50)

    def book(self, symbol):
        mark = self.prices[symbol]
        return Book(mark * (1 - self.width), mark * (1 + self.width), self.depth, self.depth, mark, time.time())


class RiskScenarioTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)

    def test_precision_beyond_decimal_default_cannot_cross_quantity_or_spread_limits(self):
        self.assertFalse(hedge_balanced("1", ".998999999999999999999999999999999999"))
        self.assertTrue(hedge_balanced("1", ".999"))
        self.assertEqual(floor_step(dec(".99999999999999999999999999999"), dec(1)), 0)
        snapshot = self.f.broker.snapshot(["XAUUSD1"])
        book = replace(self.f.market.book("XAUUSD1"), bid=dec("99.975"),
                       ask=dec("100.0250000000000000000000000000000000000001"), mark=dec(100))
        plan = plan_pair(snapshot, book, self.f.market.rules["XAUUSD1"], {5: dec(500000)}, self.f.account["policy"])
        self.assertEqual(plan.qty, 0)
        self.assertIn("价差", plan.reason)

    def test_exact_occupancy_limit_rejects_tiny_excess_despite_rounded_display(self):
        snapshot = self.f.broker.snapshot(["XAUUSD1"])
        long, short = snapshot.pair("XAUUSD1")
        long.qty = short.qty = dec(1)
        long.mark = short.mark = dec(1)
        snapshot.equity = dec(".799999999999999999999999999999999999")
        self.assertEqual(snapshot.ratio, dec(".5"))
        self.assertTrue(snapshot.margin_exceeds(".5"))
        engine = Engine(self.f.store, market=self.f.market)
        self.assertTrue(engine.check_post_fill_occupancy(self.f.account, snapshot))
        self.assertFalse(self.f.store.account("test")["enabled"])

        long.qty = dec("1.0000000000000000000000000001")
        short.qty = dec(0)
        snapshot.equity = dec(".4")
        self.assertTrue(snapshot.margin_exceeds(".5"))
        self.assertEqual(long.occupied_margin_exact, Fraction(long.qty) / 5)

    def test_sizing_uses_exact_constraint_at_last_quantity_step(self):
        snapshot = self.f.broker.snapshot(["XAUUSD1"])
        snapshot.equity = dec("999.999999999999999999999999999999999")
        snapshot.available = dec(2000)
        snapshot.fees["XAUUSD1"] = dec(0)
        book = replace(self.f.market.book("XAUUSD1"), bid=dec(1250), ask=dec(1250), mark=dec(1250))
        rule = replace(self.f.market.rules["XAUUSD1"], step=dec(".1"), min_qty=dec(".1"), min_notional=dec(".1"))
        plan = plan_pair(snapshot, book, rule, {5: dec(500000)},
                         {**self.f.account["policy"], "order_notional": "2000"})
        self.assertEqual(plan.qty, dec(".9"))
        self.assertGreater(Fraction(500) / Fraction(snapshot.equity), Fraction(1, 2))

    def test_terminating_order_quantities_survive_rational_conversion_exactly(self):
        for value in (Fraction(1, 2 ** 100), Fraction(1, 5 ** 100), Fraction(-7, 10 ** 80), Fraction(0)):
            with self.subTest(value=value):
                self.assertEqual(Fraction(decimal_value(value, exact=True)), value)
        with self.assertRaises(TradingError):
            decimal_value(Fraction(1, 3), exact=True)

    def test_three_seeded_portfolio_runs_respect_risk_after_simulated_fills(self):
        market = ScenarioMarket()
        broker = PaperBroker("test", market, self.f.store)
        initial = copy.deepcopy(broker.state)
        opened = blocked = 0
        for seed in (17, 139, 7723):
            randomizer = random.Random(seed)
            for case in range(120):
                with self.subTest(seed=seed, case=case):
                    broker.state = copy.deepcopy(initial)
                    market.width = dec(0)
                    market.depth = dec(50)
                    for symbol in market.rules:
                        market.prices[symbol] = dec(randomizer.choice((100, 1000, 5000, 10000)))
                        broker.state["leverages"][symbol] = randomizer.choice((5, 10, 20))
                        quantity = dec(randomizer.randint(10, 100)) / 10
                        difference = dec(randomizer.randint(0, int(quantity))) / 1000
                        smaller = randomizer.choice(("LONG", "SHORT"))
                        for side in ("LONG", "SHORT"):
                            broker.state["positions"][symbol + ":" + side] = {
                                "qty": str(quantity - difference if side == smaller else quantity),
                                "entry": str(market.prices[symbol]),
                            }
                    occupied = broker.snapshot(["XAUUSD1"]).occupied_margin
                    broker.state["wallet"] = str(occupied * dec(randomizer.choice(("1.7", "1.95", "2", "2.02", "2.5", "4", "6", "8"))))
                    snapshot = broker.snapshot(["XAUUSD1"])
                    baseline = tuple(p.qty for p in snapshot.pair("XAUUSD1"))
                    market.prices["XAUUSD1"] *= dec(randomizer.choice((".98", "1", "1.02")))
                    market.width = dec(randomizer.choice(("0", ".0001")))
                    market.depth = dec(randomizer.choice((".1", "1", "10", "50", "100")))
                    book = market.book("XAUUSD1")
                    leverage = snapshot.pair("XAUUSD1")[0].leverage
                    policy = {**self.f.account["policy"], "order_notional": str(randomizer.choice((1000, 2000, 5000)))}
                    capacities = {leverage: dec(randomizer.choice((10001, 12000, 500000)))}
                    plan = plan_pair(snapshot, book, market.rules["XAUUSD1"], capacities, policy)
                    if not plan.qty:
                        blocked += 1
                        continue
                    opened += 1
                    self.assertGreaterEqual(Fraction(plan.qty) * Fraction(book.mark), 500)
                    self.assertEqual(plan.qty % market.rules["XAUUSD1"].step, 0)
                    orders = [Executor.order("XAUUSD1", side, "BUY" if side == "LONG" else "SELL", plan.qty,
                                             f"{seed}-{case}-{side}")
                              for side in ("LONG", "SHORT")]
                    receipts = broker.submit(orders)
                    self.assertTrue(all(row["status"] == "FILLED" for row in receipts))
                    after = broker.snapshot(["XAUUSD1"])
                    self.assertFalse(after.margin_exceeds(".55" if leverage in (10, 20) else ".5"))
                    self.assertGreaterEqual(after.available, 0)
                    # PaperBroker persists rounded weighted entry prices. Allow
                    # display rounding here; the applicable risk check stays exact.
                    self.assertLessEqual(after.ratio - plan.projected_ratio, dec("1e-26"))
                    long, short = after.pair("XAUUSD1")
                    self.assertTrue(hedge_balanced(long.qty, short.qty))
                    self.assertEqual((long.qty - baseline[0], short.qty - baseline[1]), (plan.qty, plan.qty))
        self.assertGreater(opened, 60)
        self.assertGreater(blocked, 60)
