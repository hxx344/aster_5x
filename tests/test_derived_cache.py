"""Reused calculations retain exact ledger, clock and stream boundaries."""
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from unittest import TestCase
from unittest.mock import patch

from tests.test_cycle_volume import CycleVolumeTests, DAY
from tests.test_depth_stream import connected_cache, synchronized, event, SYMBOL, NOW
from trading.cycle import _depth_sweeps
from trading.cycle_cost import calculate_cycle_costs
from trading.engine import Engine
from trading.models import TradingError
from trading.paper import DemoMarket
from trading.store import Store


class LedgerReuseTests(TestCase):
    setUp = CycleVolumeTests.setUp
    intent = CycleVolumeTests.intent
    fill = CycleVolumeTests.fill

    def test_repeated_checks_read_revision_without_rescanning_fills(self):
        intent = self.intent()
        self.store.record_cycle_fills(intent, [self.fill(intent, executed_at=DAY)])
        first = self.store.cycle_rolling_volume("first", DAY + 10)
        statements, connect = [], self.store.connect
        @contextmanager
        def traced():
            with connect() as db:
                db.set_trace_callback(statements.append)
                yield db
        with patch.object(self.store, "connect", traced):
            for second in range(11, 20):
                current = self.store.cycle_rolling_volume("first", DAY + second)
                self.assertEqual(current["volume"], first["volume"])
                self.assertEqual(current["window_end"], DAY + second)
                current["volume"] = "corrupted caller copy"
        self.assertFalse(any("FROM cycle_fills " in sql for sql in statements))
        self.assertEqual(sum("SELECT revision" in sql for sql in statements), 9)

    def test_other_store_late_fill_and_rollback_invalidate_only_committed_data(self):
        intent = self.intent(quantity="3", filled="3")
        self.store.record_cycle_fills(intent, [self.fill(intent, "later", executed_at=DAY + 5)])
        self.assertEqual(self.store.cycle_rolling_volume("first", DAY + 10)["volume"], "100")
        other = Store(self.path)
        before = self.store.cycle_fill_revision("first")
        try:
            with other.connect() as db:
                db.execute("UPDATE cycle_fills SET notional='900' WHERE account_id='first'")
                raise RuntimeError("rollback")
        except RuntimeError:
            pass
        self.assertEqual(self.store.cycle_fill_revision("first"), before)
        self.assertEqual(self.store.cycle_rolling_volume("first", DAY + 10)["volume"], "100")
        other.record_cycle_fills(intent, [self.fill(intent, "late", executed_at=DAY)])
        self.assertGreater(self.store.cycle_fill_revision("first"), before)
        self.assertEqual(self.store.cycle_rolling_volume("first", DAY + 11)["volume"], "200")
        with other.connect() as db:
            db.execute("DELETE FROM cycle_fills WHERE trade_id='late'")
        self.assertEqual(self.store.cycle_rolling_volume("first", DAY + 12)["volume"], "100")

    def test_future_fill_expiry_and_clock_reversal_are_not_cached_past_boundaries(self):
        intent = self.intent(quantity="2", filled="2")
        self.store.record_cycle_fills(intent, [self.fill(intent, "old", executed_at=DAY - 86399),
                                              self.fill(intent, "future", executed_at=DAY + 2)])
        self.assertEqual(self.store.cycle_rolling_volume("first", DAY)["volume"], "100")
        self.assertEqual(self.store.cycle_rolling_volume("first", DAY + 1)["volume"], "0")
        self.assertEqual(self.store.cycle_rolling_volume("first", DAY + 2)["volume"], "100")
        self.assertEqual(self.store.cycle_rolling_volume("first", DAY - .5)["volume"], "100")
        self.assertEqual(self.store.cycle_rolling_volume("second", DAY)["volume"], "0")

    def test_old_read_snapshot_cannot_relabel_newer_ledger_totals(self):
        intent = self.intent(quantity="2", filled="2")
        self.store.record_cycle_fills(intent, [self.fill(intent, "first", executed_at=DAY)])
        other = Store(self.path)
        with self.store.read_snapshot() as old:
            old.cycle_fill_revision("first")  # Establish the old WAL view.
            other.record_cycle_fills(intent, [self.fill(intent, "second", executed_at=DAY + 1)])
            self.assertEqual(self.store.cycle_rolling_volume("first", DAY + 2)["volume"], "200")
            self.assertEqual(old.cycle_rolling_volume("first", DAY + 2)["volume"], "100")
        self.assertEqual(self.store.cycle_rolling_volume("first", DAY + 2)["volume"], "200")

    def test_reports_reuse_costs_but_recheck_backlog_and_clock_boundaries(self):
        intent = self.intent(quantity="1", filled="1")
        self.store.record_cycle_fills(intent, [self.fill(intent, executed_at=DAY - 1)])
        engine = Engine(self.store, market=DemoMarket())
        owner = self.store.account("first")
        with patch("trading.engine.calculate_cycle_costs", wraps=calculate_cycle_costs) as costs:
            first = engine._load_dashboard_report(owner, DAY - .5)
            engine._load_dashboard_report(owner, DAY - .25)
            self.assertEqual(costs.call_count, 1)
            midnight = engine._load_dashboard_report(owner, DAY)
            self.assertEqual(costs.call_count, 2)
            self.assertEqual(midnight["costs"]["daily"]["taker_fee"], "0")
            self.assertEqual(first["costs"]["daily"]["taker_fee"], "0.0125")
            self.store.mark_cycle_volume_synced(intent["id"])
            current = engine._load_dashboard_report(owner, DAY + 1)
            self.assertFalse(current["volumes"]["XAUUSD1"]["daily_volume"]["sync_pending"])
            self.assertEqual(costs.call_count, 2)
            current["trades"].clear()
            self.assertEqual(len(engine._load_dashboard_report(owner, DAY + 2)["trades"]), 1)
            expired = engine._load_dashboard_report(owner, DAY + 86399)
            self.assertEqual(expired["costs"]["rolling"]["taker_fee"], "0")
            self.assertEqual(costs.call_count, 3)

    def test_legacy_account_aggregate_is_removed_without_losing_fills(self):
        intent = self.intent()
        self.store.record_cycle_fills(intent, [self.fill(intent, executed_at=DAY)])
        with self.store.connect() as db:
            db.execute("CREATE TABLE cycle_volume_days(account_id TEXT, volume TEXT)")
            db.execute("INSERT INTO cycle_volume_days VALUES ('first','999')")
        migrated = Store(self.path)
        self.assertEqual(migrated.cycle_daily_volume("first", DAY)["volume"], "100")
        self.assertEqual(len(migrated.cycle_trade_records("first")), 1)
        with migrated.connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='cycle_volume_days'").fetchone())


