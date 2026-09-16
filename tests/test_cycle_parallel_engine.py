"""A cycle owns one market while ordinary work shares the account safely."""
from copy import deepcopy
import time
from unittest import TestCase
from unittest.mock import patch

from tests.helpers import Fixture, account
from trading.cycle_execution import CycleExecutor
from trading.engine import Engine
from trading.execution import Executor
from trading.models import AccountModeError, SYMBOLS, dec, plan_pair


class CycleParallelEngineTests(TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        owner = self.f.store.account("test")
        owner["enabled"] = False
        owner["policy"]["symbols"] = list(SYMBOLS)
        self.f.store.save_account(owner)
        self.engine = Engine(self.f.store, market=self.f.market)
        self.engine.brokers["test"] = self.f.broker
        self.capacities()

    def capacities(self, available=None):
        for symbol in SYMBOLS:
            self.engine.markets[symbol] = {
                "status": "ok", "checked_at": time.time(),
                "capacities": {"2": "1000000", **{str(tier): str((available or {}).get(symbol, {}).get(tier, 0))
                               for tier in (5, 10, 20)}},
            }

    def start(self, **changes):
        self.engine.configure("test", {"cycle": {"enabled": True, "max_notional": "1000",
                                                   "spread_notional": "1000", **changes}})
        self.f.broker.set_cycle_leverage("XAUUSD1", 2)
        self.engine.enable("test", True)
        self.engine.view("test", snapshot={"timestamp": time.time(), "positions": [
            {"symbol": "XAUUSD1", "leverage": 2}]})
        return self.f.store.account("test")

    def holding(self):
        self.start()
        self.engine.tick_account("test")
        progress = self.f.store.get("cycle:test")
        self.assertEqual(progress["phase"], "holding")
        self.assertIsNone(self.f.store.intent("test"))
        return progress

    def pair(self, symbol):
        return self.f.broker.snapshot(list(SYMBOLS)).pair(symbol)

    def assert_pair_filled(self, symbol):
        long, short = self.pair(symbol)
        self.assertGreater(long.qty, 0)
        self.assertEqual(long.qty, short.qty)

    def test_holding_cycle_allows_actual_additions_in_both_other_markets(self):
        progress = self.holding()
        cycle_orders = deepcopy(self.f.broker.state["orders"])
        for symbol in ("SPCXUSD1", "CLUSD1"):
            self.capacities({symbol: {5: 500000}, "XAUUSD1": {5: 500000, 10: 500000, 20: 500000}})
            self.engine.tick_account("test")
            self.assert_pair_filled(symbol)
            self.assertIsNone(self.f.store.intent("test"))
        self.assertEqual(tuple(p.qty for p in self.pair("XAUUSD1")),
                         (dec(progress["quantities"]["LONG"]), dec(progress["quantities"]["SHORT"])))
        self.assertEqual([p.leverage for p in self.pair("XAUUSD1")], [2, 2])
        self.assertEqual([row for row in self.f.broker.state["orders"].values() if row["symbol"] == "XAUUSD1"],
                         list(cycle_orders.values()))
        self.assertEqual(self.f.store.account("test")["policy"]["symbols"], list(SYMBOLS))
        self.assertEqual({p["symbol"] for p in self.engine.views["test"]["snapshot"]["positions"]}, set(SYMBOLS))

    def test_cycle_spread_wait_does_not_block_other_market_fills(self):
        self.start(spread_limit_bp="0")
        self.capacities({"SPCXUSD1": {5: 500000}})
        self.engine.tick_account("test")
        self.assert_pair_filled("SPCXUSD1")
        self.assertEqual(tuple(p.qty for p in self.pair("XAUUSD1")), (0, 0))
        self.assertEqual(self.engine.views["test"]["cycle_state"]["phase"], "waiting_open")
        self.assertIn("价差", self.engine.views["test"]["cycle_state"]["reason"])
        self.assertTrue(self.f.store.account("test")["enabled"])
        checks = [event for event in self.f.store.events() if event["kind"] == "cycle_check"]
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["cycle_check"]["symbol"], "XAUUSD1")
        self.assertEqual(checks[0]["cycle_check"]["count"], 1)

    def test_completed_ordinary_snapshot_refreshes_missing_flat_cycle_market(self):
        owner = self.f.store.account("test")
        owner["policy"]["symbols"] = ["SPCXUSD1"]
        self.f.store.save_account(owner)
        original_snapshot = self.f.broker.snapshot

        def live_style_snapshot(symbols, fresh_modes=False):
            snapshot = original_snapshot(symbols, fresh_modes=fresh_modes)
            # Live account snapshots retain all exposure, but only requested
            # flat markets have explicit LONG and SHORT rows.
            snapshot.positions = [p for p in snapshot.positions if p.qty or p.symbol in symbols]
            return snapshot

        with patch.object(self.f.broker, "snapshot", side_effect=live_style_snapshot) as snapshot_reads:
            self.start(spread_limit_bp="0")
            self.capacities({"SPCXUSD1": {5: 500000}})
            self.engine.tick_account("test")
        self.assert_pair_filled("SPCXUSD1")
        self.assertEqual(tuple(p.qty for p in self.pair("XAUUSD1")), (0, 0))
        self.assertTrue(self.f.store.account("test")["enabled"])
        self.assertIsNone(self.f.store.intent("test"))
        self.assertIsNone(self.f.store.get("post_fill_check:test"))
        self.assertIn(["SPCXUSD1"], [call.args[0] for call in snapshot_reads.call_args_list])
        self.assertEqual(set(snapshot_reads.call_args_list[-1].args[0]), {"XAUUSD1", "SPCXUSD1"})
        state = self.engine.state()["accounts"][0]
        self.assertEqual({(p["symbol"], p["side"]) for p in state["snapshot"]["positions"]},
                         {(symbol, side) for symbol in ("XAUUSD1", "SPCXUSD1") for side in ("LONG", "SHORT")})
        self.assertEqual(state["cycle_state"]["phase"], "waiting_open")
        self.assertIn("价差", state["cycle_state"]["reason"])

    def test_cycle_volume_wait_does_not_block_other_market_fills(self):
        self.start(daily_volume_limit="1")
        self.capacities({"CLUSD1": {5: 500000}})
        self.engine.tick_account("test")
        self.assert_pair_filled("CLUSD1")
        self.assertEqual(tuple(p.qty for p in self.pair("XAUUSD1")), (0, 0))
        self.assertEqual(self.engine.views["test"]["cycle_state"]["phase"], "daily_limit")
        self.assertTrue(self.f.store.account("test")["enabled"])

    def test_selected_cycle_market_cannot_upgrade_or_add_at_any_ordinary_tier(self):
        self.holding()
        before = deepcopy(self.f.broker.state["orders"])
        self.capacities({"XAUUSD1": {5: 500000, 10: 500000, 20: 500000}})
        with patch.object(self.f.broker, "set_leverage", wraps=self.f.broker.set_leverage) as leverage:
            self.engine.tick_account("test")
        leverage.assert_not_called()
        self.assertEqual(self.f.broker.state["orders"], before)
        self.assertEqual([p.leverage for p in self.pair("XAUUSD1")], [2, 2])

    def test_capacity_wakeup_excludes_only_this_accounts_selected_market(self):
        self.start()
        other = account("second")
        other["policy"]["symbols"] = list(SYMBOLS)
        self.f.store.save_account(other)
        for symbol in ("XAUUSD1", "SPCXUSD1"):
            self.engine.wake_capacity_accounts(symbol, {10: dec(500000)}, time.time(), self.f.store.accounts())
        self.assertNotIn("XAUUSD1", self.engine.work("test").priority_levels)
        self.assertNotIn("XAUUSD1", self.engine.work("test").priority)
        self.assertIn("SPCXUSD1", self.engine.work("test").priority_levels)
        self.assertIn("SPCXUSD1", self.engine.work("test").priority)
        self.assertIn("XAUUSD1", self.engine.work("second").priority_levels)
        self.assertIn("XAUUSD1", self.engine.work("second").priority)

    def test_existing_cycle_batch_recovers_before_any_ordinary_work(self):
        owner = self.start()
        with patch.object(CycleExecutor, "reconcile", return_value="process interrupted"):
            self.engine.tick_cycle_account(owner, self.f.broker, None)
        self.assertEqual(self.f.store.intent("test")["kind"], "cycle")
        submitted = deepcopy(self.f.broker.state["orders"])
        self.capacities({"SPCXUSD1": {5: 500000}, "CLUSD1": {5: 500000}})
        self.engine.tick_account("test")
        self.assertIsNone(self.f.store.intent("test"))
        self.assertEqual(self.f.store.get("cycle:test")["phase"], "holding")
        self.assertEqual(self.f.broker.state["orders"], submitted)
        self.assertEqual(tuple(p.qty for p in self.pair("SPCXUSD1")), (0, 0))
        self.assertEqual(tuple(p.qty for p in self.pair("CLUSD1")), (0, 0))

    def test_existing_ordinary_batch_recovers_before_any_new_cycle(self):
        owner = self.start()
        symbol = "SPCXUSD1"
        snapshot = self.f.broker.snapshot(list(SYMBOLS))
        book = self.f.market.book(symbol)
        plan = plan_pair(snapshot, book, self.f.market.rules[symbol], {5: dec(500000)}, owner["policy"])
        self.assertGreater(plan.qty, 0)
        executor = Executor(self.f.store, self.f.broker, self.f.market)
        with patch.object(executor, "reconcile", return_value="process interrupted"):
            executor.open_pair(owner, snapshot, symbol, plan, book)
        self.assertEqual(self.f.store.intent("test")["kind"], "pair")
        submitted = deepcopy(self.f.broker.state["orders"])
        with patch.object(self.engine, "tick_cycle_account", side_effect=AssertionError("pending ordinary batch has priority")):
            self.engine.tick_account("test")
        self.assertIsNone(self.f.store.intent("test"))
        self.assertEqual(self.f.broker.state["orders"], submitted)
        self.assert_pair_filled(symbol)
        self.assertEqual(tuple(p.qty for p in self.pair("XAUUSD1")), (0, 0))
        self.assertIsNone(self.f.store.get("post_fill_check:test"))

    def test_durable_post_fill_risk_check_precedes_new_cycle_orders(self):
        self.start()
        for side in ("LONG", "SHORT"):
            self.f.broker.state["positions"]["CLUSD1:" + side] = {"qty": "1000", "entry": "79.325"}
        self.f.broker.save()
        self.f.store.put("post_fill_check:test", {"symbol": "CLUSD1", "leverage": 5})
        with patch.object(self.engine, "tick_cycle_account", side_effect=AssertionError("durable risk marker has priority")):
            self.engine.tick_account("test")
        self.assertFalse(self.f.store.account("test")["enabled"])
        self.assertIn("成交后保证金", self.f.store.account("test")["pause_reason"])
        self.assertIsNone(self.f.store.get("post_fill_check:test"))
        self.assertEqual(self.f.broker.state["orders"], {})

    def test_ordinary_capacity_wait_does_not_starve_due_cycle_close(self):
        progress = self.holding()
        progress["opened_at"] = time.time() - progress["config"]["hold_seconds"] - 1
        self.f.store.put("cycle:test", progress)
        self.capacities()
        self.engine.tick_account("test")
        self.assertEqual(self.f.store.get("cycle:test")["completed_cycles"], 1)
        self.assertEqual(tuple(p.qty for p in self.pair("XAUUSD1")), (0, 0))
        self.assertEqual(tuple(p.qty for p in self.pair("SPCXUSD1")), (0, 0))
        self.assertEqual(tuple(p.qty for p in self.pair("CLUSD1")), (0, 0))
        self.assertTrue(self.f.store.account("test")["enabled"])

    def test_shared_account_or_cycle_position_fault_still_stops_ordinary_work(self):
        self.holding()
        before = deepcopy(self.f.broker.state["orders"])
        self.capacities({"SPCXUSD1": {5: 500000}, "CLUSD1": {5: 500000}})
        with patch.object(self.f.broker, "cycle_snapshot", side_effect=AccountModeError("账户持仓模式无法确认")):
            self.engine.tick_account("test")
        self.assertFalse(self.f.store.account("test")["enabled"])
        self.assertEqual(self.engine.views["test"]["cycle_state"]["phase"], "attention")
        self.assertEqual(self.f.broker.state["orders"], before)
        self.engine.enable("test", True)
        row = self.f.broker.state["positions"]["XAUUSD1:LONG"]
        row["qty"] = str(dec(row["qty"]) + 1)
        self.f.broker.save()
        self.engine.tick_account("test")
        self.assertFalse(self.f.store.account("test")["enabled"])
        self.assertEqual(self.engine.views["test"]["cycle_state"]["phase"], "attention")
        self.assertEqual(self.f.broker.state["orders"], before)
