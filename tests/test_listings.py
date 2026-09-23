import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import httpx
import monitor
from trading.engine import Engine
from trading.exchange import API, MarketData
from trading.listings import ListingMonitor, maximum_leverage, parse_symbols
from trading.models import SYMBOLS, TradingError
from trading.store import Store


def symbol(name="BTCUSD1", **fields):
    return {"symbol": name, "quoteAsset": "USD1", "contractType": "PERPETUAL", "status": "TRADING",
            "onboardDate": 1790097300000, **fields}


def brackets(name="BTCUSD1", maximum=200, cap="100"):
    return {"success": True, "code": "000000", "data": {"brackets": [{"symbol": name, "riskBrackets": [
        {"minOpenPosLeverage": 1, "maxOpenPosLeverage": maximum, "bracketNotionalCap": cap}]}]}}


def oi(name="BTCUSD1", remaining="0", leverage="200"):
    return {"success": True, "code": "000000", "data": {"symbol": name, "leverageOiRemainingMap": {leverage: remaining}}}


def detail(capacity="0", **fields):
    return {"max_leverage": 200, "bracket_cap": "100", "capacity": capacity, "remaining": capacity,
            "checked_at": time.time(), "brackets_checked_at": time.time(), "error": None, **fields}


class ListingParsingTests(unittest.TestCase):
    def test_only_usd1_perpetuals_and_no_suffix_guess(self):
        payload = {"symbols": [symbol(), symbol("USD1USDT", quoteAsset="USDT"),
                               symbol("FAKEUSD1", quoteAsset="USDT"), symbol("BTCUSD1_2612", contractType="CURRENT_QUARTER")]}
        self.assertEqual(list(parse_symbols(payload)), ["BTCUSD1"])

    def test_live_catalog_empty_contract_type_on_unrelated_prelistings_is_ignored(self):
        payload = {"symbols": [symbol(), symbol("MBLUSDT", quoteAsset="USDT", status="PENDING_TRADING", contractType=""),
                               symbol("FUTUREUSD1", status="PENDING_TRADING", contractType="")]}
        self.assertEqual(list(parse_symbols(payload)), ["BTCUSD1"])

    def test_unicode_symbols_are_valid_and_pass_through_without_case_conversion(self):
        payload = {"symbols": [symbol(), symbol("币安人生USDT", quoteAsset="USDT"), symbol("龙虾USD1")]}
        self.assertEqual(set(parse_symbols(payload)), {"BTCUSD1", "龙虾USD1"})

    def test_reject_invalid_or_empty_catalog(self):
        for value in ({}, {"symbols": []}, {"symbols": [None]}, {"symbols": [symbol(), symbol()]},
                      {"symbols": [symbol(quoteAsset=None)]}, {"symbols": [symbol("BTCUSDT", quoteAsset="USDT")]}):
            with self.subTest(value=value), self.assertRaises(TradingError):
                parse_symbols(value)

    def test_exact_maximum_can_exceed_existing_strategy_tiers(self):
        self.assertEqual(maximum_leverage(brackets(maximum=200), "BTCUSD1")[0], 200)
        for value in (brackets("ETHUSD1"), brackets(maximum=0), brackets(maximum=2.5), brackets(maximum=10001)):
            with self.subTest(value=value), self.assertRaises(monitor.MonitorError):
                maximum_leverage(value, "BTCUSD1")
        ambiguous = brackets()
        ambiguous["data"]["brackets"][0]["riskBrackets"] *= 2
        with self.assertRaises(monitor.MonitorError):
            maximum_leverage(ambiguous, "BTCUSD1")

    def test_live_adapter_uses_unsigned_exact_tier_and_caps_remaining(self):
        for remaining, expected in (("0", "0"), ("150", "100"), ("2.123456789", "2.123456789")):
            seen = []
            def handler(request):
                seen.append(request)
                return httpx.Response(200, json=brackets() if request.method == "POST" else oi(remaining=remaining))
            api = API(transport=httpx.MockTransport(handler))
            self.addCleanup(api.close)
            result = MarketData(api=api).listing_detail("BTCUSD1")
            self.assertEqual(result["capacity"], expected)
            self.assertEqual(result["max_leverage"], 200)
            self.assertEqual(json.loads(seen[0].content), {"symbol": "BTCUSD1"})
            self.assertEqual(dict(seen[1].url.params), {"symbol": "BTCUSD1"})
            self.assertTrue(all(request.url.host == "www.asterdex.com" for request in seen))
            self.assertTrue(all("signature" not in str(request.url) for request in seen))

    def test_missing_exact_tier_keeps_maximum_but_never_substitutes_zero_or_lower_tier(self):
        for payload in (oi(leverage="100"), oi("ETHUSD1"), oi(remaining="NaN"), oi(remaining="-1")):
            api = API(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=brackets() if request.method == "POST" else payload)))
            self.addCleanup(api.close)
            result = MarketData(api=api).listing_detail("BTCUSD1")
            self.assertEqual(result["max_leverage"], 200)
            self.assertEqual(result["bracket_cap"], "100")
            self.assertIsNone(result["capacity"])
            self.assertIsNone(result["checked_at"])
            self.assertTrue(result["error"])

    def test_public_rate_limit_sets_shared_backoff(self):
        api = API(transport=httpx.MockTransport(lambda _: httpx.Response(429, headers={"Retry-After": "240"})))
        self.addCleanup(api.close)
        with self.assertRaises(TradingError):
            MarketData(api=api).listing_detail("BTCUSD1")
        self.assertIn("公共额度接口", api.budget.cooldown_message())
        with self.assertRaises(TradingError):
            api.budget.reserve(1)


class ListingMonitorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "state.sqlite3")
        self.market = Mock()
        self.market.listing_symbols.return_value = {"symbols": [symbol()]}
        self.market.listing_detail.return_value = detail()
        self.shutdown = threading.Event()
        self.monitor = ListingMonitor(self.store, self.market, self.shutdown)

    def poll(self, rows=None):
        if rows is not None:
            self.market.listing_symbols.return_value = {"symbols": rows}
            self.monitor.catalog_due = 0
        self.monitor.detail_due.clear()
        self.monitor.poll()
        return self.store.get("usd1_listings")

    def test_baseline_new_listing_restart_and_temporary_absence(self):
        state = self.poll()
        self.assertTrue(state["initialized"])
        self.assertEqual(self.store.pending_notifications(), 0)
        state = self.poll([symbol(), symbol("NEWUSD1")])
        self.assertTrue(state["rows"]["NEWUSD1"]["is_new"])
        self.assertEqual(self.store.pending_notifications(), 1)
        message = self.store.due_notifications()[0]["message"]
        self.assertIn("最大杠杆：200x", message)
        self.assertIn("公开可用额度：0 USD1", message)
        self.assertIn("额度采样时间：", message)
        self.monitor = ListingMonitor(self.store, self.market, self.shutdown)
        self.poll()
        self.poll([symbol()])
        self.poll([symbol(), symbol("NEWUSD1")])
        self.assertEqual(self.store.pending_notifications(), 1)

    def test_prelisting_is_not_notified_until_trading(self):
        self.poll([symbol(), symbol("NEWUSD1", status="PENDING_TRADING")])
        self.assertEqual(self.store.pending_notifications(), 0)
        self.poll([symbol(), symbol("NEWUSD1")])
        self.assertEqual(self.store.pending_notifications(), 1)

    def test_new_symbols_while_service_was_stopped_are_discovered(self):
        self.poll()
        self.monitor = ListingMonitor(Store(self.store.path), self.market, self.shutdown)
        self.poll([symbol(), symbol("NEWUSD1")])
        self.assertEqual(self.store.pending_notifications(), 1)

    def test_empty_or_failed_catalog_preserves_baseline_and_source_timestamp(self):
        before = self.poll()
        after = self.poll([])
        self.assertEqual(after["checked_at"], before["checked_at"])
        self.assertEqual(set(after["rows"]), set(before["rows"]))
        self.assertTrue(after["error"])
        self.market.listing_symbols.side_effect = RuntimeError("untrusted remote details")
        self.monitor.catalog_due = 0
        self.monitor.poll()
        self.assertNotIn("untrusted", json.dumps(self.store.get("usd1_listings")))

    def test_detail_failure_notifies_discovery_then_supplements_once(self):
        self.poll()
        self.market.listing_detail.return_value = detail(None, remaining=None, checked_at=None, error="精确档位缺失")
        self.poll([symbol(), symbol("NEWUSD1")])
        first = self.store.due_notifications()[0]
        self.assertIn("最大杠杆：200x", first["message"])
        self.assertIn("公开可用额度：暂不可用", first["message"])
        self.assertEqual(self.store.pending_notifications(), 1)
        self.monitor = ListingMonitor(self.store, self.market, self.shutdown)
        self.market.listing_detail.return_value = detail("10")
        self.monitor.poll()
        self.monitor.poll()
        self.assertEqual(self.store.pending_notifications(), 2)
        self.assertIn("额度补全", self.store.due_notifications()[1]["message"])
        self.poll([symbol(), symbol("NEWUSD1")])
        self.assertEqual(self.store.pending_notifications(), 2)

    def test_bad_symbol_does_not_block_other_symbols_or_renew_failed_sample(self):
        before = self.poll()
        self.market.listing_detail.side_effect = lambda name: (_ for _ in ()).throw(TradingError("failed")) if name == "BTCUSD1" else detail("9")
        self.poll([symbol(), symbol("NEWUSD1")])
        self.monitor.detail_due.clear()
        self.monitor.poll()
        after = self.store.get("usd1_listings")
        self.assertEqual(after["rows"]["BTCUSD1"]["checked_at"], before["rows"]["BTCUSD1"]["checked_at"])
        self.assertEqual(after["rows"]["NEWUSD1"]["capacity"], "9")
        self.assertTrue(after["rows"]["BTCUSD1"]["error"])

    def test_catalog_and_details_are_throttled(self):
        self.monitor.poll()
        for _ in range(10):
            self.monitor.poll()
        self.assertEqual(self.market.listing_symbols.call_count, 1)
        self.assertEqual(self.market.listing_detail.call_count, 1)

    def test_alert_and_state_rollback_together(self):
        before = self.poll()
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER fail_listing BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT, 'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save_listing_state({"broken": True}, [("usd1-listing:NEWUSD1", "message")])
        self.assertEqual(self.store.get("usd1_listings"), before)
        self.assertEqual(self.store.pending_notifications(), 0)

    def test_existing_feishu_outbox_retries_without_duplicate_discovery(self):
        self.poll()
        self.poll([symbol(), symbol("NEWUSD1")])
        engine = Engine(self.store, market=Mock())
        with patch.object(engine, "notification_config", return_value={"webhook": "https://example.invalid", "cooldown_seconds": 0}), \
             patch("monitor.send_feishu", side_effect=monitor.MonitorError("failed")) as send:
            engine.notify()
            self.assertEqual(send.call_count, 1)
        item = self.store.get("usd1_listings")
        self.assertEqual(item["rows"]["NEWUSD1"]["notification_phase"], "queued")
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM outbox").fetchone()
            self.assertEqual(row["attempts"], 1)
            self.assertIsNone(row["delivered_at"])
            db.execute("UPDATE outbox SET due_at=0")
        with patch.object(engine, "notification_config", return_value={"webhook": "https://example.invalid", "cooldown_seconds": 0}), patch("monitor.send_feishu") as send:
            engine.notify()
            self.assertEqual(send.call_count, 1)
        self.assertEqual(self.store.pending_notifications(), 0)
        self.poll([symbol(), symbol("NEWUSD1")])
        self.assertEqual(self.store.pending_notifications(), 0)

    def test_no_account_state_and_demo_isolation(self):
        self.poll()
        api = API(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
        self.addCleanup(api.close)
        engine = Engine(self.store, market=MarketData(api=api))
        state = engine.state(compact=True)
        self.assertEqual(state["accounts"], [])
        self.assertTrue(state["listings"]["enabled"])
        self.assertEqual(state["listings"]["rows"]["BTCUSD1"]["capacity"], "0")
        demo = Engine(Store(Path(self.directory.name) / "demo.sqlite3", demo=True), demo=True)
        self.assertIsNone(demo.listing_monitor)
        self.assertFalse(demo.state()["listings"]["enabled"])
        with patch("monitor.send_feishu") as send:
            demo.notify()
            send.assert_not_called()

    def test_scheduler_polls_listings_with_no_accounts(self):
        api = API(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
        market = MarketData(api=api, stream=Mock(), depth_stream=Mock())
        engine = Engine(self.store, market=market)
        market.rules = {s: None for s in SYMBOLS}
        called = threading.Event()
        def listings():
            called.set()
            engine.shutdown.set()
            return 60
        with patch.object(market, "load_rules"), patch.object(engine, "poll_market", return_value=60), \
             patch.object(engine, "poll_public_brackets", return_value=60), patch.object(engine, "poll_book", return_value=60), \
             patch.object(engine, "poll_depth", return_value=60), patch.object(engine, "notify", return_value=60), \
             patch.object(engine.listing_monitor, "poll", side_effect=listings):
            worker = threading.Thread(target=engine.run, daemon=True)
            worker.start()
            try:
                self.assertTrue(called.wait(5))
            finally:
                engine.shutdown.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
