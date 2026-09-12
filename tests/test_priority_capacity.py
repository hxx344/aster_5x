"""High-tier capacity wakes and their upgrade/confirmation/opening handoff."""
from contextlib import contextmanager
import os
import threading
import time
import unittest
from unittest.mock import patch

from trading.engine import Engine, snapshot_json
from trading.exchange import BudgetWait
from trading.models import TradingError, dec
from .helpers import Fixture, account


SYMBOL = "XAUUSD1"


class PriorityCapacityFixture(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.engine = Engine(self.f.store, market=self.f.market)
        self.engine.brokers["test"] = self.f.broker
        self.real_poll_market = self.engine.poll_market

    def publish(self, capacities):
        values = {tier: dec(value) for tier, value in capacities.items()}
        with patch.object(self.f.market, "capacities", return_value=values):
            return self.real_poll_market(SYMBOL)

    def take_priority_tick(self):
        # Reproduce the scheduler's consumption of a signal before dispatch.
        with self.engine.lock:
            self.engine.priority_accounts.pop("test", None)
            self.engine.priority_followups.discard("test")
            self.engine.active_priority_accounts.add("test")
        return self.engine.tick_account("test")


class PriorityCapacitySignalTests(PriorityCapacityFixture):
    def test_only_capacity_strictly_above_the_account_threshold_wakes(self):
        self.f.account["policy"]["threshold"] = "20000"
        self.f.store.save_account(self.f.account)
        self.assertEqual(self.publish({5: 500000, 10: 20000, 20: 19999}), 2)
        self.assertNotIn("test", self.engine.priority_accounts)

        self.publish({10: "20000.0001", 20: 20000})
        self.assertIn(SYMBOL, self.engine.priority_accounts["test"])

    def test_paused_unrelated_and_disallowed_live_accounts_do_not_wake(self):
        paused = {**account("paused"), "enabled": False}
        unrelated = account("other_market")
        unrelated["policy"]["symbols"] = ["CLUSD1"]
        live = account("live", mode="live")
        high_threshold = account("high_threshold")
        high_threshold["policy"]["threshold"] = "900000"
        for row in (paused, unrelated, live, high_threshold):
            self.f.store.save_account(row)

        with patch.dict(os.environ, {}, clear=True):
            self.publish({10: 500000, 20: 500000})
        self.assertEqual(set(self.engine.priority_accounts), {"test"})

    def test_unchanged_availability_does_not_wake_again_but_new_20x_does(self):
        self.publish({10: 500000, 20: 0})
        self.assertIn("test", self.engine.priority_accounts)
        self.engine.priority_accounts.pop("test")

        self.publish({10: 499999, 20: 0})
        self.assertNotIn("test", self.engine.priority_accounts)

        self.publish({10: 499999, 20: 500000})
        self.assertIn(SYMBOL, self.engine.priority_accounts["test"])

    def test_capacity_recovery_after_a_drop_creates_a_new_opportunity(self):
        self.publish({10: 500000, 20: 0})
        self.engine.priority_accounts.pop("test")
        self.publish({10: 0, 20: 0})
        self.assertNotIn("test", self.engine.priority_accounts)
        self.publish({10: 500000, 20: 0})
        self.assertIn("test", self.engine.priority_accounts)

    def test_losing_one_available_tier_does_not_create_another_wake(self):
        self.publish({10: 500000, 20: 500000})
        self.engine.priority_accounts.pop("test")
        self.publish({10: 0, 20: 500000})
        self.assertNotIn("test", self.engine.priority_accounts)
        self.publish({10: 500000, 20: 500000})
        self.assertIn("test", self.engine.priority_accounts)

    def test_current_high_tier_can_wake_when_new_capacity_is_below_existing_exposure(self):
        self.f.broker.state["leverages"][SYMBOL] = 10
        for side in ("LONG", "SHORT"):
            self.f.broker.state["positions"][SYMBOL + ":" + side] = {"qty": "5", "entry": "4412"}
        self.f.broker.save()
        snapshot = self.f.broker.snapshot([SYMBOL])
        self.engine.view("test", snapshot=snapshot_json(snapshot, [SYMBOL]))

        # Existing gross exposure exceeds 44,000; 20,000 remaining public
        # capacity still covers this account's next 1,000-per-side batch.
        self.publish({10: 20000, 20: 0})
        self.assertIn("test", self.engine.priority_accounts)
        with patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit, \
             patch.object(self.f.broker, "set_leverage", wraps=self.f.broker.set_leverage) as change:
            self.take_priority_tick()
        submit.assert_called_once()
        change.assert_not_called()

    def test_slow_capacity_response_cannot_wake_with_an_expired_sample(self):
        clock = [1000.0]

        def slow_capacity(*args):
            clock[0] += 9
            return {10: dec(500000)}

        with patch("trading.engine.time.time", side_effect=lambda: clock[0]), \
             patch.object(self.f.market, "capacities", side_effect=slow_capacity):
            self.real_poll_market(SYMBOL)
            self.assertNotIn("test", self.engine.priority_accounts)
            with self.assertRaisesRegex(TradingError, "额度快照"):
                self.engine.capacities(SYMBOL)

    def test_capacity_poll_does_not_read_or_wait_for_a_book(self):
        with patch.object(self.f.market, "book", side_effect=AssertionError("book has its own worker")):
            self.assertEqual(self.publish({10: 500000}), 2)
            self.assertEqual(self.engine.capacities(SYMBOL)[10], dec(500000))
        self.assertIn("test", self.engine.priority_accounts)

    def test_a_blocked_book_worker_does_not_delay_capacity_publication(self):
        book = self.f.market.book(SYMBOL)
        entered, release = threading.Event(), threading.Event()
        results = []

        def blocked_book(symbol):
            entered.set()
            release.wait(3)
            return book

        with patch.object(self.f.market, "book", side_effect=blocked_book):
            worker = threading.Thread(target=lambda: results.append(self.engine.poll_book(SYMBOL)))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertEqual(self.publish({10: 500000}), 2)
                self.assertIn("test", self.engine.priority_accounts)
                self.assertEqual(self.engine.capacities(SYMBOL)[10], dec(500000))
                self.assertTrue(worker.is_alive())
            finally:
                release.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [5])

    def test_a_blocked_depth_worker_does_not_delay_capacity_publication(self):
        depth = self.f.market.depth(SYMBOL)
        entered, release = threading.Event(), threading.Event()
        results = []

        def blocked_depth(symbol):
            entered.set()
            release.wait(3)
            return depth

        with patch.object(self.f.market, "depth", side_effect=blocked_depth):
            worker = threading.Thread(target=lambda: results.append(self.engine.poll_depth(SYMBOL)))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertEqual(self.publish({10: 500000}), 2)
                self.assertIn("test", self.engine.priority_accounts)
                self.assertEqual(self.engine.capacities(SYMBOL)[10], dec(500000))
                self.assertTrue(worker.is_alive())
            finally:
                release.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [10])


