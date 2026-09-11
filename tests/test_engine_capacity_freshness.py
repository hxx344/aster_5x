from dataclasses import replace
import os
import unittest
from unittest.mock import patch

from trading.engine import Engine
from trading.models import TradingError, dec
from .helpers import Fixture


class CapacityFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.engine = Engine(self.f.store, market=self.f.market)
        self.now = 1000.0
        self.book = self.f.market.book("XAUUSD1")

    def test_capacity_request_latency_cannot_renew_cache_or_alert_freshness(self):
        def slow_capacity(*args):
            self.now += 9
            return {5: dec(20000)}

        with patch("trading.engine.time.time", side_effect=lambda: self.now), \
             patch.dict(os.environ, {"FEISHU_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/hook/test"}, clear=True), \
             patch.object(self.f.market, "capacities", side_effect=slow_capacity), \
             patch.object(self.f.market, "book", side_effect=lambda symbol: replace(self.book, timestamp=self.now)):
            self.engine.poll_market("XAUUSD1")
            with self.assertRaisesRegex(TradingError, "额度快照"):
                self.engine.capacities("XAUUSD1")
        self.assertEqual(self.engine.markets["XAUUSD1"]["checked_at"], 1000)
        self.assertEqual(self.f.store.pending_notifications(), 0)

    def test_slow_book_read_does_not_extend_capacity_lifetime(self):
        def slow_book(symbol):
            self.now += 7
            return replace(self.book, timestamp=self.now)

        with patch("trading.engine.time.time", side_effect=lambda: self.now), \
             patch.object(self.f.market, "capacities", return_value={5: dec(20000)}), \
             patch.object(self.f.market, "book", side_effect=slow_book):
            self.engine.poll_market("XAUUSD1")
            self.engine.poll_book("XAUUSD1")
            self.assertEqual(self.engine.capacities("XAUUSD1"), {5: dec(20000)})
            self.now += 2
            with self.assertRaisesRegex(TradingError, "额度快照"):
                self.engine.capacities("XAUUSD1")

    def test_clock_rollback_cannot_keep_a_future_capacity_cache_usable(self):
        with patch("trading.engine.time.time", side_effect=lambda: self.now):
            self.engine.poll_market("XAUUSD1")
            self.now -= 2
            with self.assertRaisesRegex(TradingError, "额度快照"):
                self.engine.capacities("XAUUSD1")
