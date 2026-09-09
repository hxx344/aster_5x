import os
import time
import unittest
from unittest.mock import patch

import monitor
from trading.engine import Engine
from trading.models import TradingError, dec
from trading.store import Store
from .helpers import Fixture


WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/test-capacity-notification"


class CapacityAlertEngineTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.env = patch.dict(os.environ, {"FEISHU_WEBHOOK_URL": WEBHOOK}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        sender_patch = patch("trading.engine.monitor.send_feishu")
        self.sender = sender_patch.start()
        self.addCleanup(sender_patch.stop)
        self.engine = Engine(self.f.store, market=self.f.market)

    def publish(self, capacities=None, symbol="XAUUSD1"):
        values = {4: dec(20000)} if capacities is None else capacities
        with patch.object(self.f.market, "capacities", return_value=values):
            self.engine.poll_market(symbol)

    def trade_notification(self):
        with self.f.store.connect() as db:
            db.execute("INSERT INTO outbox(id,message,due_at) VALUES (?,?,?)", ("trade", "Aster 双向开仓完成", time.time()))

    def test_existing_market_poll_is_reused_without_any_extra_aster_requests(self):
        values = {tier: dec(20000) for tier in (1, 2, 3, 4, 5, 10, 20)}
        with patch.object(self.f.market, "capacities", return_value=values) as capacities, \
             patch.object(self.f.market, "book", wraps=self.f.market.book) as book:
            self.engine.poll_market("XAUUSD1")
        capacities.assert_called_once()
        book.assert_called_once_with("XAUUSD1")
        self.assertEqual(self.f.store.pending_notifications(), 4)
        self.engine.notify()
        self.engine.notify()
        messages = [call.args[1] for call in self.sender.call_args_list]
        self.assertEqual(len(messages), 4)
        for tier in (4, 5, 10, 20):
            self.assertTrue(any(f" · {tier}x" in message for message in messages))
        self.assertTrue(all("公开" in m and "检查时间" in m for m in messages))

    def test_alerts_do_not_require_accounts_or_live_trading(self):
        with self.f.store.connect() as db:
            db.execute("DELETE FROM accounts")
        self.publish()
        self.engine.notify()
        self.sender.assert_called_once()
        self.assertNotIn("ASTER_ALLOW_LIVE", os.environ)

    def test_all_twelve_combinations_are_independent_and_high_values_do_not_repeat(self):
        for symbol in ("XAUUSD1", "SPCXUSD1", "CLUSD1"):
            self.publish({t: dec(20000) for t in (4, 5, 10, 20)}, symbol)
        self.assertEqual(self.f.store.pending_notifications(), 12)
        for _ in range(6):
            self.engine.notify()
        self.assertEqual(self.sender.call_count, 12)
        restored = Engine(Store(self.f.store.path), market=self.f.market)
        for symbol in ("XAUUSD1", "SPCXUSD1", "CLUSD1"):
            with patch.object(self.f.market, "capacities", return_value={t: dec(30000) for t in (4, 5, 10, 20)}):
                restored.poll_market(symbol)
        restored.notify()
        self.assertEqual(self.sender.call_count, 12)

    def test_no_webhook_does_not_accumulate_history_and_new_sample_enables_alert(self):
        with patch.dict(os.environ, {}, clear=True):
            self.publish()
            self.assertEqual(self.f.store.pending_notifications(), 0)
            self.engine.notify()
        self.sender.assert_not_called()
        self.engine.notify()
        self.sender.assert_not_called()
        self.publish()
        self.engine.notify()
        self.sender.assert_called_once()

    def test_demo_never_queues_or_sends_even_with_webhook(self):
        demo = Engine(self.f.store, demo=True, market=self.f.market)
        demo.poll_market("XAUUSD1")
        demo.notify()
        self.assertEqual(self.f.store.pending_notifications(), 0)
        self.sender.assert_not_called()

    def test_book_failure_does_not_hide_successful_capacity_alert(self):
        with patch.object(self.f.market, "capacities", return_value={4: dec(20000)}), \
             patch.object(self.f.market, "book", side_effect=TradingError("BBO unavailable")):
            self.engine.poll_market("XAUUSD1")
        self.assertEqual(self.engine.markets["XAUUSD1"]["status"], "error")
        self.engine.notify()
        self.sender.assert_called_once()

    def test_capacity_failure_suspends_queued_alert_until_a_new_valid_sample(self):
        self.publish()
        with patch.object(self.f.market, "capacities", side_effect=TradingError("capacity unavailable")):
            self.engine.poll_market("XAUUSD1")
        self.engine.notify()
        self.sender.assert_not_called()
        self.publish()
        self.engine.notify()
        self.sender.assert_called_once()

    def test_missing_tier_does_not_send_its_older_queued_value(self):
        self.publish({4: dec(20000), 5: dec(20000)})
        self.publish({4: dec(21000)})
        self.engine.notify()
        self.sender.assert_called_once()
        self.assertIn(" · 4x", self.sender.call_args.args[1])

    def test_alert_setting_failure_does_not_block_market_or_trade_completion(self):
        self.trade_notification()
        with patch.dict(os.environ, {"ASTER_CAPACITY_ALERT_THRESHOLD": "NaN"}):
            self.publish()
            self.assertEqual(self.engine.markets["XAUUSD1"]["status"], "ok")
            self.engine.notify()
        self.sender.assert_called_once()
        self.assertEqual(self.sender.call_args.args[1], "Aster 双向开仓完成")
        self.assertIn("额度提醒", self.engine.state()["notification"]["error"])

    def test_disabled_capacity_alert_does_not_disable_trade_summary(self):
        self.publish()
        self.trade_notification()
        with patch.dict(os.environ, {"ASTER_CAPACITY_ALERT_ENABLED": "0"}):
            self.engine.notify()
        self.sender.assert_called_once()
        self.assertEqual(self.sender.call_args.args[1], "Aster 双向开仓完成")

    def test_delivery_refreshes_after_earlier_message_and_skips_now_low_capacity(self):
        self.publish()
        self.trade_notification()
        def send(config, message):
            self.assertEqual(message, "Aster 双向开仓完成")
            self.publish({4: dec(10000)})
        self.sender.side_effect = send
        self.engine.notify()
        self.sender.assert_called_once()

    def test_retry_uses_new_value_and_expired_restored_candidate_does_not_send(self):
        stamp = time.time()
        with patch("trading.engine.time.time", return_value=stamp):
            self.publish()
            self.sender.side_effect = monitor.MonitorError("test delivery failed")
            self.engine.notify()
        self.assertEqual(self.sender.call_count, 1)
        self.sender.side_effect = None
        restored = Engine(Store(self.f.store.path), market=self.f.market)
        with patch("trading.engine.time.time", return_value=stamp + 20):
            restored.notify()
            self.assertEqual(self.sender.call_count, 1)
            with patch.object(self.f.market, "capacities", return_value={4: dec(31000)}):
                restored.poll_market("XAUUSD1")
            restored.notify()
        self.assertEqual(self.sender.call_count, 2)
        self.assertIn("31,000.00", self.sender.call_args.args[1])

    def test_webhook_change_requires_a_new_matching_observation(self):
        self.publish()
        with patch.dict(os.environ, {"FEISHU_WEBHOOK_URL": WEBHOOK + "-new"}):
            self.engine.notify()
            self.sender.assert_not_called()
            self.publish()
            self.engine.notify()
        self.sender.assert_called_once()

    def test_settings_apply_only_to_public_alerts_and_do_not_leak_webhook(self):
        with patch.dict(os.environ, {"ASTER_CAPACITY_ALERT_THRESHOLD": "25000", "ASTER_CAPACITY_ALERT_COOLDOWN_SECONDS": "600"}):
            self.publish({4: dec(25000)})
            self.assertEqual(self.f.store.pending_notifications(), 0)
            self.publish({4: dec(25001)})
            self.engine.notify()
            self.assertIn("25,000.00", self.sender.call_args.args[1])
            self.assertEqual(self.f.store.account("test")["policy"]["threshold"], "10000")
            self.assertNotIn(WEBHOOK, str(self.engine.state()))


if __name__ == "__main__":
    unittest.main()
