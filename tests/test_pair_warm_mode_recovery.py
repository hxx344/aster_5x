"""Recovery reuses bounded account-mode evidence without reusing balances or fills."""
from dataclasses import replace
import time
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from tests import test_pair_budget as fixtures
from trading.exchange import BudgetWait, SnapshotSuperseded
from trading.execution import Executor
from trading.models import AccountModeError, TradingError, dec
from trading.pair_execution import runtime_default
from trading.pair_planning import plan_ordinary

SYMBOL, DUAL, MULTI = fixtures.SYMBOL, fixtures.DUAL, fixtures.MULTI
ACCOUNT = "/fapi/v3/accountWithJoinMargin"
RISK = "/fapi/v3/positionRisk"


class PairWarmModeRecoveryTests(TestCase):
    live_brokers = fixtures.PairBudgetTests.live_brokers

    def setUp(self):
        fixtures.PairBudgetTests.setUp(self)
        self.live_brokers()
        self.ticks = time.monotonic()
        clock = patch("trading.exchange.time.monotonic", side_effect=lambda: self.ticks)
        clock.start()
        self.addCleanup(clock.stop)

    def clear_calls(self):
        for broker in self.brokers.values():
            broker.api.calls.clear()

    def warm(self):
        result = self.trader._read(self.brokers)
        self.clear_calls()
        return result

    def paths(self, key):
        return [path for _, path, _ in self.brokers[key].api.calls]

    def test_warm_recovery_reads_both_current_accounts_for_twenty_weight(self):
        self.warm()
        stamps = {key: dict(broker.cached_at) for key, broker in self.brokers.items()}
        self.paper["long"].submit([Executor.order(SYMBOL, "LONG", "BUY", dec("0.001"), "external")])
        before = self.budget.snapshot()["used"]
        snapshots, guards = self.trader._read(self.brokers, reconciliation=True)
        self.assertEqual(self.budget.snapshot()["used"] - before, 20)
        self.assertEqual(snapshots["long"].pair(SYMBOL)[0].qty, dec("0.001"))
        for key, broker in self.brokers.items():
            self.assertEqual(self.paths(key), [ACCOUNT, RISK])
            self.assertTrue(all(method == "GET" and flags[0] for method, _, flags in broker.api.calls))
            self.assertEqual(broker.cached_at, stamps[key])
            guards[key]()

    def test_shared_budget_admission_uses_warm_cost_and_retains_total_limit(self):
        self.warm()
        with self.budget.reconciliation():
            self.budget.reserve(self.budget.limit - 24 - self.budget.snapshot()["used"])
        self.trader._read(self.brokers, reconciliation=True)
        self.assertEqual(self.budget.snapshot()["used"], self.budget.limit - 4)
        self.clear_calls()
        with self.assertRaises(BudgetWait):
            self.trader._read(self.brokers, reconciliation=True)
        self.assertTrue(all(not self.paths(key) for key in self.brokers))

    def test_dual_expires_at_fifteen_seconds_but_multi_at_ten_minutes(self):
        self.warm()
        started = self.ticks
        self.ticks += 14.999
        self.trader._read(self.brokers, reconciliation=True)
        self.assertTrue(all(DUAL not in self.paths(key) and MULTI not in self.paths(key) for key in self.brokers))
        self.clear_calls()
        self.ticks += 0.002
        self.trader._read(self.brokers, reconciliation=True)
        for key in self.brokers:
            self.assertEqual(self.paths(key), [DUAL, ACCOUNT, RISK])
        self.clear_calls()
        self.ticks = started + 599.999
        self.trader._read(self.brokers, reconciliation=True)
        for key, broker in self.brokers.items():
            self.assertNotIn(MULTI, self.paths(key))
            self.assertEqual(broker.cached_at["multi"], started)
        self.clear_calls()
        self.ticks = started + 600.001
        self.trader._read(self.brokers, reconciliation=True)
        for key in self.brokers:
            self.assertEqual(self.paths(key), [MULTI, ACCOUNT, RISK])

    def test_one_expired_field_refreshes_only_that_accounts_mode(self):
        self.warm()
        self.brokers["long"].cached_at["dual"] -= 15
        before = self.budget.snapshot()["used"]
        self.trader._read(self.brokers, reconciliation=True)
        self.assertEqual(self.budget.snapshot()["used"] - before, 50)
        self.assertEqual(self.paths("long"), [DUAL, ACCOUNT, RISK])
        self.assertEqual(self.paths("short"), [ACCOUNT, RISK])

    def test_balance_and_order_events_preserve_modes_but_revoke_snapshots(self):
        _, guards = self.warm()
        for event in ("ACCOUNT_UPDATE", "ORDER_TRADE_UPDATE"):
            with self.subTest(event=event):
                self.brokers["long"]._cycle_account_event(event)
                with self.assertRaises(TradingError):
                    guards["long"]()
                _, guards = self.trader._read(self.brokers, reconciliation=True)
                self.assertEqual(self.paths("long"), [ACCOUNT, RISK])
                self.clear_calls()

    def test_config_event_forces_modes_and_rejects_changed_account_mode(self):
        _, guards = self.warm()
        broker = self.brokers["long"]
        broker._cycle_account_event("ACCOUNT_CONFIG_UPDATE")
        broker.api.hedge = False
        with self.assertRaises(TradingError):
            guards["long"]()
        snapshots, _ = self.trader._read(self.brokers, reconciliation=True)
        self.assertEqual(self.paths("long"), [DUAL, MULTI, ACCOUNT, RISK])
        self.assertEqual(self.paths("short"), [ACCOUNT, RISK])
        with self.assertRaises(AccountModeError):
            snapshots["long"].require_modes([SYMBOL])

    def test_event_during_warm_recovery_still_discards_inconsistent_read(self):
        self.warm()
        broker = self.brokers["long"]
        original = broker.api.call

        def changed(method, path, *args, **kwargs):
            value = original(method, path, *args, **kwargs)
            if path == ACCOUNT:
                broker._cycle_account_event("ACCOUNT_UPDATE")
            return value

        with patch.object(broker.api, "call", side_effect=changed), self.assertRaises(SnapshotSuperseded):
            self.trader._read(self.brokers, reconciliation=True)
        self.assertNotIn(DUAL, self.paths("long"))
        self.assertNotIn(MULTI, self.paths("long"))

    def test_leverage_confirmation_still_forces_fresh_modes(self):
        self.warm()
        state = runtime_default()
        state["pending"] = {"kind": "leverage", "before": {"LONG": "0", "SHORT": "0"},
                            "target_leverage": 5, "previous_leverage": 2, "results": {}}
        self.trader._recover_leverage(self.pair, state, self.brokers)
        self.assertIsNone(state["pending"])
        for key in self.brokers:
            self.assertEqual(self.paths(key), [DUAL, MULTI, ACCOUNT, RISK])

    def ordinary_plan(self):
        self.pair["cycle"]["enabled"] = False
        self.pair["ordinary"]["enabled"] = True
        self.pair = self.store.save_pair(self.pair)
        snapshots, guards = self.warm()
        state = runtime_default()
        state["identities"] = self.trader._members(self.pair)[2]
        capacities = {5: dec(10000000), 10: dec(0), 20: dec(0)}
        capacity = patch.object(self.engine, "capacities", return_value=capacities)
        capacity.start()
        self.addCleanup(capacity.stop)
        plan = plan_ordinary(self.pair, snapshots, self.market.book(SYMBOL), self.market.rules[SYMBOL], capacities)
        return state, snapshots, guards, plan

    def test_pre_send_cancellation_finishes_without_mode_gets_or_order_posts(self):
        state, snapshots, guards, plan = self.ordinary_plan()
        before = self.budget.snapshot()["used"]
        with patch("trading.pair_execution.plan_ordinary", return_value=replace(plan, qty=dec(0))):
            self.trader._start(self.pair, state, self.brokers, snapshots, guards, plan, kind="ordinary")
        self.assertIsNone(state["pending"])
        self.assertFalse(state["last_batch"]["completed"])
        self.assertEqual(state["owned"], {"LONG": "0", "SHORT": "0"})
        self.assertEqual(self.budget.snapshot()["used"] - before, 20)
        for key in self.brokers:
            self.assertEqual(self.paths(key), [ACCOUNT, RISK])

    def test_successful_market_orders_reconcile_current_positions_without_mode_gets(self):
        state, snapshots, guards, plan = self.ordinary_plan()
        before = self.budget.snapshot()["used"]
        self.trader._start(self.pair, state, self.brokers, snapshots, guards, plan, kind="ordinary")
        self.assertIsNone(state["pending"])
        self.assertTrue(state["last_batch"]["completed"])
        self.assertTrue(all(dec(qty) == plan.qty for qty in state["owned"].values()))
        self.assertEqual(self.budget.snapshot()["used"] - before, 22)
        for key, broker in self.brokers.items():
            self.assertEqual(self.paths(key), ["/fapi/v3/order", ACCOUNT, RISK])
            self.assertEqual([method for method, _, _ in broker.api.calls], ["POST", "GET", "GET"])

    def test_stream_gaps_revoke_modes_once_and_ignore_old_stream_callbacks(self):
        broker = self.brokers["long"]
        streams = []

        def make_stream(api, *, on_state, on_event, on_payload=None):
            stream = SimpleNamespace(on_state=on_state, on_event=on_event, on_payload=on_payload,
                                     start=Mock(side_effect=lambda: on_state(True)),
                                     close=Mock(side_effect=lambda: on_state(False)))
            streams.append(stream)
            return stream

        with patch("trading.exchange.PrivateAccountStream", side_effect=make_stream):
            broker.start_cycle_hot_data([SYMBOL])
            _, guards = self.warm()
            streams[0].on_state(False)
            with self.assertRaises(TradingError):
                guards["long"]()
            _, guards = self.trader._read(self.brokers, reconciliation=True)
            self.assertEqual(self.paths("long"), [DUAL, MULTI, ACCOUNT, RISK])
            # Offline REST evidence stays usable until expiry; retries do not
            # repeatedly throw it away and trigger another pair of 30-weight GETs.
            streams[0].on_state(False)
            guards["long"]()
            self.clear_calls()
            streams[0].on_state(True)
            with self.assertRaises(TradingError):
                guards["long"]()
            _, guards = self.trader._read(self.brokers, reconciliation=True)
            self.assertEqual(self.paths("long"), [DUAL, MULTI, ACCOUNT, RISK])
            broker.stop_cycle_hot_data()
            with self.assertRaises(TradingError):
                guards["long"]()
            broker.start_cycle_hot_data([SYMBOL])
            _, guards = self.trader._read(self.brokers, reconciliation=True)
            self.clear_calls()
            streams[0].on_state(False)
            streams[0].on_event("ACCOUNT_CONFIG_UPDATE")
            guards["long"]()
            self.trader._read(self.brokers, reconciliation=True)
            self.assertEqual(self.paths("long"), [ACCOUNT, RISK])
            broker.stop_cycle_hot_data()
