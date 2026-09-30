"""Independent funds checks through real paper ledgers and pair scheduling."""
from dataclasses import replace
import time
from unittest import TestCase
from unittest.mock import patch

from tests import test_pair_trading as fixtures
from tests import test_pair_budget as budget_fixtures
from trading.exchange import AmbiguousOrder, ExchangeError, RequestNotSent
from trading.margin_balance import MarginBalancer
from trading.models import TradingError, dec
from trading.pair_execution import PairTrader
from trading.pair_planning import PairRecoveryConflict


class PairMarginLifelineTests(TestCase):
    def setUp(self):
        fixtures.PairTradingTests.setUp(self)
        self.margin.stop()
        self.wall = time.time()
        clock = patch("time.time", side_effect=lambda: self.wall)
        clock.start()
        self.addCleanup(clock.stop)
        self.engine.pairs.trader = self.trader
        self.enable_margin()

    tick = fixtures.PairTradingTests.tick
    expire = fixtures.PairTradingTests.expire

    def enable_margin(self):
        self.pair = self.store.pair("gold")
        self.pair["margin"].update(enabled=True, cooldown_seconds=1)
        self.pair = self.store.save_pair(self.pair)

    def wallet(self, side, amount):
        broker = self.brokers[side]
        broker.state["wallet"] = str(amount)
        self.store.put("paper:" + side, broker.state)

    def assert_transferred(self, state):
        self.assertEqual(state["margin"]["status"], "paper_confirmed", state)
        self.assertEqual(dec(self.brokers["long"].state["wallet"]), 25000)
        self.assertEqual(dec(self.brokers["short"].state["wallet"]), 25000)
        journal = self.store.get("pair_margin:gold")
        self.assertIsNone(journal["pending"])
        self.assertEqual(dec(journal["last_transfer"]["amount"]), 1000)

    def assert_no_orders(self):
        self.assertTrue(all(not broker.state["orders"] for broker in self.brokers.values()))

    def test_mismatched_position_or_leverage_does_not_block_real_transfer(self):
        for mismatch in ("quantity", "leverage"):
            with self.subTest(mismatch=mismatch):
                self.store.put("pair_runtime:gold", {})
                self.store.put("pair_margin:gold", {})
                self.wallet("long", 26000)
                self.wallet("short", 24000)
                long = self.brokers["long"]
                long.state["leverages"]["XAUUSD1"] = 10 if mismatch == "leverage" else 5
                position = long.state["positions"]["XAUUSD1:LONG"]
                position.update(qty="0.01" if mismatch == "quantity" else "0", entry="4412.015")
                self.store.put("paper:long", long.state)
                state = self.tick()
                self.assertEqual(state["margin"]["status"], "paper_confirmed", state)
                self.assertIsNone(state["pending"])
                self.assert_no_orders()
                self.assertEqual(dec(self.brokers["long"].state["wallet"]) +
                                 dec(self.brokers["short"].state["wallet"]), 50000)
                self.wall += 5
                state = self.tick()
                self.assertEqual(state["phase"], "attention", state)
                self.assert_no_orders()

    def test_hot_failure_uses_one_margin_fallback_and_keeps_trade_retry(self):
        self.wallet("long", 26000)
        self.wallet("short", 24000)
        original = self.trader._read

        def read(brokers, **options):
            if options.get("hot"):
                raise RequestNotSent("hot data unavailable", retry_after=60)
            self.assertTrue(options.get("margin"))
            return original(brokers, **options)

        with patch.object(self.trader, "_read", side_effect=read) as reads:
            self.assertEqual(self.engine.pairs.tick("gold"), 5)
            state = self.store.get("pair_runtime:gold")
            self.assert_transferred(state)
            self.assertEqual(reads.call_count, 2)
            self.wall += 1
            self.engine.pairs.tick("gold")
            self.assertEqual(reads.call_count, 2)
            self.wall += 4
            self.wallet("long", 26000)
            self.wallet("short", 24000)
            self.assertEqual(self.engine.pairs.tick("gold"), 5)
            self.assert_transferred(self.store.get("pair_runtime:gold"))
            self.assertEqual(reads.call_count, 3)
        self.assert_no_orders()

    def test_market_retry_survives_restart_while_margin_keeps_checking(self):
        with patch.object(self.engine, "cycle_book", side_effect=RequestNotSent("market backoff", retry_after=60)) as book:
            self.assertEqual(self.engine.pairs.tick("gold"), 5)
            self.assertEqual(book.call_count, 1)
            self.wall += 5
            self.wallet("long", 26000)
            self.wallet("short", 24000)
            self.trader = PairTrader(self.engine)
            self.engine.pairs.trader = self.trader
            self.assertEqual(self.engine.pairs.tick("gold"), 5)
            self.assert_transferred(self.store.get("pair_runtime:gold"))
            self.assertEqual(book.call_count, 1)
            self.wall += 55
            self.engine.pairs.tick("gold")
            self.assertEqual(book.call_count, 2)
        self.assert_no_orders()

    def test_margin_fallback_cannot_borrow_recovery_budget_or_poll_during_backoff(self):
        budget_fixtures.PairBudgetTests.live_brokers(self)
        self.budget.reserve(1500)
        before = self.budget.snapshot()["used"]
        state = self.tick()
        self.assertEqual(state["margin"]["status"], "blocked", state)
        self.assertEqual(state["margin"]["api_notice"]["kind"], "budget")
        self.assertEqual(self.budget.snapshot()["used"], before)
        self.assertTrue(all(not broker.api.calls for broker in self.brokers.values()))
        self.wall += 1
        with patch.object(self.trader, "_read", side_effect=AssertionError("no reads during API backoff")):
            state = self.tick()
        self.assertGreater(state["retry_after"], 1)
        self.assertEqual(state["margin"]["api_notice"]["kind"], "budget")

    def test_pair_revision_revokes_previous_trade_retry(self):
        with patch.object(self.engine, "cycle_book", side_effect=RequestNotSent("market backoff", retry_after=60)) as book:
            self.tick()
            self.pair = self.store.save_pair({**self.store.pair("gold"), "name": "new revision"})
            self.tick()
            self.assertEqual(book.call_count, 2)

    def test_due_close_read_or_market_failure_still_transfers(self):
        self.assertEqual(self.tick()["phase"], "holding")
        self.expire()
        counts = {side: len(broker.state["orders"]) for side, broker in self.brokers.items()}
        for failure in ("read", "market", "planning", "before_intent"):
            with self.subTest(failure=failure):
                self.store.put("pair_margin:gold", {})
                self.wallet("long", 26000)
                self.wallet("short", 24000)
                if failure == "read":
                    original = self.trader._read

                    def read(brokers, **options):
                        if options.get("reconciliation"):
                            raise TradingError("close read unavailable")
                        return original(brokers, **options)

                    mock = patch.object(self.trader, "_read", side_effect=read)
                elif failure == "market":
                    mock = patch.object(self.engine, "cycle_book", side_effect=TradingError("close book unavailable"))
                elif failure == "planning":
                    mock = patch("trading.pair_execution.plan_paired_cycle", side_effect=TradingError("close plan unavailable"))
                else:
                    mock = patch.object(self.trader, "_start", side_effect=RequestNotSent("lease expired before intent"))
                with mock:
                    state = self.tick()
                self.assertEqual(state["margin"]["status"], "paper_confirmed", state)
                self.assertIsNone(state["pending"])
                self.assertEqual({side: len(broker.state["orders"]) for side, broker in self.brokers.items()}, counts)
                self.wall += 5

    def test_account_modes_and_open_orders_still_block_transfers(self):
        self.wallet("long", 26000)
        self.wallet("short", 24000)
        original = self.trader._read
        for mode in ("isolated", "hedge", "multi", "missing_symbol", "orders"):
            with self.subTest(mode=mode):
                self.store.put("pair_runtime:gold", {})
                self.store.put("pair_margin:gold", {})

                def read(brokers, **options):
                    snapshots, guards = original(brokers, **options)
                    snapshot = snapshots["long"]
                    if mode == "isolated":
                        snapshots["long"] = replace(snapshot, positions=[replace(position, isolated=True)
                            if position.symbol == "XAUUSD1" else position for position in snapshot.positions])
                    elif mode == "hedge":
                        snapshots["long"] = replace(snapshot, hedge_mode=False)
                    elif mode == "multi":
                        snapshots["long"] = replace(snapshot, multi_assets=True)
                    elif mode == "orders":
                        snapshots["long"] = replace(snapshot, open_orders=[{"orderId": "external"}])
                    else:
                        snapshots["long"] = replace(snapshot, positions=[position for position in snapshot.positions
                            if position.symbol != "XAUUSD1"])
                    return snapshots, guards

                with patch.object(self.trader, "_read", side_effect=read):
                    state = self.tick()
                self.assertEqual(state["margin"]["status"], "blocked", state)
                self.assertEqual(self.brokers["long"].state["wallet"], "26000")
                self.assert_no_orders()

    def test_unknown_orders_publish_wait_without_transfer_or_repeated_orders(self):
        with patch.object(self.brokers["long"], "submit", side_effect=AmbiguousOrder("unknown order")), \
             patch.object(self.brokers["long"], "query", side_effect=ExchangeError("unknown order", code=-2013)):
            self.assertIsNotNone(self.tick()["pending"])
            self.wallet("long", 26000)
            self.wallet("short", 24000)
            with patch.object(MarginBalancer, "_paper", side_effect=AssertionError("no new transfer")), \
                 patch.object(self.brokers["short"], "submit", side_effect=AssertionError("no repeated order")):
                for _ in range(3):
                    self.wall += 5
                    state = self.tick()
                    self.assertIsNotNone(state["pending"])
                    self.assertEqual(state["margin"]["status"], "waiting")
                    self.assertIn("核对订单", state["margin"]["reason"])
            self.assertEqual(self.brokers["long"].state["wallet"], "26000")
            self.assertEqual(self.brokers["short"].state["wallet"], "24000")
            self.assertIsNone(self.store.get("pair_margin:gold").get("last_transfer"))

    def test_paused_pair_and_changed_identity_never_transfer(self):
        self.wallet("long", 26000)
        self.wallet("short", 24000)
        self.pair = self.store.save_pair({**self.pair, "enabled": False})
        self.assertEqual(self.tick()["phase"], "paused")
        self.assertEqual(self.brokers["long"].state["wallet"], "26000")
        self.pair = self.store.save_pair({**self.pair, "enabled": True})
        state = self.store.get("pair_runtime:gold")
        state["identities"]["long"]["env_prefix"] = "DIFFERENT_BINDING"
        self.store.put("pair_runtime:gold", state)
        state = self.tick()
        self.assertEqual(state["phase"], "attention")
        self.assertEqual(self.brokers["long"].state["wallet"], "26000")
        self.assert_no_orders()

    def test_margin_recovery_conflict_preserves_newer_runtime(self):
        self.wallet("long", 26000)
        self.wallet("short", 24000)
        newer = {"phase": "attention", "reason": "late archived order", "pending": {"id": "newer"}}

        def conflict(*args):
            self.store.put("pair_runtime:gold", newer)
            raise PairRecoveryConflict("late archived order")

        with patch("trading.pair_recovery.require_archived_orders_clear", side_effect=conflict):
            self.assertEqual(self.tick(), newer)
        self.assertEqual(self.store.get("pair_runtime:gold"), newer)
        self.assertEqual(self.brokers["long"].state["wallet"], "26000")
        self.assert_no_orders()
