"""Deterministic interleavings of maintenance, durable intent and account leases."""
from dataclasses import replace
import time
from unittest import TestCase
from unittest.mock import Mock, patch

from tests import test_pair_budget as fixtures
from tests import test_cycle_hot_engine as engine_fixtures
from trading.account_cache import HotAccountUnavailable
from trading.engine import CYCLE_HOT_POLL_INTERVAL
from trading.exchange import AmbiguousOrder
from trading.models import dec
from trading.pair_execution import runtime_default
from trading.pair_planning import plan_paired_cycle


SYMBOL = "XAUUSD1"


class PairHotRaceTests(TestCase):
    live_brokers = fixtures.PairBudgetTests.live_brokers

    def setUp(self):
        fixtures.PairBudgetTests.setUp(self)
        self.live_brokers()
        self.engine.live_allowed = Mock(return_value=True)
        for key, broker in self.brokers.items():
            account = self.store.account(key)
            account["mode"] = "live"
            self.store.save_account(account)
            broker.start_cycle_hot_data = Mock()
            broker.cycle_cache.configure([SYMBOL])
            broker.cycle_cache.set_connected(True)
            self.publish(key)
        self.state = runtime_default()
        self.state["identities"] = self.trader._members(self.pair)[2]
        self.snapshots, self.guards = self.trader._read(self.brokers, hot=True)
        capacity = self.engine.require_cycle_open_capacity(self.pair["cycle"], 5)
        self.plan = plan_paired_cycle(self.pair, self.snapshots, self.engine.cycle_book(SYMBOL),
            self.engine.cycle_depth(SYMBOL), self.market.rules[SYMBOL], self.state["progress"], capacity=capacity)

    def publish(self, key, **changes):
        cache = self.brokers[key].cycle_cache
        snapshot = replace(self.paper[key].snapshot([SYMBOL]), open_orders=None, **changes)
        self.assertTrue(cache.publish(cache.begin_refresh(), snapshot, time.monotonic()))

    def start(self, after_prepare=None):
        original = self.trader._save
        fired = False

        def save(pair, state):
            nonlocal fired
            original(pair, state)
            pending = state.get("pending")
            if not fired and pending and all(leg["dispatch"] == "prepared" for leg in pending["legs"]):
                fired = True
                if after_prepare:
                    after_prepare()

        with patch.object(self.trader, "_save", side_effect=save):
            self.trader._start(self.pair, self.state, self.brokers, self.snapshots, self.guards, self.plan, kind="cycle")
        return self.state

    def posts(self):
        return [sum(method == "POST" for method, _, _ in broker.api.calls) for broker in self.brokers.values()]

    def test_maintenance_during_preparation_and_sending_does_not_cancel_orders(self):
        original = self.trader._save
        phases = set()

        def save(pair, state):
            original(pair, state)
            pending = state.get("pending")
            if pending and all(leg["receipt"] is None for leg in pending["legs"]):
                phases.add(pending["legs"][0]["dispatch"])
                for key, broker in self.brokers.items():
                    with patch.object(broker, "refresh_cycle_hot_snapshot") as refresh:
                        self.assertEqual(self.engine.poll_cycle_hot_data(key), CYCLE_HOT_POLL_INTERVAL)
                        refresh.assert_not_called()
                    self.guards[key]()

        with patch.object(self.trader, "_save", side_effect=save):
            self.start()
        self.assertEqual(phases, {"prepared", "sending"})
        self.assertEqual(self.posts(), [1, 1])
        self.assertTrue(self.state["last_batch"]["completed"])

    def test_fresh_replacement_replans_once_without_preorder_rest(self):
        original = self.trader._read
        reads = []

        def read(*args, **kwargs):
            reads.append((kwargs.get("hot", False), self.posts()))
            return original(*args, **kwargs)

        with patch.object(self.trader, "_read", side_effect=read):
            self.start(lambda: self.publish("long"))
        self.assertEqual(reads[0], (True, [0, 0]))
        self.assertEqual(sum(hot for hot, _ in reads), 1)
        self.assertEqual(self.posts(), [1, 1])
        self.assertTrue(self.state["last_batch"]["completed"])

    def test_new_snapshot_with_lower_balance_cannot_reuse_old_quantity(self):
        self.start(lambda: self.publish("long", available=dec("0")))
        self.assertEqual(self.posts(), [0, 0])
        self.assertFalse(self.state["last_batch"]["completed"])

    def test_real_event_on_other_side_prevents_replacement_retry(self):
        def change():
            self.publish("long")
            self.brokers["short"]._cycle_account_event("ACCOUNT_UPDATE")
            self.publish("short")

        with patch.object(self.trader, "_read", wraps=self.trader._read) as read:
            self.start(change)
        self.assertFalse(any(call.kwargs.get("hot") for call in read.call_args_list))
        self.assertEqual(self.posts(), [0, 0])

    def test_second_replacement_is_bounded_and_never_sends(self):
        original = self.trader._read

        def read(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs.get("hot"):
                self.publish("long")
            return result

        with patch.object(self.trader, "_read", side_effect=read) as reads:
            self.start(lambda: self.publish("long"))
        self.assertEqual(sum(call.kwargs.get("hot", False) for call in reads.call_args_list), 1)
        self.assertEqual(self.posts(), [0, 0])

    def test_lost_post_response_after_replan_is_not_replayed(self):
        original = self.brokers["long"].submit

        def lost(*args, **kwargs):
            original(*args, **kwargs)
            raise AmbiguousOrder("offline response lost")

        with patch.object(self.brokers["long"], "submit", side_effect=lost):
            self.start(lambda: self.publish("long"))
        self.assertEqual(self.posts(), [1, 1])
        self.assertTrue(self.state["last_batch"]["completed"])

    def test_completion_wakes_both_accounts_without_clearing_api_cooldown(self):
        for key in self.brokers:
            self.engine.work(key).hot_backoff = time.monotonic() + 60
        before = {key: self.engine.work(key).hot_backoff for key in self.brokers}
        self.start()
        for key in self.brokers:
            self.assertTrue(self.engine.work(key).hot_wake)
            self.assertEqual(self.engine.work(key).hot_backoff, before[key])

    def test_pending_transfer_completion_wakes_without_another_rest_poll(self):
        self.store.put("pair_margin:gold", {"pending": {"status": "acknowledged"}})

        def reconcile(*args, **kwargs):
            self.store.put("pair_margin:gold", {"pending": None})
            return {"blocks_trading": True, "reason": "已核对"}

        with patch("trading.margin_balance.MarginBalancer.tick", side_effect=reconcile):
            self.trader.tick(self.pair)
        self.assertEqual(self.posts(), [0, 0])
        self.assertTrue(all(self.engine.work(key).hot_wake for key in self.brokers))


class PendingRefreshRaceTests(TestCase):
    setUp = engine_fixtures.CycleHotEngineTests.setUp
    warm = engine_fixtures.CycleHotEngineTests.warm
    publish = engine_fixtures.CycleHotEngineTests.publish

    def test_repeated_pending_poll_does_not_rearm_its_own_wake_callback(self):
        pair = {"id": "gold", "revision": 1, "cycle": {"enabled": True}}
        self.f.store.put("pair_runtime:gold", {"pending": {"id": "recovering"}})
        self.broker.cycle_cache.invalidate("真实写入已撤销")
        self.broker.start_cycle_hot_data.side_effect = lambda symbols, on_invalidate: self.broker.cycle_cache.set_listener(on_invalidate)
        with patch.object(self.engine.pairs, "active_for_account", return_value=pair):
            self.engine.poll_cycle_hot_data("test")
            self.assertTrue(self.engine.work("test").hot_wake)
            self.engine.work("test").hot_wake = False
            for _ in range(3):
                self.engine.poll_cycle_hot_data("test")
                self.assertFalse(self.engine.work("test").hot_wake)
        self.broker.refresh_cycle_hot_snapshot.assert_not_called()

    def test_batch_appearing_during_refresh_keeps_the_published_lease(self):
        pair = {"id": "gold", "revision": 1, "cycle": {"enabled": True}}
        original = self.publish

        def refresh():
            result = original()
            self.f.store.put("pair_runtime:gold", {"pending": {"id": "preparing"}})
            return result

        with patch.object(self.engine.pairs, "active_for_account", return_value=pair), \
             patch.object(self.broker, "refresh_cycle_hot_snapshot", side_effect=refresh):
            self.engine.poll_cycle_hot_data("test")
        self.broker.cycle_cache.lease([SYMBOL]).require_fresh()
        self.assertEqual(self.engine.work("test").hot_backoff, 0)
        self.api.call.assert_not_called()

    def test_late_pending_observation_does_not_overwrite_completion_wake(self):
        pair = {"id": "gold", "revision": 1, "cycle": {"enabled": True}}
        self.f.store.put("pair_runtime:gold", {"pending": {"id": "preparing"}})

        def complete(_):
            self.f.store.put("pair_runtime:gold", {"pending": None})
            self.engine.wake_cycle_hot_data("test")
            return None

        with patch.object(self.engine.pairs, "active_for_account", return_value=pair), \
             patch.object(self.f.store, "intent", side_effect=complete):
            self.engine.poll_cycle_hot_data("test")
        self.assertTrue(self.engine.work("test").hot_wake)
        self.assertEqual(self.engine.work("test").hot_backoff, 0)
        self.broker.cycle_cache.lease([SYMBOL]).require_fresh()

    def test_pending_batch_does_not_extend_eight_second_authority(self):
        lease = self.warm()
        pair = {"id": "gold", "revision": 1, "cycle": {"enabled": True}}
        self.f.store.put("pair_runtime:gold", {"pending": {"id": "preparing"}})
        with patch.object(self.engine.pairs, "active_for_account", return_value=pair):
            self.engine.poll_cycle_hot_data("test")
        with patch("trading.account_cache.time.monotonic", return_value=lease.started_monotonic + 9):
            with self.assertRaises(HotAccountUnavailable):
                lease.require_fresh()
