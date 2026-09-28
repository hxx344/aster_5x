"""Offline regressions for two independent collateral budgets and recovery."""
from copy import deepcopy
from dataclasses import replace
from fractions import Fraction
import tempfile
import threading
import time
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from tests.helpers import account, seed_cycle_capacity
from trading.cycle import DEFAULT_CYCLE
from trading.engine import Engine
from trading.exchange import AmbiguousOrder, ExchangeError
from trading.models import TradingError, dec
from trading.pair_execution import PairTrader
from trading.pair_planning import plan_paired_cycle, plan_ordinary, PairPositionError
from trading.pairing import validate_pair
from trading.paper import DemoMarket, PaperBroker
from trading.store import Store


class PairTradingTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / "state.sqlite3")
        self.market = DemoMarket()
        self.engine = Engine(self.store, market=self.market)
        self.brokers = {}
        for key in ("long", "short"):
            value = account(key)
            value["enabled"] = False
            self.store.save_account(value)
            self.brokers[key] = PaperBroker(key, self.market, self.store)
            self.engine.brokers[key] = self.brokers[key]
        self.pair = self.store.save_pair(validate_pair({"id": "gold", "name": "黄金配对", "long_account_id": "long",
            "short_account_id": "short", "enabled": True, "cycle": {"enabled": True}}), create=True)
        self.trader = PairTrader(self.engine)
        seed_cycle_capacity(self.engine)
        self.margin = patch("trading.margin_balance.MarginBalancer.tick", return_value={"blocks_trading": False})
        self.margin.start()
        self.addCleanup(self.margin.stop)

    def tick(self):
        seed_cycle_capacity(self.engine)
        return self.trader.tick(self.store.pair("gold"))

    def snapshots(self):
        return {key: broker.snapshot(["XAUUSD1"]) for key, broker in self.brokers.items()}

    def expire(self):
        state = self.store.get("pair_runtime:gold")
        state["progress"]["opened_at"] = time.time() - 100
        self.store.put("pair_runtime:gold", state)

    def assert_flat(self):
        self.assertTrue(all(not p.qty for snap in self.snapshots().values() for p in snap.positions))

    def test_orders_are_parallel_single_direction_and_resume_after_restart(self):
        barrier = threading.Barrier(2, timeout=3)
        originals = {key: broker.submit for key, broker in self.brokers.items()}
        def send(key, orders):
            self.assertIsNotNone(self.store.get("pair_runtime:gold")["pending"])
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0]["type"], "MARKET")
            self.assertEqual(orders[0]["positionSide"], key.upper())
            barrier.wait()
            return originals[key](orders)
        with patch.object(self.brokers["long"], "submit", side_effect=lambda rows: send("long", rows)), \
             patch.object(self.brokers["short"], "submit", side_effect=lambda rows: send("short", rows)):
            state = self.tick()
        self.assertEqual(state["phase"], "holding", state)
        self.assertIsNone(state["pending"])
        self.assertEqual(state["progress"]["quantities"]["LONG"], state["progress"]["quantities"]["SHORT"])
        self.expire()
        self.trader = PairTrader(self.engine)
        state = self.tick()
        self.assertEqual(state["progress"]["completed_cycles"], 1, state)
        self.assert_flat()

    def test_reject_one_leg_reduces_other_without_opening_missing_leg(self):
        with patch.object(self.brokers["short"], "submit", side_effect=ExchangeError("余额不足", code=-2019)):
            state = self.tick()
        self.assertEqual(state["phase"], "repairing", state)
        self.assertIsNotNone(state["pending"])
        state = self.tick()
        self.assertIsNone(state["pending"], state)
        self.assertFalse(state["last_batch"]["completed"])
        self.assertEqual(len(self.brokers["long"].state["orders"]), 2)
        self.assertEqual(len(self.brokers["short"].state["orders"]), 0)
        self.assert_flat()

    def test_timeout_after_fill_queries_same_id_without_resending(self):
        original = self.brokers["long"].submit
        def timeout(orders):
            original(orders)
            raise AmbiguousOrder("模拟回执丢失")
        with patch.object(self.brokers["long"], "submit", side_effect=timeout) as send:
            state = self.tick()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(state["phase"], "holding", state)
        self.assertEqual(len(self.brokers["long"].state["orders"]), 1)

    def test_unknown_live_style_query_blocks_retries_and_margin(self):
        with patch.object(self.brokers["long"], "submit", side_effect=AmbiguousOrder("timeout")), \
             patch.object(self.brokers["long"], "query", side_effect=ExchangeError("order not found", code=-2013)):
            state = self.tick()
            self.assertIsNotNone(state["pending"])
            frozen = deepcopy(state["pending"]["legs"])
            with patch.object(self.brokers["short"], "submit", side_effect=AssertionError("never repeat")), \
                 patch("trading.margin_balance.MarginBalancer.tick", side_effect=AssertionError("no transfer during unknown")):
                for _ in range(3):
                    state = self.tick()
            self.assertEqual(state["pending"]["legs"][0]["order"], frozen[0]["order"])
            self.assertEqual(state["phase"], "reconciling")

    def test_paused_cycle_reduces_increment_without_waiting_timer(self):
        state = self.tick()
        self.assertEqual(state["phase"], "holding")
        value = self.store.pair("gold")
        value["enabled"] = False
        self.store.save_pair(value)
        state = self.tick()
        self.assertIsNone(state["pending"], state)
        self.assertEqual(state["progress"]["completed_cycles"], 1)
        self.assert_flat()
        count = len(self.brokers["long"].state["orders"])
        self.tick()
        self.assertEqual(len(self.brokers["long"].state["orders"]), count)

    def test_external_opposite_position_blocks_all_new_writes(self):
        self.brokers["long"].state["positions"]["XAUUSD1:SHORT"].update(qty="1", entry="4400")
        self.brokers["long"].save()
        with patch.object(self.brokers["short"], "submit", side_effect=AssertionError("blocked")):
            state = self.tick()
        self.assertEqual(state["phase"], "attention", state)

    def test_weak_subaccount_limits_quantity_despite_large_combined_equity(self):
        self.brokers["long"].state["wallet"] = "50"
        self.brokers["long"].save()
        state = self.tick()
        self.assertEqual(state["phase"], "holding", state)
        qty = dec(state["progress"]["quantities"]["LONG"])
        self.assertLess(qty, dec("0.04"))
        self.assertLess(self.snapshots()["long"].ratio, dec("0.55"))

    def test_closing_does_not_require_opening_fees_or_capital_or_daily_allowance(self):
        state = self.tick()
        self.expire()
        state = self.store.get("pair_runtime:gold")
        snapshots = {key: replace(snap, equity=dec("-1"), available=dec("-5"), fees={}, brackets={}, current_leverage_caps={})
                     for key, snap in self.snapshots().items()}
        plan = plan_paired_cycle(self.pair, snapshots, self.market.book("XAUUSD1"), self.market.depth("XAUUSD1"),
                                 self.market.rules["XAUUSD1"], state["progress"])
        self.assertEqual(plan.phase, "close")

    def test_public_capacity_counts_both_accounts_not_each_independently(self):
        value = deepcopy(self.pair)
        value["ordinary"]["threshold"] = "0"
        value["ordinary"]["order_notional"] = "100000"
        book = self.market.book("XAUUSD1")
        plan = plan_ordinary(value, self.snapshots(), book, self.market.rules["XAUUSD1"], {5: "2000"})
        self.assertLessEqual(Fraction(plan.qty) * 2 * max(Fraction(book.ask), Fraction(book.mark)), 2000)

    def test_daily_limit_is_checked_for_each_account(self):
        value = self.store.pair("gold")
        value["cycle"]["daily_volume_limit"] = "1000"
        self.store.save_pair(value)
        state = self.tick()
        self.assertEqual(state["phase"], "holding", state)
        qty = dec(state["progress"]["quantities"]["LONG"])
        self.assertLessEqual(2 * qty * self.market.book("XAUUSD1").ask, 1000)
        self.expire()
        self.tick()
        count = len(self.brokers["long"].state["orders"])
        state = self.tick()
        self.assertEqual(len(self.brokers["long"].state["orders"]), count)
        self.assertNotEqual(state["phase"], "holding")

    def test_ordinary_baseline_survives_switch_to_cycle(self):
        pair = self.store.pair("gold")
        pair["cycle"]["enabled"] = False
        pair["ordinary"]["enabled"] = True
        self.store.save_pair(pair)
        for _ in range(4):
            state = self.tick()
            if dec(state["owned"]["LONG"]):
                break
        self.assertGreater(dec(state["owned"]["LONG"]), 0, state)
        baseline = deepcopy(state["owned"])
        pair = self.store.pair("gold")
        pair["cycle"]["enabled"] = True
        pair["ordinary"]["enabled"] = False
        self.store.save_pair(pair)
        state = self.tick()
        self.assertEqual(state["phase"], "holding", state)
        self.expire()
        state = self.tick()
        self.assertEqual(state["owned"], baseline)
        self.assertEqual(self.snapshots()["long"].pair("XAUUSD1")[0].qty, dec(baseline["LONG"]))

    def test_final_configuration_change_prevents_both_submissions(self):
        original = self.trader._config_guard
        calls = 0
        def guard(pair, identities, *, opening):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise TradingError("changed")
            return original(pair, identities, opening=opening)
        with patch.object(self.trader, "_config_guard", side_effect=guard):
            state = self.tick()
        self.assertIsNone(state["pending"], state)
        self.assert_flat()
        self.assertFalse(self.brokers["long"].state["orders"])

    def test_partial_close_finishes_only_residual_and_preserves_baseline(self):
        self.tick()
        self.expire()
        original = self.brokers["short"].submit
        first = True
        def partial(orders):
            nonlocal first
            if first:
                first = False
                original_book = self.market.book
                with patch.object(self.market, "book", side_effect=lambda symbol: replace(original_book(symbol), ask_qty=dec("0.1"))):
                    return original(orders)
            return original(orders)
        with patch.object(self.brokers["short"], "submit", side_effect=partial):
            state = self.tick()
        self.assertIsNotNone(state["pending"], state)
        state = self.tick()
        self.assertIsNone(state["pending"], state)
        self.assert_flat()
