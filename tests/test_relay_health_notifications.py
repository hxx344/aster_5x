"""Relay incidents and outbox races, using only local telemetry and mock delivery."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import monitor
from trading import relay_health_notifications as health
from trading.engine import Engine
from trading.exchange import MarketData, RateBudget
from trading.store import Store


def telemetry(*, count=0, connected=False, duration=0, idle=0, sampled=True, instance="instance-a", connection=100):
    return {"enabled": True, "running": True, "closed": False, "instance_id": instance, "connected": connected,
            "ws": {"failure_count": count, "connected_at": connection if connected else None,
                   "connected_age_seconds": duration if connected else None,
                   "disconnected_age_seconds": None if connected else duration,
                   "oi_idle_seconds": idle if connected else None, "has_oi_sample": sampled if connected else False},
            "http": {"last_success_at": None, "last_error": None}}


class RelayHealthStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / "state.sqlite3")
        self.now = 1000.0
        clock = patch("trading.store.time.time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def observe(self, status, *, available=True):
        return self.store.observe_relay_health(status, available=available,
            revision=self.store.monitoring_settings()["revision"])

    def rows(self):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM outbox ORDER BY due_at,id")]

    def fault(self, *, sent=False):
        self.observe(telemetry(count=3))
        item = self.store.due_notifications()[0]
        if sent:
            self.store.notification_result(item, True)
        return item

    def healthy(self, duration, **kwargs):
        self.now += 5
        self.observe(telemetry(connected=True, duration=duration, count=3, **kwargs))

    def test_three_failures_threshold_and_single_fault(self):
        self.observe(telemetry(count=2))
        self.assertEqual(self.rows(), [])
        item = self.fault(sent=True)
        self.observe(telemetry(count=20, duration=500))
        self.assertEqual(len(self.rows()), 1)
        self.assertIsNone(self.store.notification_for_delivery(item["id"]))
        self.assertEqual(self.rows()[0]["symbols"], "[]")

    def test_disconnect_sixty_second_boundary(self):
        self.observe(telemetry(duration=59.999))
        self.assertEqual(self.rows(), [])
        self.observe(telemetry(duration=60))
        self.assertIn("持续断连已达 60 秒", self.store.due_notifications()[0]["message"])

    def test_connected_without_new_oi_for_120_seconds(self):
        self.observe(telemetry(connected=True, duration=120, idle=119.999, sampled=False))
        self.assertEqual(self.rows(), [])
        self.observe(telemetry(connected=True, duration=120, idle=120, sampled=False))
        self.assertIn("120 秒没有新的有效公开额度样本", self.store.due_notifications()[0]["message"])

    def test_recovery_requires_thirty_seconds_and_successful_fault_delivery(self):
        self.fault(sent=True)
        self.healthy(10)
        self.healthy(39.999)
        self.assertEqual(self.store.due_notifications(), [])
        self.healthy(40)
        recovery = self.store.due_notifications()[0]
        self.assertIn("已恢复", recovery["message"])
        self.store.notification_result(recovery, True)
        self.healthy(50)
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.store.due_notifications(), [])

    def test_unsent_failure_is_cancelled_on_stable_recovery_without_recovery_message(self):
        item = self.fault()
        self.store.notification_result(item, False)
        self.healthy(10)
        self.healthy(40)
        self.now += 100
        self.assertEqual(self.store.due_notifications(), [])
        self.assertIsNone(self.store.notification_for_delivery(item["id"]))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["expires_at"], 0)

    def test_short_connections_do_not_reset_failure_count_or_recovery_candidate(self):
        for count in (0, 1, 2):
            self.observe(telemetry(count=count, connected=True, duration=20, connection=100 + count))
        self.assertEqual(self.rows(), [])
        self.observe(telemetry(count=3, connected=True, duration=20, connection=103))
        self.assertEqual(len(self.rows()), 1)
        self.store.notification_result(self.store.due_notifications()[0], True)
        self.observe(telemetry(count=4, connected=True, duration=25, connection=103))
        self.observe(telemetry(count=4, connected=True, duration=54.999, connection=103))
        self.assertEqual(self.store.due_notifications(), [])
        self.observe(telemetry(count=4, connected=True, duration=55, connection=103))
        self.assertIn("已恢复", self.store.due_notifications()[0]["message"])

    def test_pending_recovery_is_cancelled_if_relay_fails_again(self):
        self.fault(sent=True)
        self.healthy(10)
        self.healthy(40)
        recovery = self.store.due_notifications()[0]
        self.store.notification_result(recovery, False)
        self.observe(telemetry(count=4, duration=5))
        self.now += 100
        self.assertIsNone(self.store.notification_for_delivery(recovery["id"]))
        self.assertEqual(self.store.due_notifications(), [])
        self.observe(telemetry(count=4, connected=True, duration=10, connection=200))
        self.observe(telemetry(count=4, connected=True, duration=40, connection=200))
        self.assertEqual(self.store.due_notifications()[0]["id"], recovery["id"])

    def test_restarts_keep_incident_but_never_reuse_cross_instance_recovery_time(self):
        self.fault(sent=True)
        self.healthy(10)
        self.healthy(39)
        self.store = Store(self.store.path)
        self.observe(telemetry(instance="instance-b", connected=True, duration=200))
        self.observe(telemetry(instance="instance-b", connected=True, duration=229.999))
        self.assertEqual(len(self.rows()), 1)
        self.observe(telemetry(instance="instance-b", connected=True, duration=230))
        recovery = self.store.due_notifications()[0]
        self.store = Store(self.store.path)
        self.observe(telemetry(instance="instance-b", connected=True, duration=240))
        self.assertEqual(len(self.rows()), 2)
        self.store.notification_result(recovery, True)
        self.store = Store(self.store.path)
        self.observe(telemetry(instance="instance-c", connected=True, duration=0))
        self.assertEqual(len(self.rows()), 2)

    def test_disabled_history_is_not_replayed_and_new_failures_still_accumulate(self):
        self.store.edit_monitoring({"relay_health_alerts": False})
        self.store.edit_monitoring({"relay_health_alerts": True})
        self.observe(telemetry(count=5, duration=300))
        self.observe(telemetry(count=6, duration=305))
        self.observe(telemetry(count=7, duration=359.999))
        self.assertEqual(self.rows(), [])
        self.observe(telemetry(count=7, duration=360))
        self.assertEqual(len(self.rows()), 1)
        self.store.edit_monitoring({"feishu_enabled": False})
        self.store.edit_monitoring({"feishu_enabled": True})
        self.observe(telemetry(count=20, duration=1000))
        self.observe(telemetry(count=22, duration=1002))
        self.assertEqual(self.store.due_notifications(), [])
        self.observe(telemetry(count=23, duration=1003))
        self.assertEqual(len(self.store.due_notifications()), 1)

    def test_disabled_old_oi_age_needs_a_new_120_seconds_or_fresh_sample(self):
        self.store.edit_monitoring({"relay_health_alerts": False})
        self.store.edit_monitoring({"relay_health_alerts": True})
        self.observe(telemetry(connected=True, duration=500, idle=300))
        self.observe(telemetry(connected=True, duration=619.999, idle=419.999))
        self.assertEqual(self.rows(), [])
        self.observe(telemetry(connected=True, duration=620, idle=420))
        self.assertEqual(len(self.rows()), 1)

    def test_new_disconnect_after_reenabled_connection_has_its_own_sixty_second_timer(self):
        self.store.edit_monitoring({"relay_health_alerts": False})
        self.store.edit_monitoring({"relay_health_alerts": True})
        self.observe(telemetry(count=5, duration=300))
        self.observe(telemetry(count=5, connected=True, duration=0))
        self.observe(telemetry(count=6, duration=59.999))
        self.assertEqual(self.rows(), [])
        self.observe(telemetry(count=6, duration=60))
        self.assertEqual(len(self.rows()), 1)

    def test_inflight_recovery_success_does_not_consume_a_later_fault(self):
        original = self.fault(sent=True)
        self.healthy(0)
        self.healthy(30)
        recovery = self.store.due_notifications()[0]
        self.observe(telemetry(count=6, duration=60))
        self.assertIsNone(self.store.notification_for_delivery(recovery["id"]))
        self.store.notification_result(recovery, True)
        self.observe(telemetry(count=6, duration=60))
        current = self.store.due_notifications()[0]
        self.assertNotEqual(current["id"], original["id"])
        self.assertIn("WS 异常", current["message"])

    def test_global_health_alert_ignores_symbol_and_monitoring_switches(self):
        self.store.edit_monitoring({"alerts": False, "monitor": False}, symbol="XAUUSD1")
        self.store.edit_monitoring({"monitoring_enabled": False, "discovery_enabled": False})
        self.assertIsNotNone(self.fault())

    def test_shutdown_or_unconfigured_cancels_pending_and_rebaselines(self):
        item = self.fault()
        self.observe({**telemetry(count=3), "closed": True})
        self.assertIsNone(self.store.notification_for_delivery(item["id"]))
        self.observe(telemetry(count=20, duration=1000), available=False)
        self.observe(telemetry(count=20, duration=1000))
        self.assertEqual(self.store.due_notifications(), [])

    def test_retry_backoff_and_settings_revision_race(self):
        item = self.fault()
        self.store.notification_result(item, False)
        self.observe(telemetry(count=3))
        self.assertEqual(self.store.due_notifications(), [])
        self.assertEqual(self.rows()[0]["due_at"], 1010)
        revision = self.store.monitoring_settings()["revision"]
        self.store.edit_monitoring({"relay_health_alerts": False})
        self.store.edit_monitoring({"relay_health_alerts": True})
        self.assertFalse(self.store.observe_relay_health(telemetry(count=50), available=True, revision=revision))
        self.store.notification_result(item, True)
        self.assertEqual(self.store.get(health.KEY), {"rebaseline": True})

    def test_concurrent_observation_queues_one_incident(self):
        stores = [Store(self.store.path) for _ in range(3)]
        with ThreadPoolExecutor(3) as pool:
            futures = [pool.submit(store.observe_relay_health, telemetry(count=3), available=True, revision=0) for store in stores]
            for future in futures:
                future.result()
        self.assertEqual(len(self.rows()), 1)

    def test_failed_state_write_rolls_back_fault_enqueue(self):
        with self.store.connect() as db:
            db.execute("""CREATE TRIGGER reject_health BEFORE INSERT ON kv WHEN NEW.key='relay_health_notifications'
                BEGIN SELECT RAISE(ABORT,'failed'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.fault()
        self.assertEqual(self.rows(), [])

    def test_http_context_uses_only_recent_success_and_fixed_safe_messages(self):
        status = telemetry(count=3)
        status["http"] = {"last_success_at": self.now - 120, "last_error": None}
        self.observe(status)
        self.assertIn("最近 120 秒 HTTP 补取成功", self.rows()[0]["message"])
        status["http"]["last_success_at"] = self.now + 1
        self.observe(status)
        self.assertIn("暂无近期", self.rows()[0]["message"])
        status["http"]["last_error"] = "secret-url-token"
        self.observe(status)
        self.assertIn("HTTP 补取也有异常", self.rows()[0]["message"])
        self.assertNotIn("secret", self.rows()[0]["message"])


class RelayHealthEngineTests(unittest.TestCase):
    def setUp(self):
        RelayHealthStoreTests.setUp(self)
        env = patch.dict(os.environ, {"FEISHU_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/hook/relay-test",
                                     "ASTER_CAPACITY_ALERT_ENABLED": "0"}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.status = telemetry()
        self.relay = Mock()
        self.relay.status.side_effect = lambda: deepcopy(self.status)
        self.api = Mock()
        self.api.budget = RateBudget()
        self.market = MarketData(self.api, capacity_relay=self.relay, stream=Mock(), depth_stream=Mock())
        self.engine = Engine(self.store, market=self.market)
        self.addCleanup(self.engine.dashboard_reports.close)
        sender = patch("monitor.send_feishu")
        self.sender = sender.start()
        self.addCleanup(sender.stop)

    def queue_trade(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO outbox(id,message,due_at,category,symbols) VALUES ('trade','trade',0,'trade_summary','[]')")

    def test_health_delivery_reads_local_status_only(self):
        self.status = telemetry(count=3)
        with patch.object(self.engine, "broker") as broker, patch.object(self.engine, "state") as state:
            self.engine.notify()
        self.sender.assert_called_once()
        broker.assert_not_called()
        state.assert_not_called()
        self.api.request.assert_not_called()
        self.relay.sample.assert_not_called()
        self.assertTrue(all(call[0] == "status" for call in self.relay.method_calls))

    def test_demo_unconfigured_and_shutdown_never_send(self):
        self.status = telemetry(count=3)
        self.engine.demo = True
        self.engine.notify()
        self.engine.demo = False
        with patch.dict(os.environ, {"FEISHU_WEBHOOK_URL": ""}):
            self.engine.notify()
        self.engine.shutdown.set()
        self.engine.notify()
        self.sender.assert_not_called()

    def test_status_failure_does_not_block_trade_or_hourly_notifications(self):
        self.engine.notify()
        self.now = 3600
        self.queue_trade()
        self.relay.status.side_effect = RuntimeError("private-status-secret")
        # The shared dashboard report may independently succeed from cached state.
        with patch.object(self.engine, "state", return_value={}), self.assertLogs("aster.trading", level="ERROR") as log:
            self.engine.notify()
        self.assertEqual(self.sender.call_count, 2)
        self.assertNotIn("private-status-secret", str(log.output) + self.engine.relay_health_error)

    def test_failed_health_send_uses_retry_and_does_not_block_trade(self):
        self.status = telemetry(count=3)
        self.queue_trade()
        self.sender.side_effect = lambda config, message: (_ for _ in ()).throw(monitor.MonitorError("failed")) if "WS 异常" in message else None
        self.engine.notify()
        self.assertEqual(self.sender.call_count, 2)
        self.now = 1009
        self.engine.notify()
        self.assertEqual(self.sender.call_count, 2)
        self.now = 1010
        self.sender.side_effect = None
        self.engine.notify()
        self.assertEqual(self.sender.call_count, 3)
        self.assertTrue(self.store.get(health.KEY)["incident"]["fault_sent"])

    def test_delivery_rechecks_recovery_after_an_earlier_message(self):
        self.status = telemetry(count=3)
        config = self.engine.notification_config()
        self.engine.observe_relay_health(config)
        self.status = telemetry(count=3, connected=True, duration=0)
        self.engine.observe_relay_health(config)
        self.queue_trade()
        def sent(*_):
            self.status = telemetry(count=3, connected=True, duration=30)
        self.sender.side_effect = sent
        self.engine.notify()
        self.sender.assert_called_once()
        self.assertEqual(self.sender.call_args.args[1], "trade")
        self.assertEqual(self.store.pending_notifications(), 0)

    def test_delivery_rechecks_disabled_setting_and_configuration_revision(self):
        self.status = telemetry(count=3)
        self.queue_trade()
        self.sender.side_effect = lambda *_: self.store.edit_monitoring({"relay_health_alerts": False})
        self.engine.notify()
        self.sender.assert_called_once()
        self.assertEqual(self.sender.call_args.args[1], "trade")
        self.store.edit_monitoring({"relay_health_alerts": True})
        def changed():
            self.store.edit_monitoring({"feishu_enabled": False})
            return deepcopy(self.status)
        self.relay.status.side_effect = changed
        self.engine.notify()
        self.assertEqual(self.sender.call_count, 1)


if __name__ == "__main__":
    unittest.main()
