from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import manage
import monitor as m


def config():
    with patch.dict(os.environ, {}, clear=True):
        return {**m.validate_config(json.loads((m.ROOT / "config.json").read_text())), "leverage": 5}


def reading(value):
    return {"value": str(value), "global_remaining": str(value),
            "bracket_cap": "5000000", "checked_at": m.now_iso()}


class MultiMarketTests(unittest.TestCase):
    def test_invalid_leverages_rejected(self):
        for tiers in ([], [4, 4], [3], [True], [4.0], "4,5", [{}]):
            with self.subTest(tiers=tiers), self.assertRaises(m.MonitorError):
                m.validate_config({**config(), "leverages": tiers})

    def test_all_old_5x_gates_survive_without_suppressing_4x(self):
        current = config()
        saved = {"version": 2, "markets": {s: {
            "identity": m.alert_identity(current, s),
            "gate": {"notified": True, "last_alert": 123}}
            for s in current["symbols"]}}
        for symbol in current["symbols"]:
            self.assertTrue(m.restore_gate(saved, current, symbol).state["notified"])
            self.assertFalse(m.restore_gate(saved, {**current, "leverage": 4}, symbol).state["notified"])

    @patch("monitor.request_json")
    def test_shared_snapshot_uses_exact_tiers_and_isolates_missing_tier(self, request):
        oi = {"success": True, "code": "000000", "data": {
            "symbol": "XAUUSD1", "leverageOiRemainingMap": {"4": "20000", "5": "0"}}}
        brackets = {"success": True, "code": "000000", "data": {"brackets": [{
            "symbol": "XAUUSD1", "riskBrackets": [{"minOpenPosLeverage": 1,
                "maxOpenPosLeverage": 5, "bracketNotionalCap": "5000000"}]}]}}
        request.side_effect = [brackets, oi]
        result = m.sample({**config(), "symbol": "XAUUSD1"})
        self.assertEqual(request.call_count, 2)
        self.assertEqual(result[4]["value"], "20000")
        self.assertEqual(result[5]["value"], "0")
        del oi["data"]["leverageOiRemainingMap"]["4"]
        request.side_effect = [brackets, oi]
        result = m.sample({**config(), "symbol": "XAUUSD1"})
        self.assertIsInstance(result[4], m.MonitorError)
        self.assertEqual(result[5]["value"], "0")

    @patch("monitor.send_feishu")
    def test_six_combinations_alert_reset_and_retry_independently(self, send):
        current = {**config(), "leverages": [4, 5], "feishu_enabled": True, "cooldown_seconds": 0}
        trackers = [m.MarketMonitor({**current, "leverage": v}, s, {}, threading.Event())
                    for s in current["symbols"] for v in current["leverages"]]
        for tracker in trackers:
            tracker.check(reading(15000))
            tracker.check(reading(15000))
        self.assertEqual(send.call_count, 6)
        for tracker, call in zip(trackers, send.call_args_list):
            self.assertIn(f"{tracker.symbol} · {tracker.config['leverage']}x", call.args[1])
        trackers[0].check(reading(10000))
        send.side_effect = m.MonitorError("test failed delivery")
        trackers[0].check(reading(15000))
        self.assertFalse(trackers[0].gate.state["notified"])
        self.assertTrue(trackers[1].gate.state["notified"])
        send.side_effect = None
        for tracker in trackers:
            tracker.check(reading(15000))
        self.assertEqual(send.call_count, 8)

    @patch("monitor.time.monotonic")
    @patch("monitor.sample")
    def test_failed_tier_backs_off_while_other_tier_keeps_updating(self, sample, monotonic):
        tracker = m.SymbolMonitor({**config(), "leverages": [4, 5]}, "XAUUSD1", {}, threading.Event())
        sample.return_value = {4: m.MonitorError("test missing tier"), 5: reading(0)}
        monotonic.return_value = 100
        first = tracker.check()[0]
        self.assertEqual(first["XAUUSD1:4"]["status"], "error")
        self.assertEqual(first["XAUUSD1:5"]["status"], "ok")
        monotonic.return_value = 105
        self.assertEqual(set(tracker.check()[0]), {"XAUUSD1:5"})
        sample.return_value = {4: reading(0), 5: reading(0)}
        monotonic.return_value = 110
        self.assertTrue(all(row["status"] == "ok" for row in tracker.check()[0].values()))

    def test_defaults_and_legacy_config_adopt_all_three(self):
        current = config()
        self.assertEqual(current["symbols"], list(m.SUPPORTED_SYMBOLS))
        legacy = {**current, "symbol": "XAUUSD1", "threshold": "22000"}
        legacy.pop("symbols")
        legacy.pop("leverages")
        upgraded = m.validate_config(legacy)
        self.assertEqual(upgraded["symbols"], list(m.SUPPORTED_SYMBOLS))
        self.assertEqual(upgraded["threshold"], 22000)
        self.assertEqual(upgraded["leverages"], [4, 5, 10, 20])
        self.assertFalse(upgraded["feishu_enabled"])

    def test_invalid_and_duplicate_symbols_rejected(self):
        for symbols in ([], "XAUUSD1", ["XAUUSD1", "XAUUSD1"], ["OTHER"], [None], [{}]):
            with self.subTest(symbols=symbols), self.assertRaises(m.MonitorError):
                m.validate_config({**config(), "symbols": symbols})

    def test_legacy_alert_migrates_only_to_gold(self):
        current = config()
        saved = {"identity": m.alert_identity(current, "XAUUSD1"),
                 "gate": {"notified": True, "last_alert": 1000}}
        self.assertTrue(m.restore_gate(saved, current, "XAUUSD1").state["notified"])
        self.assertFalse(m.restore_gate(saved, current, "SPCXUSD1").state["notified"])
        self.assertFalse(m.restore_gate(saved, current, "CLUSD1").state["notified"])

    @patch("monitor.send_feishu")
    def test_each_market_alerts_independently_with_correct_destination(self, send):
        current = {**config(), "feishu_enabled": True}
        trackers = [m.MarketMonitor(current, symbol, {}, threading.Event()) for symbol in current["symbols"]]
        for tracker in trackers:
            tracker.check(reading(15000))
            tracker.check(reading(15000))
        self.assertEqual(send.call_count, 3)
        for call, symbol in zip(send.call_args_list, current["symbols"]):
            self.assertIn(symbol + " · 5x", call.args[1])
            self.assertTrue(call.args[1].endswith("/" + symbol))

    @patch("monitor.send_feishu")
    def test_failed_delivery_retries_only_affected_market(self, send):
        current = {**config(), "feishu_enabled": True}
        trackers = {symbol: m.MarketMonitor(current, symbol, {}, threading.Event()) for symbol in current["symbols"]}
        send.side_effect = lambda cfg, message: (_ for _ in ()).throw(m.MonitorError("test failure")) if cfg["symbol"] == "SPCXUSD1" else None
        with patch("monitor.sample", return_value=reading(15000)):
            for tracker in trackers.values():
                tracker.check(reading(15000))
            self.assertEqual(trackers["SPCXUSD1"].status["status"], "error")
            send.side_effect = None
            for tracker in trackers.values():
                tracker.check(reading(15000))
        self.assertEqual(send.call_count, 4)
        self.assertTrue(all(t.gate.state["notified"] for t in trackers.values()))

    def test_slow_or_failed_market_does_not_block_another_worker(self):
        waiting = threading.Event()
        release = threading.Event()
        def sample(cfg):
            if cfg["symbol"] == "SPCXUSD1":
                waiting.set()
                release.wait(5)
                raise m.MonitorError("test network failure")
            return {v: reading(10) for v in cfg["leverages"]}
        current = config()
        slow = m.SymbolMonitor(current, "SPCXUSD1", {}, threading.Event())
        fast = m.SymbolMonitor(current, "CLUSD1", {}, threading.Event())
        with patch("monitor.sample", side_effect=sample), ThreadPoolExecutor(2) as pool:
            pending = pool.submit(slow.check)
            try:
                self.assertTrue(waiting.wait(2))
                self.assertEqual(pool.submit(fast.check).result(timeout=2)[0]["CLUSD1:4"]["status"], "ok")
                self.assertFalse(pending.done())
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=2)[0]["SPCXUSD1:4"]["status"], "error")

    @patch("monitor.socket.socket")
    def test_once_persists_per_market_results_and_reports_partial_failure(self, socket):
        current = config()
        def sample(cfg):
            if cfg["symbol"] == "SPCXUSD1":
                raise m.MonitorError("test network failure")
            return {v: reading(0) for v in cfg["leverages"]}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"ASTER_RUNTIME_DIR": directory}), \
                patch("monitor.sample", side_effect=sample), redirect_stdout(StringIO()) as out:
            result = m.run(current, once=True)
            status = json.loads(out.getvalue())
            self.assertEqual(result, 1)
            self.assertEqual(status["status"], "partial")
            self.assertEqual(status["markets"]["CLUSD1:4"]["status"], "ok")
            self.assertEqual(status["markets"]["SPCXUSD1:5"]["status"], "error")
            persisted = json.loads((Path(directory) / "alerts.json").read_text())
            self.assertEqual(set(persisted["markets"]), {m.market_key(s, v) for s in current["symbols"] for v in current["leverages"]})

    @patch("manage.systemctl")
    def test_status_lists_all_symbols_and_rejects_partial_or_stale_success(self, systemctl):
        systemctl.return_value.stdout = "active"
        current = config()
        status = {"status": "ok", "markets": {m.market_key(s, v): {**reading(0), "status": "ok"} for s in current["symbols"] for v in current["leverages"]}}
        with patch("monitor.load_config", return_value=current), patch("manage.read_status", return_value=status), redirect_stdout(StringIO()) as out:
            self.assertEqual(manage.show_status(), 0)
            self.assertTrue(all(s in out.getvalue() for s in current["symbols"]))
            status["markets"]["SPCXUSD1:5"]["checked_at"] = "2000-01-01T00:00:00+00:00"
            self.assertEqual(manage.show_status(), 1)
            status["markets"].pop("SPCXUSD1:5")
            self.assertEqual(manage.show_status(), 1)

    @patch("monitor.socket.socket")
    def test_scheduler_pauses_all_new_requests_on_rate_limit(self, socket):
        current = {**config(), "poll_seconds": 0.01}
        shutdown = threading.Event()
        counts = dict.fromkeys(current["symbols"], 0)
        def sample(cfg):
            symbol = cfg["symbol"]
            counts[symbol] += 1
            if symbol == "SPCXUSD1":
                raise m.MonitorError("test rate limit", retry_after=5)
            return {v: reading(0) for v in cfg["leverages"]}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"ASTER_RUNTIME_DIR": directory}), \
                patch("monitor.sample", side_effect=sample), ThreadPoolExecutor(1) as pool:
            future = pool.submit(m.run, current, False, shutdown)
            try:
                deadline = time.monotonic() + 2
                state_path = Path(directory) / "status.json"
                while time.monotonic() < deadline:
                    if state_path.exists():
                        state = json.loads(state_path.read_text())
                        if all(row["status"] != "starting" for row in state.get("markets", {}).values()):
                            break
                    time.sleep(0.02)
                self.assertEqual(counts, dict.fromkeys(current["symbols"], 1))
                time.sleep(0.2)
                self.assertEqual(counts, dict.fromkeys(current["symbols"], 1))
            finally:
                shutdown.set()
                future.result(timeout=3)

    @patch("monitor.socket.socket")
    def test_shutdown_preserves_delivery_completed_by_worker(self, socket):
        current = {**config(), "feishu_enabled": True}
        shutdown = threading.Event()
        delivered = []
        def send(cfg, message):
            delivered.append((cfg["symbol"], cfg["leverage"]))
            shutdown.set()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"ASTER_RUNTIME_DIR": directory}), \
                patch("monitor.sample", return_value={4: reading(15000), 5: reading(15000)}), patch("monitor.send_feishu", side_effect=send):
            m.run(current, shutdown=shutdown)
            self.assertTrue(delivered)
            saved = json.loads((Path(directory) / "alerts.json").read_text())
            for symbol, leverage in delivered:
                self.assertTrue(m.restore_gate(saved, {**current, "leverage": leverage}, symbol).state["notified"])


if __name__ == "__main__":
    unittest.main()
