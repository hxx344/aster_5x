"""Destination isolation, legacy migration and fair outbox delivery; no network I/O."""
import json
import os
import unittest
from unittest.mock import patch

import monitor
from trading import notification_channels as channels
from tests import test_hourly_notification_engine as hourly_tests


PREFIX = "https://open.feishu.cn/open-apis/bot/v2/hook/"
SCHEDULED, EVENT, LEGACY = (PREFIX + name for name in ("scheduled", "event", "legacy"))
SPLIT = {"FEISHU_SCHEDULED_WEBHOOK_URL": SCHEDULED, "FEISHU_SCHEDULED_SIGN_SECRET": "scheduled-secret",
         "FEISHU_EVENT_WEBHOOK_URL": EVENT, "FEISHU_EVENT_SIGN_SECRET": "event-secret"}


class ChannelConfigurationTests(unittest.TestCase):
    def test_legacy_only_mode_reuses_old_robot_and_secret(self):
        env = {"FEISHU_WEBHOOK_URL": LEGACY, "FEISHU_SIGN_SECRET": "old-secret"}
        self.assertEqual(channels.routing_mode(env), "legacy")
        for key in channels.CHANNELS:
            self.assertEqual(channels.credentials(key, env),
                             {"webhook": LEGACY, "secret": "old-secret", "source": "legacy"})

    def test_any_new_key_even_empty_disables_legacy_fallback(self):
        for key in (key for pair in channels.KEYS.values() for key in pair):
            with self.subTest(key=key):
                env = {"FEISHU_WEBHOOK_URL": LEGACY, "FEISHU_SIGN_SECRET": "old-secret", key: ""}
                self.assertEqual(channels.routing_mode(env), "split")
                for channel in channels.CHANNELS:
                    self.assertEqual(channels.credentials(channel, env),
                                     {"webhook": "", "secret": "", "source": "none"})

    def test_dedicated_robot_never_inherits_legacy_signature(self):
        env = {"FEISHU_WEBHOOK_URL": LEGACY, "FEISHU_SIGN_SECRET": "old-secret",
               "FEISHU_EVENT_WEBHOOK_URL": EVENT}
        self.assertEqual(channels.credentials("event", env)["secret"], "")
        self.assertEqual(channels.credentials("scheduled", env)["webhook"], "")

    def test_only_hourly_category_routes_to_scheduled_including_old_records(self):
        for item in ({"category": "relay_health"}, {"category": "new_listing"},
                     {"category": "listing_capacity"}, {"category": "trade_summary"},
                     {"category": "strategy_capacity"}, {"id": "old-trade"},
                     {"capacity_key": "capacity_alert:XAUUSD1:5"}):
            with self.subTest(item=item):
                self.assertEqual(channels.for_item({"symbols": "[]", **item}), "event")
        self.assertEqual(channels.for_item({"category": "hourly_summary", "symbols": "[]"}), "scheduled")

    def test_bad_webhooks_are_generic_errors_and_do_not_leak_credentials(self):
        for url in ("http://open.feishu.cn/open-apis/bot/v2/hook/private-token",
                    PREFIX + "private-token?query=secret", PREFIX + "private-token\n",
                    "https://open.feishu.cn@evil.invalid/open-apis/bot/v2/hook/private-token"):
            with self.subTest(url=url):
                state = channels.public_status(environ={**SPLIT, "FEISHU_EVENT_WEBHOOK_URL": url})
                self.assertFalse(state["event"]["configured"])
                self.assertTrue(state["scheduled"]["configured"])
                self.assertEqual(state["event"]["error"], "飞书配置无效")
                self.assertNotIn("private-token", json.dumps(state))
                self.assertNotIn("secret", json.dumps(state))

    def test_secret_controls_and_length_are_rejected(self):
        for secret in ("x\ny", "x\x00y", "x\x7fy", "x\x85y", "x\u2028y", "x\u2029y", "x" * 257, None):
            with self.subTest(secret=secret):
                with self.assertRaises(monitor.MonitorError):
                    monitor.validate_feishu_secret(secret)


