"""Notification throttling must not stop otherwise healthy public market reads."""
import threading
import unittest
from unittest.mock import patch

import monitor as m


class NotificationBackoffTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "symbols": ["XAUUSD1", "SPCXUSD1"], "leverages": [5, 10],
            "threshold": m.number(10000), "poll_seconds": 5, "timeout_seconds": 10,
            "cooldown_seconds": 300, "feishu_enabled": True,
            "webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/test-placeholder", "secret": "",
        }
        self.now = 100.0
        self.clock = patch("monitor.time.monotonic", side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    @staticmethod
    def reading(value):
        return {"value": str(value), "global_remaining": str(value),
                "bracket_cap": "5000000", "checked_at": m.now_iso()}

    def tracker(self, symbol="XAUUSD1"):
        return m.SymbolMonitor(self.config, symbol, {}, threading.Event())

    def test_feishu_limit_preserves_tier_retry_without_global_market_backoff(self):
        for retry in (180, 86400):
            with self.subTest(retry=retry):
                tracker = self.tracker()
                with patch("monitor.sample", return_value={5: self.reading(15000), 10: self.reading(0)}), \
                     patch("monitor.send_feishu", side_effect=m.MonitorError("HTTP 429", retry)) as send:
                    markets, records, delay, global_retry = tracker.check()
                self.assertEqual(global_retry, 0)
                self.assertEqual(delay, 5)
                self.assertEqual(markets["XAUUSD1:5"]["retry_seconds"], retry)
                self.assertEqual(tracker.next_due["XAUUSD1:5"], self.now + retry)
                self.assertFalse(records["XAUUSD1:5"]["gate"]["notified"])
                self.assertEqual(markets["XAUUSD1:10"]["status"], "ok")
                send.assert_called_once()

    def test_other_market_and_tier_continue_while_throttled_delivery_waits(self):
        tracker, other = self.tracker(), self.tracker("SPCXUSD1")
        with patch("monitor.sample", return_value={5: self.reading(15000), 10: self.reading(0)}) as sample, \
             patch("monitor.send_feishu", side_effect=[m.MonitorError("HTTP 429", 180), None]) as send:
            self.assertEqual(tracker.check()[3], 0)
            self.now += 5
            markets, _, _, global_retry = tracker.check()
            self.assertEqual(set(markets), {"XAUUSD1:10"})
            self.assertEqual(global_retry, 0)
            self.assertEqual(other.check()[0]["SPCXUSD1:5"]["status"], "ok")
        self.assertEqual(sample.call_count, 3)
        self.assertEqual(send.call_count, 2)

    def test_aster_limit_still_requests_global_backoff(self):
        tracker = self.tracker()
        with patch("monitor.sample", side_effect=m.MonitorError("HTTP 429", 180)), \
             patch("monitor.send_feishu") as send:
            markets, _, delay, global_retry = tracker.check()
        self.assertEqual(global_retry, 180)
        self.assertEqual(delay, 180)
        self.assertTrue(all(row["status"] == "error" for row in markets.values()))
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