class DepthReuseTests(TestCase):
    def test_repeated_read_reuses_immutable_snapshot_and_prefixes(self):
        stream, _, _ = connected_cache()
        depth = synchronized(stream)
        self.assertIs(stream.snapshot(SYMBOL), depth)
        self.assertIs(depth.sweeps, depth.sweeps)
        with self.assertRaises(FrozenInstanceError):
            depth.sweeps[0].quantity = 999
        with self.assertRaises(TypeError):
            depth.sweeps[0].levels[0] = (1, 2)
        stream._handle_message(event(U=102, u=103, pu=101, b=[["100", "9"]]))
        latest = stream.snapshot(SYMBOL)
        self.assertIsNot(latest, depth)
        self.assertEqual(depth.bids[0][1], 3)
        self.assertEqual(latest.bids[0][1], 9)
        self.assertNotEqual(latest.sweeps[0].quantity, depth.sweeps[0].quantity)

    def test_cached_prefixes_do_not_bypass_expiry_or_sequence_revocation(self):
        stream, _, _ = connected_cache()
        depth = synchronized(stream)
        with patch("trading.depth.time.monotonic", return_value=100):
            _depth_sweeps(depth, NOW)
            with self.assertRaises(TradingError):
                _depth_sweeps(depth, NOW + 4)
            stream._handle_message(event(U=105, u=106, pu=104))
            self.assertIsNone(stream.snapshot(SYMBOL))
            with self.assertRaises(TradingError):
                _depth_sweeps(depth, NOW)
