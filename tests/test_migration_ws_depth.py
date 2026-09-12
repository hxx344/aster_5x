"""Shared depth admission checks using an isolated paper ledger only."""
from dataclasses import replace
from fractions import Fraction
import threading
import time
from unittest import TestCase
from unittest.mock import patch

from tests.helpers import Fixture
from trading.depth import DEPTH_MAX_AGE, DepthSnapshot
from trading.engine import Engine
from trading.migration import DEFAULT_MIGRATION, plan_migration
from trading.models import SYMBOLS, TradingError, dec, wire


SOURCE, TARGET, OTHER = "XAUUSD1", "SPCXUSD1", "CLUSD1"


class SharedDepthValidityTests(TestCase):
    def test_revocation_invalidates_an_already_returned_snapshot(self):
        connected = threading.Event()
        connected.set()
        snapshot = DepthSnapshot(
            ((Fraction(99), Fraction(100)),),
            ((Fraction(100), Fraction(100)),),
            time.time(), monotonic_timestamp=time.monotonic(),
            validity=connected.is_set,
        )
        snapshot.require_fresh()
        connected.clear()
        with self.assertRaises(TradingError):
            snapshot.require_fresh()

    def test_monotonic_expiry_cannot_be_reset_by_a_recent_wall_timestamp(self):
        snapshot = DepthSnapshot(
            ((Fraction(99), Fraction(100)),),
            ((Fraction(100), Fraction(100)),),
            100, monotonic_timestamp=1000,
        )
        with patch("trading.depth.time.monotonic", return_value=1000 + DEPTH_MAX_AGE):
            self.assertEqual(snapshot.age(100), DEPTH_MAX_AGE)
            snapshot.require_fresh(100)
        with patch("trading.depth.time.monotonic", return_value=1000 + DEPTH_MAX_AGE + .001):
            with self.assertRaises(TradingError):
                snapshot.require_fresh(100)


