"""Independent regression cases for transient recovery and reduction priority."""
from dataclasses import replace
from datetime import datetime, timezone
import sqlite3
import time
from unittest import TestCase
from unittest.mock import patch

from tests import test_pair_trading as fixtures
from trading.exchange import ExchangeError, RequestNotSent
from trading.models import TradingError, dec
from trading.pair_execution import PairTrader, runtime_default


class PairReviewTests(TestCase):
    setUp = fixtures.PairTradingTests.setUp
    tick = fixtures.PairTradingTests.tick
    snapshots = fixtures.PairTradingTests.snapshots
    expire = fixtures.PairTradingTests.expire
    assert_flat = fixtures.PairTradingTests.assert_flat

    def test_batch_history_write_failure_keeps_recoverable_pending(self):
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER reject_pair_history BEFORE INSERT ON kv "
                       "WHEN NEW.key LIKE 'pair_batch:%' "
                       "BEGIN SELECT RAISE(ABORT, 'simulated disk write failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.tick()
        state = self.store.get("pair_runtime:gold")
        self.assertIsNotNone(state["pending"])
        self.assertEqual(state["progress"]["phase"], "waiting_open")
        self.assertEqual(state["daily_volume"], {})
        batch_id = state["pending"]["id"]
        self.assertIsNone(self.store.get("pair_batch:" + batch_id))
        count = sum(len(b.state["orders"]) for b in self.brokers.values())
        with self.store.connect() as db:
            db.execute("DROP TRIGGER reject_pair_history")
        state = self.tick()
        self.assertIsNone(state["pending"])
        self.assertEqual(state["phase"], "holding")
        self.assertTrue(self.store.get("pair_batch:" + batch_id)["completed"])
        self.assertEqual(sum(len(b.state["orders"]) for b in self.brokers.values()), count)

    def test_revoked_recovery_snapshot_preserves_pending_until_fresh_read(self):
        original = self.trader._read
        reads = 0

        def read(brokers, **kwargs):
            nonlocal reads
            reads += 1
            snapshots, guards = original(brokers, **kwargs)
            if reads == 2:
                guards["long"] = lambda: (_ for _ in ()).throw(TradingError("账户快照已撤销"))
            return snapshots, guards

        with patch.object(self.trader, "_read", side_effect=read):
            state = self.tick()
        self.assertIsNotNone(state["pending"])
        self.assertEqual(state["progress"]["phase"], "waiting_open")
        self.assertNotIn("last_batch", state)
        count = sum(len(b.state["orders"]) for b in self.brokers.values())
        state = self.tick()
        self.assertIsNone(state["pending"])
        self.assertEqual(state["phase"], "holding")
        self.assertEqual(sum(len(b.state["orders"]) for b in self.brokers.values()), count)

    def test_revoked_leverage_confirmation_preserves_pending(self):
        state = runtime_default()
        _, _, identities = self.trader._members(self.pair)
        pending = {"kind": "leverage", "identities": identities,
                   "before": {"LONG": "0", "SHORT": "0"}, "target_leverage": 5,
                   "previous_leverage": 2, "results": {}}
        state["pending"] = pending
        snapshots, guards = self.trader._read(self.brokers)
        guards["short"] = lambda: (_ for _ in ()).throw(TradingError("账户快照已撤销"))
        with patch.object(self.trader, "_read", return_value=(snapshots, guards)):
            with self.assertRaisesRegex(TradingError, "已撤销"):
                self.trader._recover_leverage(self.pair, state, self.brokers)
        self.assertIs(state["pending"], pending)
        self.trader._recover_leverage(self.pair, state, self.brokers)
        self.assertIsNone(self.store.get("pair_runtime:gold")["pending"])

    def test_transient_position_read_lag_does_not_strand_verified_cycle(self):
        original_read = self.trader._read
        reads = 0

        def read(brokers, **kwargs):
            nonlocal reads
            reads += 1
            snapshots, guards = original_read(brokers, **kwargs)
            if reads == 2:
                snapshots = {key: replace(snapshot, positions=[replace(p, qty=dec(0)) for p in snapshot.positions])
                             for key, snapshot in snapshots.items()}
            return snapshots, guards

        with patch.object(self.trader, "_read", side_effect=read):
            state = self.tick()
        self.assertIsNotNone(state["pending"])
        state = self.tick()
        self.assertIsNone(state["pending"])
        self.assertEqual(state["phase"], "holding")
        self.expire()
        state = self.tick()
        self.assertEqual(state["progress"]["completed_cycles"], 1, state)
        self.assert_flat()

    def test_nonpending_transfer_validation_failure_does_not_block_cycle_reduction(self):
        state = self.tick()
        self.assertEqual(state["phase"], "holding")
        self.expire()
        pair = self.store.pair("gold")
        pair["margin"]["enabled"] = True
        self.store.save_pair(pair)
        self.margin.stop()
        with patch("trading.margin_balance.MarginBalancer._check_snapshots", side_effect=TradingError("划转资格检查失败")):
            state = self.tick()
        self.assertEqual(state["progress"]["completed_cycles"], 1, state)
        self.assert_flat()

    def test_local_unsent_repairs_do_not_exhaust_exchange_recovery_attempts(self):
        original = self.brokers["long"].submit

        def unavailable(orders):
            if orders[0]["side"] == "SELL":
                raise RequestNotSent("本地额度不足，未发送")
            return original(orders)

        with patch.object(self.brokers["long"], "submit", side_effect=unavailable), \
             patch.object(self.brokers["short"], "submit", side_effect=ExchangeError("余额不足", code=-2019)):
            for _ in range(4):
                runtime = self.store.get("pair_runtime:gold")
                if runtime and runtime.get("pending"):
                    runtime["pending"]["repair_retry_at"] = time.time() - 1
                    self.store.put("pair_runtime:gold", runtime)
                self.tick()
        for _ in range(3):
            runtime = self.store.get("pair_runtime:gold")
            if runtime and runtime.get("pending"):
                runtime["pending"]["repair_retry_at"] = time.time() - 1
                self.store.put("pair_runtime:gold", runtime)
            state = self.tick()
            if not state["pending"]:
                break
        self.assertIsNone(state["pending"], state)
        self.assert_flat()

    def test_failed_opening_rolls_back_then_waits_before_new_fee_bearing_attempt(self):
        with patch.object(self.brokers["short"], "submit", side_effect=ExchangeError("余额不足", code=-2019)):
            self.tick()
            state = self.tick()
            self.assertIsNone(state["pending"])
            original_count = len(self.brokers["long"].state["orders"])
            self.tick()
        self.assertEqual(len(self.brokers["long"].state["orders"]), original_count)
        self.assert_flat()

    def test_cross_midnight_volume_uncertainty_blocks_only_affected_utc_day(self):
        now = datetime.now(timezone.utc)
        boundary = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        instant = boundary + 2

        class FrozenUTC(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromtimestamp(instant, tz)

        state = runtime_default()
        pending = {"created_at": boundary - 1, "legs": [{"key": "long", "receipt": {
            "executedQty": "1", "avgPrice": "100", "updateTime": (boundary + 1) * 1000}}], "repairs": []}
        with patch("trading.pair_execution.datetime", FrozenUTC), patch("trading.pair_execution.time.time", return_value=instant):
            PairTrader._account_volume(state, pending)
        self.assertTrue(state["volume_unknown"])
        pair = self.store.pair("gold")
        pair["cycle"]["daily_volume_limit"] = "1000"
        pair = self.store.save_pair(pair)
        with self.assertRaisesRegex(TradingError, "尚未核实"):
            PairTrader._daily_remaining(pair, state)
        self.store.put("pair_runtime:gold", state)
        instant += 86400
        with patch("trading.pair_execution.datetime", FrozenUTC):
            state = self.tick()
        self.assertFalse(state.get("volume_unknown"), state)
        self.assertEqual(state["phase"], "holding", state)

    def test_uncertain_daily_volume_does_not_block_tracked_position_close(self):
        state = self.tick()
        state.update(volume_unknown=True, volume_unknown_until_utc="9999-12-31")
        self.store.put("pair_runtime:gold", state)
        self.expire()
        pair = self.store.pair("gold")
        pair["cycle"]["daily_volume_limit"] = "1000"
        self.store.save_pair(pair)
        state = self.tick()
        self.assertEqual(state["progress"]["completed_cycles"], 1, state)
        self.assert_flat()
