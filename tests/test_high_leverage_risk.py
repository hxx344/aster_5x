"""Account-wide headroom for confirmed 10x / 20x opening batches."""
from dataclasses import replace
from fractions import Fraction
import sqlite3
import time
import unittest
from unittest.mock import patch

from trading.engine import Engine
from trading.execution import Executor
from trading.models import SYMBOLS, TradingError, dec, opening_margin_limit, plan_pair
from trading.paper import PaperBroker
from trading.store import Store
from .helpers import Fixture, account
from .test_risk_scenarios import ScenarioMarket


class HighLeverageRiskTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.market = ScenarioMarket()
        self.market.depth = dec(10000)
        self.broker = PaperBroker("test", self.market, self.f.store)
        self.account = self.f.account
        self.policy = self.account["policy"]
        self.policy.update(symbols=list(SYMBOLS), margin_limit=".9", order_notional="10000", min_open_leverage=5)
        self.f.store.save_account(self.account)
        self.broker.state["wallet"] = "20000"
        self.broker.state["leverages"].update(XAUUSD1=5, SPCXUSD1=10, CLUSD1=20)
        for side in ("LONG", "SHORT"):
            self.broker.state["positions"]["XAUUSD1:" + side] = {"qty": "450", "entry": "100"}
        self.broker.save()

    def snapshot(self, *, fees=True):
        snapshot = self.broker.snapshot(SYMBOLS)
        if not fees:
            snapshot.fees = dict.fromkeys(SYMBOLS, dec(0))
        return snapshot

    def plan(self, symbol="SPCXUSD1", snapshot=None, book=None, capacities=None):
        snapshot = snapshot or self.snapshot(fees=False)
        leverage = snapshot.pair(symbol)[0].leverage
        return plan_pair(snapshot, book or self.market.book(symbol), self.market.rules[symbol],
                         capacities if capacities is not None else {leverage: dec(500000)}, self.policy)

    def engine(self, symbol="SPCXUSD1", tier=10):
        engine = Engine(self.f.store, market=self.market)
        engine.brokers["test"] = self.broker
        for name in SYMBOLS:
            engine.poll_market(name)
            engine.markets[name]["capacities"] = {str(tier): "500000"} if name == symbol else {}
        return engine

    def test_xau_only_at_ninety_can_open_new_spcx_or_cl_to_exactly_ninety_five(self):
        for symbol in ("SPCXUSD1", "CLUSD1"):
            for tier in (10, 20):
                with self.subTest(symbol=symbol, tier=tier):
                    self.broker.state["leverages"][symbol] = tier
                    snapshot = self.snapshot(fees=False)
                    self.assertEqual(snapshot.ratio, dec(".9"))
                    self.assertTrue(all(p.qty == 0 for p in snapshot.positions if p.symbol != "XAUUSD1"))
                    plan = self.plan(symbol, snapshot)
                    self.assertEqual(plan.qty, tier * 5)
                    self.assertEqual(plan.projected_ratio, dec(".95"))

    def test_extra_limit_applies_only_to_exact_tiers_and_never_above_one_hundred(self):
        for tier in (2, 4, 5, 7, 10, 15, 20, 125):
            snapshot = self.snapshot(fees=False)
            for position in snapshot.pair("SPCXUSD1"):
                position.leverage = tier
            with self.subTest(tier=tier):
                self.assertEqual(bool(self.plan(snapshot=snapshot).qty), tier in (10, 20))
        for base, expected in ((".5", ".55"), (".9", ".95"), (".95", "1"), (".99", "1"), ("1", "1")):
            for tier in (10, 20):
                self.assertEqual(opening_margin_limit({"margin_limit": base}, tier), dec(expected))
        base = ".900000000000000000000000000000000001"
        self.assertEqual(Fraction(opening_margin_limit({"margin_limit": base}, 10)), Fraction(dec(base)) + Fraction(1, 20))

    def test_fee_and_spread_charges_keep_next_quantity_step_outside_bonus_limit(self):
        snapshot = self.snapshot()
        book = replace(self.market.book("SPCXUSD1"), bid=dec("99.99"), ask=dec("100.01"))
        plan = self.plan(snapshot=snapshot, book=book)
        self.assertGreater(plan.qty, 0)
        self.assertLess(plan.qty, 50)
        def ratio(qty):
            qty = Fraction(qty)
            cost = qty * (Fraction(book.ask) - Fraction(book.bid) + (Fraction(book.ask) + Fraction(book.bid)) * Fraction(snapshot.fees["SPCXUSD1"]))
            return (snapshot.occupied_margin_exact + 2 * qty * Fraction(book.ask) / 10) / (Fraction(snapshot.equity) - cost)
        self.assertLessEqual(ratio(plan.qty), Fraction(95, 100))
        self.assertGreater(ratio(plan.qty + self.market.rules["SPCXUSD1"].step), Fraction(95, 100))

    def test_bonus_does_not_bypass_balance_depth_capacity_spread_or_minimum_batch(self):
        snapshot = self.snapshot()
        book = self.market.book("SPCXUSD1")
        for changed_snapshot, changed_book, capacities in (
            (replace(snapshot, available=dec(99)), book, None),
            (snapshot, replace(book, bid_qty=dec(4)), None),
            (snapshot, book, {10: dec(10000)}),
            (snapshot, replace(book, ask=dec(101)), None),
            (replace(snapshot, equity=dec(19000)), book, None),
        ):
            with self.subTest(available=changed_snapshot.available, equity=changed_snapshot.equity, bid_qty=changed_book.bid_qty, capacities=capacities, ask=changed_book.ask):
                self.assertEqual(self.plan(snapshot=changed_snapshot, book=changed_book, capacities=capacities).qty, 0)

    def test_shared_space_does_not_stack_across_ten_twenty_or_markets(self):
        first = self.plan()
        self.assertEqual(first.projected_ratio, dec(".95"))
        for side in ("LONG", "SHORT"):
            self.broker.state["positions"]["SPCXUSD1:" + side] = {"qty": str(first.qty), "entry": "100"}
        snapshot = self.snapshot(fees=False)
        self.assertEqual(snapshot.ratio, dec(".95"))
        for symbol in SYMBOLS:
            self.assertEqual(self.plan(symbol, snapshot).qty, 0)

    def test_repeated_actual_fills_share_limit_and_low_leverage_cannot_use_rest(self):
        self.policy["order_notional"] = "1000"
        self.f.store.save_account(self.account)
        opened = 0
        for index in range(20):
            symbol = "SPCXUSD1" if index % 2 == 0 else "CLUSD1"
            snapshot = self.snapshot()
            plan = self.plan(symbol, snapshot)
            if not plan.qty:
                break
            Executor(self.f.store, self.broker, self.market).open_pair(self.account, snapshot, symbol, plan, self.market.book(symbol))
            after = self.snapshot()
            self.assertFalse(self.engine().check_post_fill_occupancy(self.account, after))
            self.assertFalse(after.margin_exceeds(".95"))
            self.assertEqual(self.plan("XAUUSD1", after).qty, 0)
            opened += 1
        self.assertGreater(opened, 3)
        self.assertLess(opened, 20)
        self.assertGreater(self.snapshot().ratio, dec(".94"))
        self.assertEqual(self.plan(snapshot=self.snapshot()).qty, 0)
        self.assertTrue(self.f.store.account("test")["enabled"])

    def test_upgrade_must_be_confirmed_before_bonus_opening(self):
        self.broker.state["leverages"]["SPCXUSD1"] = 2
        self.broker.save()
        engine = self.engine()
        broker = self.broker
        class DelayedBroker:
            # Model eventual external confirmation, without the synchronous
            # PaperBroker-only recovery shortcut for a known absent write.
            def __getattr__(self, name):
                return getattr(broker, name)
        engine.brokers["test"] = DelayedBroker()
        with patch.object(self.broker, "set_leverage", return_value={"symbol": "SPCXUSD1", "leverage": 10}) as change, \
             patch.object(self.broker, "submit", wraps=self.broker.submit) as submit:
            engine.tick_account("test")
            engine.tick_account("test")
            change.assert_called_once_with("SPCXUSD1", 10)
            submit.assert_not_called()
            self.assertEqual(self.f.store.intent("test")["kind"], "leverage")
            self.broker.state["leverages"]["SPCXUSD1"] = 10
            self.broker.save()
            engine.tick_account("test")
            submit.assert_not_called()
            engine.tick_account("test")
            submit.assert_called_once()
        self.assertGreater(self.snapshot().ratio, dec(".9"))
        self.assertFalse(self.snapshot().margin_exceeds(".95"))
        self.assertTrue(self.f.store.account("test")["enabled"])

    def test_immediate_and_restarted_completed_or_pending_batch_keep_bonus_context(self):
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                for symbol in ("SPCXUSD1", "CLUSD1"):
                    for side in ("LONG", "SHORT"):
                        self.broker.state["positions"][symbol + ":" + side] = {"qty": "0", "entry": "0"}
                self.broker.state["wallet"] = "20000"
                self.broker.save()
                snapshot = self.snapshot()
                executor = Executor(self.f.store, self.broker, self.market)
                plan = self.plan(snapshot=snapshot)
                if interrupted:
                    with patch.object(executor, "reconcile", return_value="process stopped after submission"):
                        executor.open_pair(self.account, snapshot, "SPCXUSD1", plan, self.market.book("SPCXUSD1"))
                else:
                    executor.open_pair(self.account, snapshot, "SPCXUSD1", plan, self.market.book("SPCXUSD1"))
                    self.assertEqual(self.f.store.get("post_fill_check:test"), {"symbol": "SPCXUSD1", "leverage": 10})
                self.assertFalse(self.engine().check_post_fill_occupancy(self.account, self.snapshot()))
                if not interrupted:
                    self.f.store.put("post_fill_check:test", {"symbol": "SPCXUSD1", "leverage": 10})
                restored = Engine(Store(self.f.store.path), market=self.market)
                restored.brokers["test"] = self.broker
                with patch.object(self.broker, "submit", side_effect=AssertionError("must not resubmit")):
                    restored.tick_account("test")
                self.assertTrue(self.f.store.account("test")["enabled"])
                self.assertIsNone(self.f.store.intent("test"))
                self.assertIsNone(self.f.store.get("post_fill_check:test"))

    def test_post_fill_exact_limit_allowed_but_tiny_excess_pauses(self):
        snapshot = self.snapshot()
        for position in snapshot.pair("XAUUSD1"):
            position.qty = dec(475)
        engine = self.engine()
        marker = {"symbol": "SPCXUSD1", "leverage": 10}
        self.f.store.put("post_fill_check:test", marker)
        self.assertEqual(snapshot.ratio, dec(".95"))
        self.assertFalse(engine.check_post_fill_occupancy(self.account, snapshot))
        self.f.store.put("post_fill_check:test", marker)
        snapshot.equity = dec("19999.999999999999999999999999999999999999")
        self.assertEqual(snapshot.ratio, dec(".95"))
        self.assertTrue(engine.check_post_fill_occupancy(self.account, snapshot))
        self.assertFalse(self.f.store.account("test")["enabled"])

    def test_legacy_or_low_batch_and_changed_actual_leverage_cannot_inherit_bonus(self):
        for marker, actual in ((True, 10), ({"symbol": "SPCXUSD1", "leverage": 4}, 10),
                               ({"symbol": "SPCXUSD1", "leverage": 10}, 5),
                               ({"symbol": "SPCXUSD1", "leverage": 10}, 15)):
            with self.subTest(marker=marker, actual=actual):
                snapshot = self.snapshot()
                for position in snapshot.pair("XAUUSD1"):
                    position.qty = dec(460)
                for position in snapshot.pair("SPCXUSD1"):
                    position.leverage = actual
                self.f.store.put("post_fill_check:test", marker)
                self.assertTrue(self.engine().check_post_fill_occupancy(self.account, snapshot))

    def test_missing_actual_pair_preserves_durable_risk_check(self):
        snapshot = self.snapshot()
        snapshot.positions = [p for p in snapshot.positions if p.symbol != "SPCXUSD1" or p.side == "LONG"]
        marker = {"symbol": "SPCXUSD1", "leverage": 10}
        self.f.store.put("post_fill_check:test", marker)
        with self.assertRaises(TradingError):
            self.engine().check_post_fill_occupancy(self.account, snapshot)
        self.assertEqual(self.f.store.get("post_fill_check:test"), marker)

    def test_complete_and_abort_commit_risk_context_atomically(self):
        for terminal in ("complete", "aborted"):
            intent = {"id": terminal, "account_id": "test", "kind": "pair", "status": "pending", "symbol": "CLUSD1", "leverage": 20}
            self.f.store.save_intent(intent)
            def finish():
                if terminal == "complete":
                    self.f.store.complete_pair(intent, {"long_qty": "5", "short_qty": "5", "notional": "1000"})
                else:
                    self.f.store.abort_pair(intent)
            with self.f.store.connect() as db:
                db.execute("CREATE TRIGGER fail_bonus_marker BEFORE INSERT ON kv WHEN NEW.key='post_fill_check:test' BEGIN SELECT RAISE(ABORT, 'storage unavailable'); END")
            with self.assertRaises(sqlite3.IntegrityError):
                finish()
            self.assertEqual(self.f.store.intent("test")["status"], "pending")
            with self.f.store.connect() as db:
                db.execute("DROP TRIGGER fail_bonus_marker")
            finish()
            self.assertIsNone(self.f.store.intent("test"))
            self.assertEqual(Store(self.f.store.path).get("post_fill_check:test"), {"symbol": "CLUSD1", "leverage": 20})

    def test_state_returns_independent_account_limits_and_keeps_saved_base(self):
        other = account("other")
        other["policy"]["margin_limit"] = ".98"
        self.f.store.save_account(other)
        state = {row["id"]: row for row in self.engine().state()["accounts"]}
        self.assertEqual(state["test"]["risk_limits"], {"base": ".9", "high_leverage": "0.95", "migration": "0.95"})
        self.assertEqual(state["other"]["risk_limits"], {"base": ".98", "high_leverage": "1", "migration": "1"})
        self.assertEqual(self.f.store.account("test")["policy"]["margin_limit"], ".9")

    def test_campaign_above_base_waits_for_shared_limit_or_idle_timeout(self):
        self.policy["order_notional"] = "1000"
        self.f.store.save_account(self.account)
        engine = self.engine()
        engine.tick_account("test")
        self.assertGreater(self.snapshot().ratio, dec(".9"))
        engine.markets["SPCXUSD1"]["capacities"] = {}
        engine.tick_account("test")
        campaign = self.f.store.get("campaign:test")
        self.assertIsNotNone(campaign)
        campaign["last_fill_at"] = time.time() - 61
        self.f.store.put("campaign:test", campaign)
        engine.tick_account("test")
        self.assertIsNone(self.f.store.get("campaign:test"))


if __name__ == "__main__":
    unittest.main()
