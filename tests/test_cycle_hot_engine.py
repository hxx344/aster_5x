"""Account maintenance stays independent of trading and retains revocation."""
from copy import deepcopy
from dataclasses import replace
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests.helpers import Fixture
from tests.test_cycle_ws_scheduler import _Future, _Harness
from trading.account_cache import CycleAccountCache, HotAccountUnavailable
from trading.cycle import DEFAULT_CYCLE
from trading.engine import CYCLE_HOT_POLL_INTERVAL, Engine, snapshot_json
from trading.exchange import ExchangeError, LiveBroker


SYMBOL = "XAUUSD1"


class CycleHotEngineTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.row = deepcopy(self.f.account)
        self.row.update(mode="live", cycle={**DEFAULT_CYCLE, "enabled": True})
        self.f.store.save_account(self.row)
        self.engine = Engine(self.f.store, market=self.f.market)
        self.engine.live_allowed = Mock(return_value=True)
        self.api = SimpleNamespace(budget=None, close=Mock(),
            call=Mock(side_effect=AssertionError("Engine test reached a live API")))
        self.broker = LiveBroker({}, self.f.market, api=self.api)
        self.engine.brokers["test"] = self.broker
        self.broker.start_cycle_hot_data = Mock()
        self.broker.invalidate_cycle_hot_data = Mock(wraps=self.broker.invalidate_cycle_hot_data)
        self.broker.refresh_cycle_hot_snapshot = Mock(side_effect=self.publish)
        self.warm()

    def warm(self):
        self.broker.cycle_cache.configure([SYMBOL])
        self.broker.cycle_cache.set_connected(True)
        self.assertTrue(self.publish())
        return self.broker.cycle_cache.lease([SYMBOL])

    def publish(self):
        started = time.monotonic()
        ticket = self.broker.cycle_cache.begin_refresh()
        return self.broker.cycle_cache.publish(ticket, self.f.broker.snapshot([SYMBOL]), started)

    def live_harness(self):
        harness = _Harness(self.f)
        row = self.f.store.account("test")
        row["mode"] = "live"
        self.f.store.save_account(row)
        harness.engine.live_allowed = Mock(return_value=True)
        broker = Mock(spec=LiveBroker)
        broker.cycle_cache = CycleAccountCache()
        broker.api = SimpleNamespace(budget=None, call=Mock(side_effect=AssertionError("scheduler made HTTP")))
        broker.refresh_cycle_hot_snapshot.return_value = False
        harness.engine.brokers["test"] = broker
        return harness, broker

    def test_dashboard_uses_background_account_refresh_without_waiting_for_a_trade_tick(self):
        old = snapshot_json(replace(self.f.broker.snapshot([SYMBOL]), timestamp=time.time() - 60), [SYMBOL])
        self.engine.view("test", status="waiting", reason="等待价差", snapshot=old, credential_ready=True)
        self.f.broker.state["wallet"] = "30000"
        self.f.broker.state["positions"][SYMBOL + ":LONG"].update(qty="1", entry="4400")
        self.f.broker.save()
        self.assertEqual(self.engine.poll_cycle_hot_data("test"), CYCLE_HOT_POLL_INTERVAL)
        reads = self.broker.refresh_cycle_hot_snapshot.call_count
        with patch.object(self.engine, "tick_account", side_effect=AssertionError("display must not execute strategy")), \
             patch.object(self.broker, "cycle_snapshot", side_effect=AssertionError("state must not make REST reads")):
            current = self.engine.state()["accounts"][0]
        self.assertEqual(current["snapshot"]["wallet"], "30000")
        self.assertEqual(next(p["qty"] for p in current["snapshot"]["positions"]
                              if p["symbol"] == SYMBOL and p["side"] == "LONG"), "1")
        self.assertGreater(current["snapshot"]["timestamp"], old["timestamp"])
        self.assertEqual((current["status"], current["reason"]), ("waiting", "等待价差"))
        self.assertEqual(self.engine.views["test"]["snapshot"], old)
        self.assertEqual(self.broker.refresh_cycle_hot_snapshot.call_count, reads)
        self.assertFalse(self.f.broker.state["orders"])
        self.api.call.assert_not_called()

    def test_dashboard_does_not_replace_a_newer_execution_snapshot(self):
        current = snapshot_json(replace(self.f.broker.snapshot([SYMBOL]), timestamp=time.time() + .1), [SYMBOL])
        self.engine.view("test", snapshot=current)
        self.assertEqual(self.engine.state()["accounts"][0]["snapshot"], current)
        self.api.call.assert_not_called()

    def test_cache_revocation_keeps_the_latest_display_without_reverting_or_renewing_it(self):
        old = snapshot_json(replace(self.f.broker.snapshot([SYMBOL]), timestamp=time.time() - 60), [SYMBOL])
        self.engine.view("test", snapshot=old)
        displayed = self.engine.state()["accounts"][0]["snapshot"]
        self.assertGreater(displayed["timestamp"], old["timestamp"])
        self.broker.invalidate_cycle_hot_data("账户更新暂不可用")
        self.assertEqual(self.engine.state()["accounts"][0]["snapshot"], displayed)
        newer = snapshot_json(replace(self.f.broker.snapshot([SYMBOL]), timestamp=time.time() + .1), [SYMBOL])
        self.engine.view("test", snapshot=newer)
        self.assertEqual(self.engine.state()["accounts"][0]["snapshot"], newer)
        self.engine.view("test", snapshot=old)
        self.assertEqual(self.engine.state()["accounts"][0]["snapshot"], newer)
        self.api.call.assert_not_called()

    def test_unavailable_background_data_keeps_original_display_and_timestamp(self):
        old = snapshot_json(replace(self.f.broker.snapshot([SYMBOL]), timestamp=time.time() - 60), [SYMBOL])
        self.engine.view("test", snapshot=old)
        for failure in ("disconnected", "invalidated", "expired", "wrong_symbol", "read_failed"):
            with self.subTest(failure=failure):
                self.broker.cycle_cache = CycleAccountCache()
                self.warm()
                if failure == "disconnected":
                    self.broker.cycle_cache.set_connected(False)
                elif failure == "invalidated":
                    self.broker.invalidate_cycle_hot_data("订单变化")
                elif failure == "expired":
                    self.broker.cycle_cache._monotonic = lambda: time.monotonic() + 9
                elif failure == "wrong_symbol":
                    self.broker.cycle_cache.configure(["CLUSD1"])
                else:
                    ticket = self.broker.cycle_cache.begin_refresh()
                    self.broker.cycle_cache.fail(ticket, RuntimeError("read failed"))
                self.assertEqual(self.engine.state()["accounts"][0]["snapshot"], old)
        self.api.call.assert_not_called()

    def test_revocation_during_response_preparation_cannot_publish_old_account_data(self):
        old = snapshot_json(replace(self.f.broker.snapshot([SYMBOL]), timestamp=time.time() - 60), [SYMBOL])
        self.engine.view("test", snapshot=old)

        def serialize_then_revoke(*args):
            result = snapshot_json(*args)
            self.broker.invalidate_cycle_hot_data("提交订单后账户数据已失效")
            return result

        with patch("trading.engine.snapshot_json", side_effect=serialize_then_revoke):
            self.assertEqual(self.engine.state()["accounts"][0]["snapshot"], old)
        self.api.call.assert_not_called()

    def test_other_or_paused_accounts_do_not_use_this_accounts_background_snapshot(self):
        old = snapshot_json(replace(self.f.broker.snapshot([SYMBOL]), timestamp=time.time() - 60), [SYMBOL])
        self.engine.view("test", snapshot=old)
        other = {**self.row, "id": "other", "name": "其他账户"}
        self.f.store.save_account(other)
        self.engine.view("other", snapshot=old)
        views = {a["id"]: a for a in self.engine.state()["accounts"]}
        self.assertEqual(views["other"]["snapshot"], old)
        self.f.store.save_account({**self.row, "enabled": False})
        with patch.object(self.broker.cycle_cache, "lease", side_effect=AssertionError("paused account must not consume hot data")):
            paused = next(a for a in self.engine.state()["accounts"] if a["id"] == "test")
        self.assertEqual(paused["snapshot"], views["test"]["snapshot"])
        self.api.call.assert_not_called()

    def test_inactive_accounts_stop_existing_stream_without_creating_a_broker(self):
        for state in ("paused", "cycle_disabled", "paper", "not_authorized", "shutdown", "missing"):
            with self.subTest(state=state):
                row = deepcopy(self.row)
                if state == "paused":
                    row["enabled"] = False
                elif state == "cycle_disabled":
                    row["cycle"]["enabled"] = False
                elif state == "paper":
                    row["mode"] = "paper"
                self.f.store.save_account(row)
                self.engine.live_allowed.return_value = state != "not_authorized"
                self.engine.shutdown.clear()
                if state == "shutdown":
                    self.engine.shutdown.set()
                self.engine.brokers.clear()
                with patch.object(self.engine, "broker", side_effect=AssertionError("inactive account created a broker")), \
                     patch.object(self.f.store, "account", return_value=None if state == "missing" else row):
                    self.assertEqual(self.engine.poll_cycle_hot_data("test"), 30)
                    self.assertEqual(self.engine.poll_cycle_history("test"), 30)
                    self.engine.brokers["test"] = self.broker
                    lease = self.warm()
                    with patch.object(self.broker, "stop_cycle_hot_data", wraps=self.broker.stop_cycle_hot_data) as stop:
                        self.assertEqual(self.engine.poll_cycle_hot_data("test"), 30)
                        stop.assert_called_once_with()
                    with self.assertRaises(HotAccountUnavailable):
                        lease.require_fresh()
                self.broker.start_cycle_hot_data.assert_not_called()
                self.broker.refresh_cycle_hot_snapshot.assert_not_called()
        self.api.call.assert_not_called()

    def test_background_refresh_does_not_wait_for_the_account_execution_lock(self):
        finished = threading.Event()
        results = []

        def refresh():
            try:
                results.append(self.engine.poll_cycle_hot_data("test"))
            except BaseException as exc:
                results.append(exc)
            finally:
                finished.set()

        worker = threading.Thread(target=refresh)
        with self.engine.account_lock("test"):
            worker.start()
            independent = finished.wait(1)
        worker.join(1)
        self.assertTrue(independent, "background account refresh waited for the trade lock")
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [CYCLE_HOT_POLL_INTERVAL])
        self.api.call.assert_not_called()

    def test_pending_intent_or_post_fill_marker_blocks_refresh_and_history(self):
        for marker in ("intent", "post_fill"):
            with self.subTest(marker=marker):
                lease = self.warm()
                self.broker.refresh_cycle_hot_snapshot.reset_mock()
                self.f.store.put("post_fill_check:test", {"intent_id": "pending"} if marker == "post_fill" else None)
                with patch.object(self.f.store, "intent", return_value={"id": "pending"} if marker == "intent" else None), \
                     patch("trading.engine.CycleExecutor") as executor, \
                     patch.object(self.f.store, "cycle_volume_backlog") as backlog:
                    started = time.monotonic()
                    self.assertEqual(self.engine.poll_cycle_hot_data("test"), CYCLE_HOT_POLL_INTERVAL)
                    self.assertEqual(self.engine.poll_cycle_history("test"), 2)
                    self.broker.refresh_cycle_hot_snapshot.assert_not_called()
                    executor.assert_not_called()
                    backlog.assert_not_called()
                self.assertGreaterEqual(self.engine.cycle_hot_backoff["test"], started + CYCLE_HOT_POLL_INTERVAL)
                with self.assertRaises(HotAccountUnavailable):
                    lease.require_fresh()

    def test_state_or_recovery_change_during_refresh_revokes_its_published_lease(self):
        for change in ("paused", "configuration", "shutdown", "intent", "post_fill"):
            with self.subTest(change=change):
                self.f.store.save_account(deepcopy(self.row))
                self.f.store.put("post_fill_check:test", None)
                self.engine.shutdown.clear()
                self.warm()
                leases = []
                current_intent = {"value": None}

                def refresh_then_change():
                    self.assertTrue(self.publish())
                    leases.append(self.broker.cycle_cache.lease([SYMBOL]))
                    row = self.f.store.account("test")
                    if change == "paused":
                        row["enabled"] = False
                    elif change == "configuration":
                        row["cycle"]["symbol"] = "CLUSD1"
                    elif change == "shutdown":
                        self.engine.shutdown.set()
                    elif change == "intent":
                        current_intent["value"] = {"id": "new_batch"}
                    else:
                        self.f.store.put("post_fill_check:test", {"intent_id": "new_batch"})
                    self.f.store.save_account(row)
                    return True

                self.broker.refresh_cycle_hot_snapshot.side_effect = refresh_then_change
                with patch.object(self.f.store, "intent", side_effect=lambda aid: current_intent["value"]), \
                     patch.object(self.engine, "cycle_hot_ready") as ready:
                    self.assertEqual(self.engine.poll_cycle_hot_data("test"), CYCLE_HOT_POLL_INTERVAL)
                    ready.assert_not_called()
                self.assertEqual(len(leases), 1)
                with self.assertRaises(HotAccountUnavailable):
                    leases[0].require_fresh()
        self.api.call.assert_not_called()

    def test_pause_configuration_and_stop_revoke_issued_leases_before_waiting(self):
        lease = self.warm()
        self.engine.enable("test", False)
        with self.assertRaises(HotAccountUnavailable):
            lease.require_fresh()
        lease = self.warm()
        self.engine.configure("test", {"cycle": {"hold_seconds": 120}})
        with self.assertRaises(HotAccountUnavailable):
            lease.require_fresh()
        lease = self.warm()

        def check_revoked_before_join(**kwargs):
            with self.assertRaises(HotAccountUnavailable):
                lease.require_fresh()
            self.api.close.assert_not_called()

        thread = Mock()
        thread.join.side_effect = check_revoked_before_join
        thread.is_alive.return_value = True
        self.engine.thread = thread
        self.engine.stop()
        thread.join.assert_called_once()
        self.engine.thread = None
        self.api.call.assert_not_called()

    def test_scheduler_refresh_and_history_have_slots_independent_of_busy_trade_worker(self):
        harness, broker = self.live_harness()
        held_trade, held_refresh = _Future(done=False), _Future(CYCLE_HOT_POLL_INTERVAL, done=False)
        harness.on_submit = lambda aid, signal: held_trade
        hot = Mock(side_effect=[held_refresh, CYCLE_HOT_POLL_INTERVAL])
        history = Mock(return_value=60)

        def control(step):
            if step == 1:
                self.assertEqual(len(harness.calls), 1)
                self.assertFalse(held_trade.done())
                hot.assert_called_once_with("test")
                history.assert_called_once_with("test")
                for _ in range(20):
                    harness.engine.wake_cycle_hot_data("test")
                harness.ticks = 101
            elif step == 2:
                hot.assert_called_once_with("test")
                held_refresh.complete = True
                harness.ticks = 102
            elif step == 3:
                self.assertEqual(hot.call_count, 2)
                self.assertEqual(len(harness.calls), 1)
                self.assertFalse(held_trade.done())
                harness.engine.shutdown.set()

        with patch.object(harness.engine, "poll_cycle_hot_data", hot), \
             patch.object(harness.engine, "poll_cycle_history", history):
            harness.run(control)
        self.assertEqual(hot.call_count, 2)
        broker.close.assert_called_once_with()
        broker.api.call.assert_not_called()

    def test_scheduler_rate_backoff_survives_repeated_wakes_without_busy_refresh(self):
        harness, broker = self.live_harness()
        attempts = []

        def rate_limited():
            attempts.append(harness.ticks)
            raise ExchangeError("限流", retry_after=30)

        broker.refresh_cycle_hot_snapshot.side_effect = rate_limited

        def control(step):
            if step < 5:
                self.assertEqual(attempts, [100])
                harness.ticks = {1: 101, 2: 110, 3: 129.999, 4: 130}[step]
                for _ in range(20):
                    harness.engine.wake_cycle_hot_data("test")
            else:
                self.assertEqual(attempts, [100, 130])
                harness.engine.shutdown.set()

        with patch.object(harness.engine, "poll_cycle_history", return_value=60):
            harness.run(control)
        self.assertEqual(attempts, [100, 130])
        self.assertEqual(harness.iteration, 5)
        broker.api.call.assert_not_called()

    def test_unexpected_background_worker_failure_retains_backoff_across_repeated_wakes(self):
        harness, broker = self.live_harness()
        attempts = []

        def failed_worker(aid):
            attempts.append(harness.ticks)
            return _Future(error=RuntimeError("unexpected background failure"))

        def control(step):
            if step < 5:
                self.assertEqual(attempts, [100])
                # Consume the failed future at time 100 before advancing time.
                harness.ticks = {1: 100, 2: 101, 3: 129.999, 4: 130}[step]
                if step >= 2:
                    self.assertEqual(harness.engine.cycle_hot_backoff["test"], 130)
                for _ in range(20):
                    harness.engine.wake_cycle_hot_data("test")
            else:
                self.assertEqual(attempts, [100, 130])
                harness.engine.shutdown.set()

        with patch.object(harness.engine, "poll_cycle_hot_data", side_effect=failed_worker), \
             patch.object(harness.engine, "poll_cycle_history", return_value=60), \
             self.assertLogs("aster.trading", level="ERROR") as logs:
            harness.run(control)
        self.assertEqual(attempts, [100, 130])
        self.assertEqual(harness.iteration, 5)
        self.assertTrue(any("cycle-data:test" in entry for entry in logs.output))
        broker.api.call.assert_not_called()

    def test_disabled_stream_shutdown_does_not_schedule_immediate_repeat_polling(self):
        harness, _ = self.live_harness()
        harness.engine.brokers["test"] = self.broker
        row = self.f.store.account("test")
        row["enabled"] = False
        self.f.store.save_account(row)
        self.broker.cycle_cache.set_listener(lambda: harness.engine.wake_cycle_hot_data("test"))
        stream = Mock()
        stream.close.side_effect = lambda: self.broker.cycle_cache.set_connected(False)
        self.broker.cycle_stream = stream
        attempts = []
        original_poll = harness.engine.poll_cycle_hot_data

        def poll(aid):
            attempts.append(harness.ticks)
            return original_poll(aid)

        def control(step):
            if step < 5:
                self.assertEqual(attempts, [100])
                harness.ticks = {1: 100, 2: 101, 3: 129.999, 4: 130}[step]
            else:
                self.assertEqual(attempts, [100, 130])
                harness.engine.shutdown.set()

        with patch.object(harness.engine, "poll_cycle_hot_data", side_effect=poll), \
             patch.object(harness.engine, "poll_cycle_history", return_value=60):
            harness.run(control)
        self.assertEqual(attempts, [100, 130])
        stream.close.assert_called_once_with()
        self.broker.start_cycle_hot_data.assert_not_called()
        self.broker.refresh_cycle_hot_snapshot.assert_not_called()
        self.api.call.assert_not_called()

    def test_successful_publish_rearms_cycle_without_advancing_ordinary_turn(self):
        harness, broker = self.live_harness()

        def control(step):
            if step == 2:
                harness.emit(101)
                # This public opportunity was already checked while hot data
                # was missing. A successful publication must rearm it.
                harness.engine.cycle_signal_seen["test"] = (SYMBOL, "bbo", 101, harness.engine.accounts_generation)
                harness.engine.cycle_signal_ready["test"] = (SYMBOL, "open", harness.engine.accounts_generation)
                harness.engine.cycle_hot_waiting.add("test")
                broker.refresh_cycle_hot_snapshot.return_value = True
                cache = broker.cycle_cache
                cache.configure([SYMBOL])
                cache.set_connected(True)
                cache.publish(cache.begin_refresh(), self.f.broker.snapshot([SYMBOL]), time.monotonic())
                harness.engine.wake_cycle_hot_data("test")
            elif step == 3:
                self.assertNotIn("test", harness.engine.cycle_hot_waiting)
                self.assertNotIn("test", harness.engine.wake_accounts)
                harness.ticks = 101.1
            elif step == 4:
                harness.ticks = 129
            elif step == 5:
                harness.ticks = 130
            elif step == 6:
                harness.engine.shutdown.set()

        with patch.object(harness.engine, "poll_cycle_history", return_value=60):
            harness.run(control)
        self.assertEqual([(call["at"], call["signal"] is not None) for call in harness.calls],
                         [(100, False), (101.1, True), (130, False)])
        broker.api.call.assert_not_called()

    def test_history_backfills_completed_batches_without_account_refresh_or_order_work(self):
        recent = [{"id": "completed_recent"}, {"id": "completed_second"}]
        executor = Mock()
        executor.sync_volume.return_value = True
        with patch("trading.engine.CycleExecutor", return_value=executor), \
             patch.object(self.f.store, "cycle_volume_backlog", return_value=recent) as backlog, \
             patch.object(self.engine, "cycle_hot_ready") as ready:
            self.assertEqual(self.engine.poll_cycle_history("test"), 5)
            self.assertEqual([call.args[1] for call in executor.sync_volume.call_args_list], recent)
            self.assertEqual(backlog.call_args.kwargs["limit"], 4)
            self.assertGreater(backlog.call_args.kwargs["since"], 0)
            ready.assert_called_once_with("test")
            executor.sync_volume.reset_mock()
            ready.reset_mock()
            backlog.side_effect = [[], [{"id": "completed_old"}]]
            executor.sync_volume.side_effect = ExchangeError("历史回补限流", retry_after=41)
            self.assertEqual(self.engine.poll_cycle_history("test"), 41)
            self.assertEqual(backlog.call_args.kwargs, {"limit": 1, "since": 0})
            ready.assert_not_called()
        self.broker.refresh_cycle_hot_snapshot.assert_not_called()
        self.broker.start_cycle_hot_data.assert_not_called()
        self.api.call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
