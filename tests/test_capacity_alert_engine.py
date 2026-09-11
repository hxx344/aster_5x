import os
import time
import unittest
from unittest.mock import patch

import monitor
from trading.engine import Engine
from trading.models import SYMBOLS, TradingError, dec
from trading.store import Store
from .helpers import Fixture, account


WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/test-capacity-notification"


class CapacityAlertEngineTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.f.account['policy']['symbols'] = list(SYMBOLS)
        self.f.store.save_account(self.f.account)
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

    def test_no_accounts_means_no_web_threshold_and_no_capacity_alert(self):
        with self.f.store.connect() as db:
            db.execute("DELETE FROM accounts")
        self.publish()
        self.engine.notify()
        self.sender.assert_not_called()
        self.assertEqual(self.f.store.pending_notifications(), 0)
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
        with patch.dict(os.environ, {"ASTER_CAPACITY_ALERT_COOLDOWN_SECONDS": "NaN"}):
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

    def test_alerts_use_web_threshold_and_ignore_legacy_environment_override(self):
        self.configure_threshold('25000')
        with patch.dict(os.environ, {"ASTER_CAPACITY_ALERT_THRESHOLD": "1", "ASTER_CAPACITY_ALERT_COOLDOWN_SECONDS": "600"}):
            self.publish({4: dec(25000)})
            self.assertEqual(self.f.store.pending_notifications(), 0)
            self.publish({4: dec(25001)})
            self.engine.notify()
            self.assertIn("25,000.00", self.sender.call_args.args[1])
            self.assertEqual(self.f.store.account("test")["policy"]["threshold"], "25000")
            self.assertNotIn(WEBHOOK, str(self.engine.state()))

    def configure_threshold(self, threshold):
        saved = self.f.store.account('test')
        saved['enabled'] = False
        self.f.store.save_account(saved)
        self.engine.configure('test', {'threshold': threshold})

    def test_web_one_hundred_thousand_is_a_strict_boundary_even_when_paused(self):
        self.configure_threshold('100000')
        with patch.dict(os.environ, {'ASTER_CAPACITY_ALERT_THRESHOLD': 'NaN'}):
            for value in (20000, 99999, 100000):
                self.publish({4: dec(value)})
                self.engine.notify()
            self.sender.assert_not_called()
            self.publish({4: dec('100000.01')})
            self.engine.notify()
        self.sender.assert_called_once()
        message = self.sender.call_args.args[1]
        self.assertIn('触发条件：> 100,000.00 USD1', message)
        self.assertIn('满足网页额度阈值的账户：测试子账户（test，> 100,000.00 USD1）', message)

    def test_raising_web_threshold_suppresses_already_queued_alert(self):
        self.publish()
        self.assertEqual(self.f.store.pending_notifications(), 1)
        self.configure_threshold('100000')
        self.engine.notify()
        self.sender.assert_not_called()
        self.publish()
        self.engine.notify()
        self.sender.assert_not_called()
        self.publish({4: dec(100001)})
        self.engine.notify()
        self.sender.assert_called_once()

    def test_lowering_web_threshold_takes_effect_on_next_sample_without_restart(self):
        self.configure_threshold('100000')
        self.publish()
        self.engine.notify()
        self.sender.assert_not_called()
        self.configure_threshold('10000')
        self.publish()
        self.engine.notify()
        self.sender.assert_called_once()

    def test_failed_delivery_is_not_retried_under_old_threshold(self):
        self.publish()
        self.sender.side_effect = monitor.MonitorError('simulated failure')
        self.engine.notify()
        self.sender.assert_called_once()
        self.configure_threshold('100000')
        self.sender.side_effect = None
        self.publish()
        self.engine.notify()
        self.assertEqual(self.sender.call_count, 1)
        restored = Engine(Store(self.f.store.path), market=self.f.market)
        restored.notify()
        self.assertEqual(self.sender.call_count, 1)

    def test_multiple_accounts_share_one_alert_and_only_matching_symbols_apply(self):
        self.configure_threshold('100000')
        second = account('second')
        second.update(name='低阈值账户', enabled=False)
        second['policy']['threshold'] = '20000'
        self.f.store.save_account(second)  # XAU only.
        self.publish({4: dec(30000)}, 'CLUSD1')
        self.engine.notify()
        self.sender.assert_not_called()
        self.publish({4: dec(30000)})
        self.engine.notify()
        self.sender.assert_called_once()
        message = self.sender.call_args.args[1]
        self.assertIn('低阈值账户（second，> 20,000.00 USD1）', message)
        self.assertNotIn('测试子账户', message)
        self.assertIn('触发条件：> 20,000.00 USD1', message)

    def test_multiple_qualifying_accounts_are_listed_in_one_message(self):
        second = account('second')
        second['name'] = '第二个账户'
        self.f.store.save_account(second)
        self.publish()
        self.engine.notify()
        self.sender.assert_called_once()
        message = self.sender.call_args.args[1]
        self.assertIn('测试子账户', message)
        self.assertIn('第二个账户', message)

    def test_web_change_during_delivery_blocks_next_old_threshold_message(self):
        self.publish({4: dec(20000), 5: dec(20000)})
        self.sender.side_effect = lambda config, message: self.configure_threshold('100000')
        self.engine.notify()
        self.sender.assert_called_once()

    def test_restart_and_equivalent_threshold_notation_preserve_deduplication(self):
        self.configure_threshold('100000')
        self.publish({4: dec(100001)})
        self.engine.notify()
        self.sender.assert_called_once()
        self.configure_threshold('1e5')
        self.engine = Engine(Store(self.f.store.path), market=self.f.market)
        self.publish({4: dec(100002)})
        self.engine.notify()
        self.sender.assert_called_once()


if __name__ == "__main__":
    unittest.main()