class PriorityCapacityExecutionTests(PriorityCapacityFixture):
    def test_budget_denial_keeps_priority_continuation_and_the_full_backoff(self):
        self.publish({10: 500000})
        started = time.monotonic()
        with patch.object(self.f.broker, "snapshot", side_effect=BudgetWait("预算耗尽", retry_after=45)), \
             patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            delay = self.take_priority_tick()
        self.assertEqual(delay, 45)
        self.assertGreaterEqual(self.engine.account_backoff["test"], started + 45)
        self.assertIn("test", self.engine.priority_followups)
        self.assertIsNone(self.f.store.intent("test"))
        submit.assert_not_called()

    def test_priority_10x_upgrade_confirms_then_opens_before_another_upgrade(self):
        self.publish({5: 500000, 10: 500000, 20: 500000})
        with patch.object(self.f.broker, "set_leverage", wraps=self.f.broker.set_leverage) as change, \
             patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            self.take_priority_tick()
            change.assert_called_once_with(SYMBOL, 10)
            self.assertEqual(self.f.store.intent("test")["target"], 10)
            self.assertIn("test", self.engine.priority_followups)
            self.assertNotIn("test", self.engine.active_priority_accounts)
            submit.assert_not_called()

            self.take_priority_tick()
            self.assertIsNone(self.f.store.intent("test"))
            self.assertEqual(self.f.store.get("open_after_leverage:test:" + SYMBOL), 10)
            self.assertIn("test", self.engine.priority_followups)
            submit.assert_not_called()

            self.take_priority_tick()
            submit.assert_called_once()
            self.assertEqual(change.call_count, 1)
            self.assertEqual({order["symbol"] for order in submit.call_args.args[0]}, {SYMBOL})
            self.assertIsNone(self.f.store.intent("test"))
            self.assertIsNone(self.f.store.get("open_after_leverage:test:" + SYMBOL))
            self.assertNotIn("test", self.engine.priority_followups)
            self.assertEqual(self.f.broker.state["leverages"][SYMBOL], 10)

    def test_20x_is_used_when_10x_capacity_is_unavailable(self):
        self.publish({5: 500000, 10: 10000, 20: 500000})
        with patch.object(self.f.broker, "set_leverage", wraps=self.f.broker.set_leverage) as change, \
             patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            self.take_priority_tick()
            change.assert_called_once_with(SYMBOL, 20)
            submit.assert_not_called()
            self.take_priority_tick()
            submit.assert_not_called()
            self.take_priority_tick()
            submit.assert_called_once()
        self.assertEqual(self.f.broker.state["leverages"][SYMBOL], 20)

    def test_a_still_pending_confirmation_does_not_requeue_immediate_work(self):
        self.publish({10: 500000})
        self.take_priority_tick()
        with patch("trading.engine.Executor.reconcile", return_value="等待账户确认"):
            self.take_priority_tick()
        self.assertIsNotNone(self.f.store.intent("test"))
        self.assertIn("test", self.engine.urgent_accounts)
        self.assertNotIn("test", self.engine.priority_followups)

    def test_capacity_dropping_after_confirmation_prevents_opening(self):
        self.publish({10: 500000})
        with patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            self.take_priority_tick()
            self.take_priority_tick()
            self.assertIsNone(self.f.store.intent("test"))
            self.publish({10: 0, 20: 0})
            self.take_priority_tick()
            submit.assert_not_called()
        self.assertEqual(self.f.broker.state["leverages"][SYMBOL], 10)
        self.assertNotIn("test", self.engine.priority_followups)

    def test_capacity_lost_during_fresh_account_read_prevents_the_upgrade_write(self):
        self.publish({10: 500000})
        snapshot = self.f.broker.snapshot

        def fresh_read(symbols, fresh_modes=False):
            result = snapshot(symbols, fresh_modes=fresh_modes)
            if fresh_modes:
                self.publish({10: 0, 20: 0})
            return result

        with patch.object(self.f.broker, "snapshot", side_effect=fresh_read), \
             patch.object(self.f.broker, "set_leverage", wraps=self.f.broker.set_leverage) as change:
            self.take_priority_tick()
        change.assert_not_called()
        self.assertIsNone(self.f.store.intent("test"))
        self.assertNotIn("test", self.engine.priority_followups)

    def test_priority_upgrade_still_respects_a_higher_opening_floor(self):
        self.f.account["policy"]["min_open_leverage"] = 20
        self.f.store.save_account(self.f.account)
        self.publish({10: 500000})
        with patch.object(self.f.broker, "set_leverage", wraps=self.f.broker.set_leverage) as change, \
             patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            self.take_priority_tick()
            change.assert_called_once_with(SYMBOL, 10)
            self.take_priority_tick()
            self.take_priority_tick()
            submit.assert_not_called()
        self.assertIn("低于 20x", self.engine.views["test"]["strategies"][SYMBOL]["reason"])


