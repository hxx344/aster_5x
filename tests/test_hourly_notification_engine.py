"""Hourly notifications reuse local dashboard snapshots and the shared outbox."""
from contextlib import ExitStack
import os
import unittest
from unittest.mock import patch

import monitor
from trading.engine import Engine
from trading.exchange import ExchangeError
from tests.helpers import Fixture


WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/test-hourly-summary"


class HourlyNotificationEngineTests(unittest.TestCase):
    def setUp(self):
        self.now = 3500.0
        clock = patch("trading.engine.time.time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        env = patch.dict(os.environ, {"FEISHU_WEBHOOK_URL": WEBHOOK, "ASTER_CAPACITY_ALERT_ENABLED": "0"}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.engine = Engine(self.f.store, market=self.f.market)
        self.addCleanup(self.engine.dashboard_reports.close)
        sender = patch("monitor.send_feishu")
        self.sender = sender.start()
        self.addCleanup(sender.stop)

    def at_hour(self):
        self.engine.notify()
        self.now = 3600

    def queue_trade(self):
        with self.f.store.connect() as db:
            db.execute("INSERT INTO outbox(id,message,due_at,category,symbols) VALUES ('trade','trade message',0,'trade_summary','[]')")

    def test_regular_ticks_only_check_timer_and_due_snapshot_is_compact(self):
        with patch.object(self.engine, "state", return_value={"snapshot": "cached"}) as state, \
             patch("trading.engine.format_hourly_summary", return_value="hourly") as formatter:
            for now in (3500, 3505, 3550, 3599.9):
                self.now = now
                self.assertEqual(self.engine.notify(), 5)
            state.assert_not_called()
            formatter.assert_not_called()
            self.now = 3600
            self.engine.notify()
            state.assert_called_once_with(background_reports=True, compact=True)
            formatter.assert_called_once_with({"snapshot": "cached"}, 3600)
            self.engine.notify()
            self.assertEqual(state.call_count, 1)
        self.sender.assert_called_once()
        self.assertEqual(self.sender.call_args.args[1], "hourly")
        self.assertEqual(self.f.store.hourly_summary_status(available=True)["last_sent_at"], 3600)

    def test_real_formatter_and_state_make_no_market_or_broker_requests(self):
        self.at_hour()
        with ExitStack() as stack:
            calls = [stack.enter_context(patch.object(self.f.market, method, side_effect=AssertionError("unexpected I/O")))
                     for method in ("load_rules", "book", "mark_price", "capacities", "depth")]
            broker = stack.enter_context(patch.object(self.engine, "broker", side_effect=AssertionError("unexpected broker")))
            self.engine.notify()
            for call in [*calls, broker]:
                call.assert_not_called()
        self.sender.assert_called_once()
        self.assertIsNone(self.engine.hourly_summary_error)

    def test_demo_and_missing_webhook_never_build_or_send(self):
        self.at_hour()
        self.engine.demo = True
        with patch.object(self.engine, "state") as state:
            self.engine.notify()
            self.engine.demo = False
            with patch.dict(os.environ, {"FEISHU_WEBHOOK_URL": ""}):
                self.engine.notify()
                self.assertIsNone(self.f.store.hourly_summary_status(available=False)["next_due_at"])
            self.engine.notify()
            state.assert_not_called()
        self.sender.assert_not_called()
        self.assertEqual(self.f.store.pending_notifications(), 0)
        self.assertEqual(self.f.store.hourly_summary_status(available=True)["next_due_at"], 7200)

    def test_failed_send_retries_after_backoff_without_rebuilding_snapshot(self):
        self.at_hour()
        self.sender.side_effect = monitor.MonitorError("test failure")
        with patch("trading.engine.format_hourly_summary", return_value="hourly") as formatter:
            self.engine.notify()
            self.assertEqual(self.engine.notification_error, "飞书发送失败，等待重试")
            self.now = 3605
            self.engine.notify()
            self.assertEqual(self.sender.call_count, 1)
            self.now = 3610
            self.sender.side_effect = None
            self.engine.notify()
            self.assertEqual(self.sender.call_count, 2)
            formatter.assert_called_once()
        self.assertIsNone(self.engine.notification_error)
        self.assertEqual(self.f.store.hourly_summary_status(available=True)["last_sent_at"], 3610)

    def test_settings_change_during_formatting_drops_snapshot_until_next_tick(self):
        self.at_hour()
        def changed_settings(state, now):
            self.f.store.edit_monitoring({"alerts": False}, symbol="XAUUSD1")
            return "discard me"
        with patch("trading.engine.format_hourly_summary", side_effect=changed_settings):
            self.engine.notify()
        self.sender.assert_not_called()
        self.assertEqual(self.f.store.hourly_summary_status(available=True)["next_due_at"], 3600)
        with patch("trading.engine.format_hourly_summary", return_value="fresh"):
            self.engine.notify()
        self.sender.assert_called_once()
        self.assertEqual(self.sender.call_args.args[1], "fresh")

    def test_master_disabled_during_an_earlier_send_cancels_hourly_delivery(self):
        self.at_hour()
        self.queue_trade()
        self.sender.side_effect = lambda *_: self.f.store.edit_monitoring({"feishu_enabled": False})
        self.engine.notify()
        self.sender.assert_called_once()
        self.assertEqual(self.sender.call_args.args[1], "trade message")
        self.assertFalse(self.f.store.hourly_summary_status(available=True)["pending"])

    def test_expiring_during_an_earlier_send_prevents_stale_hourly_delivery(self):
        self.at_hour()
        self.queue_trade()
        def next_hour(*_):
            self.now = 7200
        self.sender.side_effect = next_hour
        self.engine.notify()
        self.sender.assert_called_once()
        self.assertEqual(self.sender.call_args.args[1], "trade message")
        self.assertEqual(self.f.store.pending_notifications(), 0)

    def test_summary_failure_does_not_block_trade_delivery_and_retries(self):
        self.at_hour()
        self.queue_trade()
        with patch("trading.engine.format_hourly_summary", side_effect=RuntimeError("test failure")), \
             self.assertLogs("aster.trading", level="ERROR"):
            self.engine.notify()
        self.sender.assert_called_once()
        self.assertEqual(self.sender.call_args.args[1], "trade message")
        self.assertEqual(self.engine.hourly_summary_error, "每小时摘要生成失败，等待重试")
        self.engine.notify()
        self.assertEqual(self.sender.call_count, 2)
        self.assertIsNone(self.engine.hourly_summary_error)


class StartupHourlyNotificationTests(unittest.TestCase):
    setUp = HourlyNotificationEngineTests.setUp

    def run_scheduler(self, control, rules_effect, *, notify_effect=None):
        from tests.test_cycle_ws_scheduler import _Pool, _SchedulerEvent
        from tests.test_pair_capacity_cadence import save_ordinary_pair
        save_ordinary_pair(self.f.store)
        self.iteration, self.failure, self.control = 0, None, control
        self.engine.scheduler_event = _SchedulerEvent(self)
        job_calls = []
        def job(name):
            def execute(*_):
                job_calls.append((name, self.engine.ready))
                return 60
            return execute
        with ExitStack() as stack:
            stack.enter_context(patch("trading.engine.ThreadPoolExecutor", return_value=_Pool()))
            stack.enter_context(patch("trading.engine.time.monotonic", side_effect=lambda: self.now))
            stack.enter_context(patch.object(self.engine.dashboard_reports, "start"))
            rules = stack.enter_context(patch.object(self.f.market, "load_rules", side_effect=rules_effect))
            for name in ("tick_account", "poll_market", "poll_book", "poll_depth"):
                stack.enter_context(patch.object(self.engine, name, side_effect=job(name)))
            stack.enter_context(patch.object(self.engine.pairs, "tick", side_effect=job("pair")))
            notify = stack.enter_context(patch.object(self.engine, "notify", **(
                {"wraps": self.engine.notify} if notify_effect is None else {"side_effect": notify_effect})))
            self.engine.run()
        if self.failure is not None:
            raise self.failure
        return rules, job_calls, notify

    def assert_startup_summaries(self, retry_after):
        times = {1: 3500.1, 2: 3600, 3: 3600.1, 4: 7200}
        def control(step):
            if step in times:
                self.now = times[step]
            else:
                self.engine.shutdown.set()
        rules, jobs, _ = self.run_scheduler(control, ExchangeError("规则连接暂不可用", retry_after=retry_after))
        self.assertEqual(rules.call_count, 1 if retry_after > 3600 else 3)
        self.assertEqual(jobs, [])
        self.assertEqual(self.sender.call_count, 2)
        for call in self.sender.call_args_list:
            self.assertIn("服务：未就绪", call.args[1])
            self.assertIn("规则连接暂不可用", call.args[1])
        self.assertEqual(self.f.store.hourly_summary_status(available=True)["last_sent_at"], 7200)

    def test_repeated_initial_rule_failures_still_send_hourly_status(self):
        self.assert_startup_summaries(0)

    def test_long_rule_retry_after_does_not_block_multiple_hourly_summaries(self):
        self.assert_startup_summaries(7200)

    def test_pending_notification_never_overlaps_during_startup_backoff(self):
        from tests.test_cycle_ws_scheduler import _Future
        first = _Future(5, done=False)
        submissions = []
        def notify():
            submissions.append(self.now)
            return first if len(submissions) == 1 else _Future(5)
        def control(step):
            if step == 1:
                self.now = 3550
            elif step == 2:
                self.now = 3600
            elif step == 3:
                self.now = 4000
            elif step == 4:
                self.assertEqual(submissions, [3500])
                first.complete = True
            elif step == 5:
                self.now = 4005
            else:
                self.engine.shutdown.set()
        _, jobs, _ = self.run_scheduler(control, ExchangeError("retry", retry_after=7200), notify_effect=notify)
        self.assertEqual(submissions, [3500, 4005])
        self.assertEqual(jobs, [])

    def test_rule_recovery_preserves_retry_deadline_and_resumes_original_jobs(self):
        from tests.test_cycle_ws_scheduler import _Future
        attempts = []
        def load_rules():
            attempts.append(self.now)
            if len(attempts) == 1:
                raise ExchangeError("retry", retry_after=20)
        def control(step):
            if step == 1:
                self.now = 3500.1
            elif step == 2:
                self.now = 3519.9
            elif step == 3:
                self.now = 3520
            else:
                self.assertTrue(self.engine.ready)
                self.assertIsNone(self.engine.error)
                self.engine.shutdown.set()
        _, jobs, notify = self.run_scheduler(control, load_rules, notify_effect=lambda: _Future(60))
        self.assertEqual(attempts, [3500, 3520])
        self.assertEqual({name for name, _ in jobs}, {"tick_account", "poll_market", "poll_book", "poll_depth", "pair"})
        self.assertTrue(all(ready for _, ready in jobs))
        notify.assert_called_once()

    def test_startup_notification_failure_keeps_worker_backoff_and_rule_retry_independent(self):
        from tests.test_cycle_ws_scheduler import _Future
        times = {1: 3500.1, 2: 3529.9, 3: 3530.1}
        def control(step):
            if step in times:
                self.now = times[step]
            else:
                self.engine.shutdown.set()
        with self.assertLogs("aster.trading", level="ERROR"):
            rules, jobs, notify = self.run_scheduler(control, ExchangeError("retry", retry_after=7200),
                notify_effect=[_Future(error=RuntimeError("worker failure")), _Future(5)])
        rules.assert_called_once()
        self.assertEqual(jobs, [])
        self.assertEqual(notify.call_count, 2)


if __name__ == "__main__":
    unittest.main()