class SharedDepthMigrationTests(TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        account = self.f.store.account("test")
        account.update(migration={**DEFAULT_MIGRATION, "enabled": True},
                       migration_run_id="shared-depth-run")
        self.f.store.save_account(account)
        for side in ("LONG", "SHORT"):
            self.f.broker.state["positions"][SOURCE + ":" + side] = {
                "qty": "1", "entry": wire(self.f.market.book(SOURCE).mark),
            }
        self.f.store.put("paper:test", self.f.broker.state)
        self.engine = Engine(self.f.store, demo=True, market=self.f.market)
        self.engine.brokers["test"] = self.f.broker
        for symbol in SYMBOLS:
            self.engine.poll_market(symbol)
        # One eligible target makes the final admission decision deterministic.
        self.engine.markets[OTHER]["capacities"]["5"] = "0"
        self.connected = threading.Event()
        self.connected.set()
        self.depths = {
            symbol: replace(self.f.market.depth(symbol),
                            monotonic_timestamp=time.monotonic(),
                            validity=self.connected.is_set)
            for symbol in (SOURCE, TARGET)
        }
        self.reads = []
        self.original_snapshot = self.f.broker.snapshot

    def read_depth(self, symbol):
        snapshot = self.depths[symbol]
        self.reads.append((symbol, snapshot))
        snapshot.require_fresh()
        return snapshot

    def read_account_then(self, callback):
        def snapshot(symbols, fresh_modes=False):
            result = self.original_snapshot(symbols, fresh_modes=fresh_modes)
            if fresh_modes:
                callback()
            return result
        return snapshot

    def assert_no_opening(self):
        self.assertEqual(self.f.broker.state["orders"], {})
        self.assertIsNone(self.f.store.intent("test"))
        self.assertIsNone(self.f.store.get("post_fill_check:test"))
        self.assertEqual(self.f.broker.state["positions"][SOURCE + ":LONG"]["qty"], "1")
        self.assertTrue(self.f.store.account("test")["enabled"])

    def test_disconnect_during_account_refresh_blocks_saved_depth_before_submission(self):
        held = self.depths[SOURCE]
        with patch.object(self.f.market, "depth", side_effect=self.read_depth), \
             patch.object(self.f.broker, "snapshot",
                          side_effect=self.read_account_then(self.connected.clear)), \
             patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            self.engine.tick_account("test")
        self.assertFalse(self.connected.is_set())
        with self.assertRaises(TradingError):
            held.require_fresh()
        submit.assert_not_called()
        self.assert_no_opening()
        self.assertGreaterEqual(sum(symbol == SOURCE for symbol, _ in self.reads), 2)

    def test_changed_shared_depth_is_replanned_before_creating_an_intent(self):
        original = self.depths[TARGET]
        reduced = replace(original,
                          bids=((original.bids[0][0], Fraction(4, 5)),),
                          asks=((original.asks[0][0], Fraction(4, 5)),))
        def shrink_target():
            self.depths[TARGET] = reduced
        with patch.object(self.f.market, "depth", side_effect=self.read_depth), \
             patch.object(self.f.broker, "snapshot",
                          side_effect=self.read_account_then(shrink_target)), \
             patch("trading.engine.plan_migration", wraps=plan_migration) as planner, \
             patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            self.engine.tick_account("test")
        submit.assert_not_called()
        self.assert_no_opening()
        self.assertTrue(any(call.args[5] is original for call in planner.call_args_list))
        self.assertTrue(any(call.args[5] is reduced for call in planner.call_args_list))
        self.assertIn("重新", self.engine.state()["accounts"][0]["migration_state"]["reason"])

    def test_new_shared_snapshots_with_the_same_legal_plan_allow_migration(self):
        previous = self.depths.copy()
        def refresh_depths():
            self.depths.update({
                symbol: replace(snapshot, timestamp=time.time(),
                                monotonic_timestamp=time.monotonic())
                for symbol, snapshot in previous.items()
            })
        with patch.object(self.f.market, "depth", side_effect=self.read_depth), \
             patch.object(self.f.broker, "snapshot",
                          side_effect=self.read_account_then(refresh_depths)), \
             patch("trading.engine.plan_migration", wraps=plan_migration) as planner, \
             patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            self.engine.tick_account("test")
        self.assertTrue(submit.called)
        self.assertIsNone(self.f.store.intent("test"))
        self.assertEqual(self.f.store.get("migration:test")["completed_batches"], 1)
        self.assertTrue(any(call.args[4] is self.depths[SOURCE]
                            and call.args[5] is self.depths[TARGET]
                            for call in planner.call_args_list))
        self.assertTrue(all(self.depths[symbol] is not previous[symbol] for symbol in previous))

    def test_migration_rejects_depth_over_three_monotonic_seconds_even_if_display_accepts(self):
        self.depths = {symbol: replace(snapshot, timestamp=time.time(), monotonic_timestamp=100)
                       for symbol, snapshot in self.depths.items()}
        with patch("trading.depth.time.monotonic", return_value=103.001), \
             patch.object(self.f.market, "depth", side_effect=self.read_depth), \
             patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            for snapshot in self.depths.values():
                snapshot.require_fresh()
                self.assertGreater(snapshot.age(time.time()), 3)
            self.engine.tick_account("test")
        submit.assert_not_called()
        self.assert_no_opening()
        self.assertIn("已过期", self.engine.state()["accounts"][0]["migration_state"]["reason"])

    def test_migration_accepts_the_exact_three_monotonic_second_boundary(self):
        self.depths = {symbol: replace(snapshot, timestamp=time.time(), monotonic_timestamp=100)
                       for symbol, snapshot in self.depths.items()}
        with patch("trading.depth.time.monotonic", return_value=103), \
             patch.object(self.f.market, "depth", side_effect=self.read_depth), \
             patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            self.engine.tick_account("test")
        self.assertTrue(submit.called)
        self.assertIsNone(self.f.store.intent("test"))
        self.assertEqual(self.f.store.get("migration:test")["completed_batches"], 1)
        self.assertLess(dec(self.f.store.get("migration:test")["source_remaining_qty"]["LONG"]), 1)