class PriorityCapacitySchedulerTests(PriorityCapacityFixture):
    @contextmanager
    def running_scheduler(self, tick):
        timing = {"test": {"interval": 60, "gap": 60}}

        def worker(aid):
            try:
                return tick(aid)
            finally:
                with self.engine.lock:
                    self.engine.active_priority_accounts.discard(aid)

        with patch.object(self.engine, "scheduling", return_value=timing), \
             patch.object(self.engine, "tick_account", side_effect=worker), \
             patch.object(self.engine, "poll_market", return_value=60), \
             patch.object(self.engine, "poll_book", return_value=60), \
             patch.object(self.engine, "poll_depth", return_value=60), \
             patch.object(self.engine, "notify", return_value=60):
            self.engine.start()
            try:
                yield
            finally:
                self.engine.stop()

    def test_opportunity_bypasses_the_ordinary_interval_and_start_gap(self):
        first, second = threading.Event(), threading.Event()
        calls = []

        def tick(aid):
            calls.append((time.monotonic(), aid in self.engine.active_priority_accounts))
            (first if len(calls) == 1 else second).set()
            return 60

        with self.running_scheduler(tick):
            self.assertTrue(first.wait(1))
            started = time.monotonic()
            self.publish({10: 500000})
            self.assertTrue(second.wait(1), "capacity opportunity waited for the ordinary schedule")
            self.assertLess(calls[1][0] - started, 1)
            self.assertTrue(calls[1][1])

    def test_scheduler_completes_upgrade_confirmation_and_first_open_without_ordinary_wait(self):
        self.publish({5: 0, 10: 0, 20: 0})
        ordinary_done, opened = threading.Event(), threading.Event()
        real_tick, real_submit = self.engine.tick_account, self.f.broker.submit
        calls, confirmed_at_submission = [], []

        def tick(aid):
            calls.append(time.monotonic())
            delay = real_tick(aid)
            if len(calls) == 1:
                ordinary_done.set()
            elif confirmed_at_submission and not self.f.store.intent(aid):
                opened.set()
            return delay

        def submit(orders):
            confirmed_at_submission.append(self.f.store.get("open_after_leverage:test:" + SYMBOL))
            return real_submit(orders)

        with patch.object(self.f.broker, "submit", side_effect=submit) as sending, \
             patch.object(self.f.broker, "set_leverage", wraps=self.f.broker.set_leverage) as change, \
             self.running_scheduler(tick):
            self.assertTrue(ordinary_done.wait(1))
            sending.assert_not_called()
            started = time.monotonic()
            self.publish({5: 500000, 10: 500000, 20: 500000})
            self.assertTrue(opened.wait(3), "upgrade/confirmation/opening waited for the ordinary interval")
            self.assertLess(time.monotonic() - started, 3)
            change.assert_called_once_with(SYMBOL, 10)
            sending.assert_called_once()
            self.assertEqual(confirmed_at_submission, [10])
            self.assertIsNone(self.f.store.intent("test"))
            self.assertIsNone(self.f.store.get("open_after_leverage:test:" + SYMBOL))

    def test_signals_during_a_busy_account_merge_into_one_following_run(self):
        first, second, third = threading.Event(), threading.Event(), threading.Event()
        release = threading.Event()
        calls = []

        def tick(aid):
            calls.append(aid)
            if len(calls) == 1:
                first.set()
                release.wait(3)
            elif len(calls) == 2:
                second.set()
            else:
                third.set()
            return 60

        with self.running_scheduler(tick):
            try:
                self.assertTrue(first.wait(1))
                self.publish({10: 500000, 20: 0})
                self.publish({10: 500000, 20: 500000})
                self.publish({10: 499999, 20: 499999})
                self.assertFalse(second.wait(.15), "the same account ran concurrently")
                release.set()
                self.assertTrue(second.wait(1), "the queued capacity signal was lost")
                self.assertFalse(third.wait(.2), "duplicate samples queued redundant account work")
            finally:
                release.set()
        self.assertEqual(calls, ["test", "test"])

    def test_capacity_priority_waits_until_the_hard_backoff_deadline(self):
        first, second = threading.Event(), threading.Event()
        calls, deadline = [], []

        def tick(aid):
            calls.append(time.monotonic())
            if len(calls) == 1:
                with self.engine.lock:
                    deadline.append(time.monotonic() + .6)
                    self.engine.account_backoff[aid] = deadline[0]
                first.set()
            else:
                second.set()
            return 60

        with self.running_scheduler(tick):
            self.assertTrue(first.wait(1))
            self.publish({10: 500000})
            self.assertFalse(second.wait(.2), "priority erased an exchange or budget backoff")
            self.assertTrue(second.wait(1), "priority did not resume after its hard backoff")
            self.assertGreaterEqual(calls[1], deadline[0])

    def test_a_failed_priority_attempt_retries_without_the_ordinary_interval(self):
        first, denied, recovered = threading.Event(), threading.Event(), threading.Event()
        real_tick = self.engine.tick_account
        calls, retry_deadline = [], []

        def tick(aid):
            calls.append(time.monotonic())
            if len(calls) == 1:
                first.set()
                return 60
            if len(calls) == 2:
                with patch.object(self.f.broker, "snapshot", side_effect=BudgetWait("预算耗尽", retry_after=45)):
                    delay = real_tick(aid)
                # The exact 45-second delay is verified above. Shorten only
                # this test deadline to exercise expiration in bounded time.
                with self.engine.lock:
                    retry_deadline.append(time.monotonic() + .35)
                    self.engine.account_backoff[aid] = retry_deadline[0]
                denied.set()
                return delay
            delay = real_tick(aid)
            recovered.set()
            return delay

        with self.running_scheduler(tick):
            self.assertTrue(first.wait(1))
            self.publish({10: 500000})
            self.assertTrue(denied.wait(1))
            self.assertFalse(recovered.wait(.15), "the failed attempt ignored its backoff")
            self.assertTrue(recovered.wait(1), "retry fell back to the 60-second ordinary schedule")
            self.assertGreaterEqual(calls[2], retry_deadline[0])
            self.assertEqual(self.f.broker.state["leverages"][SYMBOL], 10)


if __name__ == "__main__":
    unittest.main()
