"""Select the lowest relative BBO spread among usable markets at one tier."""
from dataclasses import replace
import time
import unittest
from unittest.mock import patch

from trading.engine import Engine
from trading.exchange import BudgetWait, ExchangeError
from trading.models import Book, SYMBOLS, dec
from .helpers import Fixture


XAU, SPCX, CL = SYMBOLS


class MarketPriorityTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.f.account["policy"]["symbols"] = list(SYMBOLS)
        self.f.store.save_account(self.f.account)
        self.spreads = dict(zip(SYMBOLS, map(dec, ("0.0004", "0.0001", "0.0003"))))
        self.books = {}
        def quote(symbol):
            if symbol in self.books:
                return self.books[symbol]
            mid = {XAU: dec(4000), SPCX: dec(1000), CL: dec(100)}[symbol]
            half = mid * self.spreads[symbol] / 2
            return Book(mid - half, mid + half, dec(50), dec(50), mid, time.time())
        patcher = patch.object(self.f.market, "book", side_effect=quote)
        self.quote = patcher.start()
        self.addCleanup(patcher.stop)
        self.engine = Engine(self.f.store, market=self.f.market)
        self.engine.brokers["test"] = self.f.broker
        for symbol in SYMBOLS:
            self.engine.poll_market(symbol)
            self.capacities(symbol, {4: 500000})

    def capacities(self, symbol, values):
        self.engine.markets[symbol]["capacities"] = {str(k): str(v) for k, v in values.items()}

    def open_tick(self, expected):
        with patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            self.assertEqual(self.engine.tick_account("test"), 5)
        submit.assert_called_once()
        orders, = submit.call_args.args
        self.assertEqual(len(orders), 2)
        self.assertEqual({o["symbol"] for o in orders}, {expected})
        self.assertEqual({o["positionSide"] for o in orders}, {"LONG", "SHORT"})
        self.assertEqual({o["type"] for o in orders}, {"MARKET"})
        self.assertIsNone(self.f.store.intent("test"))
        self.assertFalse(self.f.store.get("post_fill_check:test"))
        self.assertTrue(self.f.store.account("test")["enabled"])

    def test_lowest_spread_wins_even_when_rotation_starts_at_another_market(self):
        self.engine.rotation["test"] = 2
        self.open_tick(SPCX)
        self.open_tick(SPCX)

    def test_relative_spread_not_absolute_price_gap_determines_priority(self):
        self.spreads.update(zip(SYMBOLS, map(dec, ("0.0001", "0.0002", "0.0003"))))
        self.engine.rotation["test"] = 2
        self.open_tick(XAU)  # 0.4 USD1 is only 1 bp; CL's 0.03 USD1 is 3 bp.

    def test_equal_spreads_keep_rotation_between_batches(self):
        self.spreads = dict.fromkeys(SYMBOLS, dec("0.0002"))
        self.engine.rotation["test"] = 1
        self.open_tick(SPCX)
        self.open_tick(CL)

    def test_spreads_equal_after_display_rounding_are_compared_exactly(self):
        self.books[XAU] = Book(dec("999.9"), dec("1000.1"), dec(50), dec(50), dec(1000), time.time())
        self.books[SPCX] = replace(self.books[XAU], bid=dec("999.90000000000000000000000000000001"),
                                   ask=dec("1000.09999999999999999999999999999999"))
        self.assertEqual(self.books[XAU].spread, self.books[SPCX].spread)
        self.open_tick(SPCX)

    def test_next_batch_uses_changed_quotes(self):
        self.open_tick(SPCX)
        self.spreads[CL] = dec("0.00005")
        self.open_tick(CL)

    def test_capacity_at_threshold_does_not_displace_a_usable_market(self):
        self.capacities(SPCX, {4: 10000})
        self.open_tick(CL)
        self.assertIn("额度未超过阈值", self.engine.views["test"]["strategies"][SPCX]["reason"])

    def test_cooling_market_is_skipped(self):
        self.f.store.put(f"order_cooldown:test:{SPCX}", {"until": time.time() + 60})
        self.open_tick(CL)
        self.assertIn("冷却中", self.engine.views["test"]["strategies"][SPCX]["reason"])

    def test_lowest_spread_without_depth_for_minimum_order_is_skipped(self):
        self.books[SPCX] = replace(self.f.market.book(SPCX), bid_qty=dec("0.001"))
        self.open_tick(CL)
        self.assertIn("最小一笔", self.engine.views["test"]["strategies"][SPCX]["reason"])

    def test_stale_quote_is_skipped(self):
        self.books[SPCX] = replace(self.f.market.book(SPCX), timestamp=time.time() - 4)
        self.open_tick(CL)
        self.assertIn("BBO 已过期", self.engine.views["test"]["strategies"][SPCX]["reason"])

    def test_invalid_quote_is_skipped(self):
        self.books[SPCX] = replace(self.f.market.book(SPCX), bid=dec(1001), ask=dec(1000))
        self.open_tick(CL)
        self.assertIn("BBO 价格无效", self.engine.views["test"]["strategies"][SPCX]["reason"])

    def test_lowest_spread_without_account_capacity_is_skipped(self):
        snapshot = self.f.broker.snapshot
        def limited(*args, **kwargs):
            result = snapshot(*args, **kwargs)
            result.brackets[SPCX] = [{**result.brackets[SPCX][0], "notionalCap": "1"}]
            return result
        with patch.object(self.f.broker, "snapshot", side_effect=limited):
            self.open_tick(CL)

    def test_spread_does_not_override_rotation_across_different_tiers(self):
        self.f.broker.state["leverages"][XAU] = 5
        self.capacities(XAU, {5: 500000})
        self.open_tick(XAU)

    def test_same_tier_candidates_are_compared_across_another_tier(self):
        self.f.broker.state["leverages"][SPCX] = 5
        self.capacities(SPCX, {5: 500000})
        self.open_tick(CL)

    def test_same_target_tier_is_ranked_before_upgrade_and_first_open(self):
        self.f.broker.state["leverages"] = dict.fromkeys(SYMBOLS, 1)
        with patch.object(self.f.broker, "set_leverage", wraps=self.f.broker.set_leverage) as change:
            self.engine.tick_account("test")
            change.assert_called_once_with(SPCX, 4)
            self.assertEqual(self.f.store.intent("test")["symbol"], SPCX)
            self.assertEqual(self.f.broker.state["orders"], {})
            self.engine.tick_account("test")  # Confirm before comparing again.
            self.assertIsNone(self.f.store.intent("test"))
            self.open_tick(SPCX)
            self.assertEqual(change.call_count, 1)

    def test_custom_minimum_tier_uses_the_same_priority(self):
        self.f.account["policy"]["min_open_leverage"] = 7
        self.f.store.save_account(self.f.account)
        for symbol in SYMBOLS:
            self.capacities(symbol, {7: 500000})
        self.engine.tick_account("test")
        intent = self.f.store.intent("test")
        self.assertEqual((intent["symbol"], intent["target"]), (SPCX, 7))
        self.engine.tick_account("test")
        self.open_tick(SPCX)

    def test_capacity_drop_during_comparison_falls_back_without_opening_old_plan(self):
        capacity = self.engine.capacities
        reads = []
        def changed(symbol):
            reads.append(symbol)
            if symbol == SPCX and reads.count(SPCX) == 2:
                self.capacities(SPCX, {4: 10000})
            return capacity(symbol)
        with patch.object(self.engine, "capacities", side_effect=changed):
            self.open_tick(CL)

    def test_target_change_during_comparison_does_not_upgrade_to_an_unranked_tier(self):
        self.f.broker.state["leverages"] = dict.fromkeys(SYMBOLS, 1)
        capacity = self.engine.capacities
        reads = []
        def changed(symbol):
            reads.append(symbol)
            if symbol == SPCX and reads.count(SPCX) == 2:
                self.capacities(SPCX, {4: 0, 5: 500000})
            return capacity(symbol)
        with patch.object(self.engine, "capacities", side_effect=changed), \
             patch.object(self.f.broker, "set_leverage", wraps=self.f.broker.set_leverage) as change:
            self.engine.tick_account("test")
        change.assert_called_once_with(CL, 4)

    def test_comparison_reads_each_book_once_and_reuses_shared_capacities(self):
        snapshot = self.f.broker.snapshot(SYMBOLS)
        self.quote.reset_mock()
        with patch.object(self.f.broker, "snapshot", return_value=snapshot), \
             patch.object(self.f.market, "capacities", side_effect=AssertionError("use shared capacity cache")), \
             patch("trading.engine.Executor.open_pair", return_value="模拟完成") as opening:
            self.engine.tick_account("test")
        self.assertEqual([c.args[0] for c in self.quote.call_args_list], list(SYMBOLS))
        self.assertEqual(opening.call_args.args[2], SPCX)

    def test_quote_expiring_during_comparison_cannot_be_used_for_opening(self):
        snapshot = self.f.broker.snapshot(SYMBOLS)
        quotes = {s: self.f.market.book(s) for s in SYMBOLS}
        clock = [time.time()]
        def slow_quote(symbol):
            if symbol == CL:
                clock[0] += 4
            return replace(quotes[symbol], timestamp=clock[0])
        with patch.object(self.f.broker, "snapshot", return_value=snapshot), \
             patch("trading.engine.time.time", side_effect=lambda: clock[0]), \
             patch.object(self.f.market, "book", side_effect=slow_quote), \
             patch("trading.engine.Executor.open_pair", return_value="模拟完成") as opening:
            self.engine.tick_account("test")
        opening.assert_called_once()
        self.assertEqual(opening.call_args.args[2], CL)
        self.assertIn("BBO 已过期", self.engine.views["test"]["strategies"][SPCX]["reason"])

    def test_budget_wait_during_comparison_prevents_any_new_mutation(self):
        snapshot = self.f.broker.snapshot(SYMBOLS)
        quote = self.f.market.book
        def limited(symbol):
            if symbol == SPCX:
                raise BudgetWait("等待请求预算", retry_after=43)
            return quote(symbol)
        with patch.object(self.f.broker, "snapshot", return_value=snapshot), \
             patch.object(self.f.market, "book", side_effect=limited), \
             patch.object(self.f.broker, "submit", side_effect=AssertionError("comparison did not finish")), \
             patch.object(self.f.broker, "set_leverage", side_effect=AssertionError("must wait")):
            self.assertEqual(self.engine.tick_account("test"), 43)
        self.assertEqual(self.engine.views["test"]["status"], "waiting")
        self.assertIsNone(self.f.store.intent("test"))

    def test_exchange_backoff_during_comparison_still_stops_the_round(self):
        snapshot = self.f.broker.snapshot(SYMBOLS)
        with patch.object(self.f.broker, "snapshot", return_value=snapshot), \
             patch.object(self.f.market, "book", side_effect=ExchangeError("Aster 接口限流", retry_after=180)):
            self.assertEqual(self.engine.tick_account("test"), 180)
        self.assertEqual(self.engine.views["test"]["status"], "error")
        self.assertEqual(self.f.broker.state["orders"], {})


if __name__ == "__main__":
    unittest.main()