class ChannelDeliveryTests(unittest.TestCase):
    setUp = hourly_tests.HourlyNotificationEngineTests.setUp
    at_hour = hourly_tests.HourlyNotificationEngineTests.at_hour
    queue_trade = hourly_tests.HourlyNotificationEngineTests.queue_trade

    def split(self, **overrides):
        env = patch.dict(os.environ, {**SPLIT, **overrides})
        env.start()
        self.addCleanup(env.stop)

    def summary_and_trade(self):
        self.at_hour()
        self.queue_trade()

    def test_correct_url_and_signature_override_legacy_for_both_channels(self):
        self.split()
        self.summary_and_trade()
        with patch("monitor.request_json", return_value={"code": 0}) as request:
            # Use the real payload builder with a mock HTTP boundary.
            self.sender.side_effect = ORIGINAL_SEND
            self.engine.notify()
        self.assertEqual(request.call_count, 2)
        for call, url, secret in zip(request.call_args_list, (EVENT, SCHEDULED), ("event-secret", "scheduled-secret")):
            self.assertEqual(call.args[0], url)
            payload = call.args[1]
            self.assertEqual(payload["sign"], monitor.feishu_payload("", secret, self.now)["sign"])

    def test_scheduled_only_sends_hourly_and_leaves_events_pending(self):
        self.split(FEISHU_EVENT_WEBHOOK_URL="")
        self.summary_and_trade()
        self.engine.notify()
        self.sender.assert_called_once()
        self.assertEqual(self.sender.call_args.args[0]["webhook"], SCHEDULED)
        self.assertEqual(self.f.store.pending_notification_channels(), {"scheduled": 0, "event": 1})

    def test_event_only_never_generates_hourly(self):
        self.split(FEISHU_SCHEDULED_WEBHOOK_URL="")
        self.summary_and_trade()
        with patch.object(self.engine, "state") as state:
            self.engine.notify()
        state.assert_not_called()
        self.sender.assert_called_once()
        self.assertEqual(self.sender.call_args.args[0]["webhook"], EVENT)
        self.assertIsNone(self.f.store.hourly_summary_status(available=False)["next_due_at"])

    def test_invalid_channel_configuration_does_not_block_other(self):
        self.split(FEISHU_EVENT_WEBHOOK_URL="https://invalid.example/private")
        self.summary_and_trade()
        self.engine.notify()
        self.sender.assert_called_once()
        self.assertEqual(self.sender.call_args.args[0]["webhook"], SCHEDULED)
        self.assertEqual(self.engine.notification_channel_errors["event"], "飞书配置无效")
        self.assertIsNone(self.engine.notification_channel_errors["scheduled"])

    def test_invalid_scheduled_configuration_does_not_block_events(self):
        self.split(FEISHU_SCHEDULED_SIGN_SECRET="private\nsecret")
        self.summary_and_trade()
        self.engine.notify()
        self.sender.assert_called_once()
        self.assertEqual(self.sender.call_args.args[0]["webhook"], EVENT)
        self.assertEqual(self.engine.notification_channel_errors["scheduled"], "飞书配置无效")

    def test_failed_event_retries_only_event_without_clearing_its_error_on_summary_success(self):
        self.split()
        self.summary_and_trade()
        def deliver(config, _):
            if config["webhook"] == EVENT:
                raise monitor.MonitorError("private-token")
        self.sender.side_effect = deliver
        self.engine.notify()
        self.assertEqual(self.sender.call_count, 2)
        self.assertEqual(self.engine.notification_channel_errors["event"], "飞书发送失败，等待重试")
        self.assertIsNone(self.engine.notification_channel_errors["scheduled"])
        self.sender.reset_mock(side_effect=True)
        self.now = 3605
        self.engine.notify()
        self.sender.assert_not_called()
        self.now = 3610
        self.engine.notify()
        self.sender.assert_called_once()
        self.assertEqual(self.sender.call_args.args[0]["webhook"], EVENT)
        self.assertIsNone(self.engine.notification_channel_errors["event"])

    def test_event_backlog_does_not_starve_hourly_summary(self):
        self.split()
        self.at_hour()
        with self.f.store.connect() as db:
            db.executemany("INSERT INTO outbox(id,message,due_at) VALUES (?, ?, 0)",
                           [(f"trade-{i}", "old record") for i in range(20)])
        self.engine.notify()
        destinations = [call.args[0]["webhook"] for call in self.sender.call_args_list]
        self.assertEqual(destinations, [EVENT, SCHEDULED, EVENT, EVENT, EVENT, EVENT])

    def test_disabled_event_backlog_does_not_hide_hourly_summary(self):
        self.split(FEISHU_EVENT_WEBHOOK_URL="")
        self.at_hour()
        with self.f.store.connect() as db:
            db.executemany("INSERT INTO outbox(id,message,due_at) VALUES (?, ?, 0)",
                           [(f"trade-{i}", "old record") for i in range(20)])
        self.engine.notify()
        self.sender.assert_called_once()
        self.assertEqual(self.sender.call_args.args[0]["webhook"], SCHEDULED)

    def test_public_snapshot_exposes_only_per_channel_status(self):
        self.split(FEISHU_EVENT_WEBHOOK_URL="")
        self.queue_trade()
        self.engine.notify()
        state = self.engine.state(compact=True)["notification"]
        self.assertEqual(state["routing_mode"], "split")
        self.assertTrue(state["channels"]["scheduled"]["configured"])
        self.assertFalse(state["channels"]["event"]["configured"])
        self.assertEqual(state["channels"]["event"]["pending"], 1)
        self.assertIsNotNone(state["hourly_summary"]["next_due_at"])
        for private in (PREFIX, "event-secret", "scheduled-secret"):
            self.assertNotIn(private, json.dumps(state))

    def test_channel_cleared_during_other_send_cannot_receive_queued_summary(self):
        self.split()
        self.summary_and_trade()
        self.sender.side_effect = lambda *_: os.environ.update(FEISHU_SCHEDULED_WEBHOOK_URL="")
        self.engine.notify()
        self.sender.assert_called_once()
        self.assertEqual(self.sender.call_args.args[0]["webhook"], EVENT)


ORIGINAL_SEND = monitor.send_feishu


if __name__ == "__main__":
    unittest.main()
