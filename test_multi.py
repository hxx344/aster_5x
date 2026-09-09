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
        return m.validate_config(json.loads((m.ROOT / "config.json").read_text()))


def reading(value):
    return {"value": str(value), "global_remaining": str(value),
            "bracket_cap": "5000000", "checked_at": m.now_iso()}


class MultiMarketTests(unittest.TestCase):
    def test_defaults_and_legacy_config_adopt_all_three(self):
        current = config()
        self.assertEqual(current["symbols"], list(m.SUPPORTED_SYMBOLS))
        legacy = {**current, "symbol": "XAUUSD1", "threshold": "22000"}
        legacy.pop("symbols")
        upgraded = m.validate_config(legacy)
        self.assertEqual(upgraded["symbols"], list(m.SUPPORTED_SYMBOLS))
        self.assertEqual(upgraded["threshold"], 22000)
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
    @patch("monitor.sample", return_value=reading(15000))
    def test_each_market_alerts_independently_with_correct_destination(self, sample, send):
        current = {**config(), "feishu_enabled": True}
        trackers = [m.MarketMonitor(current, symbol, {}, threading.Event()) for symbol in current["symbols"]]
        for tracker in trackers:
            tracker.check()
            tracker.check()
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
                tracker.check()
            self.assertEqual(trackers["SPCXUSD1"].status["status"], "error")
            send.side_effect = None
            for tracker in trackers.values():
                tracker.check()
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
            return reading(10)
        current = config()
        slow = m.MarketMonitor(current, "SPCXUSD1", {}, threading.Event())
        fast = m.MarketMonitor(current, "CLUSD1", {}, threading.Event())
        with patch("monitor.sample", side_effect=sample), ThreadPoolExecutor(2) as pool:
            pending = pool.submit(slow.check)
            try:
                self.assertTrue(waiting.wait(2))
                self.assertEqual(pool.submit(fast.check).result(timeout=2)[0]["status"], "ok")
                self.assertFalse(pending.done())
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=2)[0]["status"], "error")

    @patch("monitor.socket.socket")
    def test_once_persists_per_market_results_and_reports_partial_failure(self, socket):
        current = config()
        def sample(cfg):
            if cfg["symbol"] == "SPCXUSD1":
                raise m.MonitorError("test network failure")
            return reading(0)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"ASTER_RUNTIME_DIR": directory}), \
                patch("monitor.sample", side_effect=sample), redirect_stdout(StringIO()) as out:
            result = m.run(current, once=True)
            status = json.loads(out.getvalue())
            self.assertEqual(result, 1)
            self.assertEqual(status["status"], "partial")
            self.assertEqual(status["markets"]["CLUSD1"]["status"], "ok")
            self.assertEqual(status["markets"]["SPCXUSD1"]["status"], "error")
            persisted = json.loads((Path(directory) / "alerts.json").read_text())
            self.assertEqual(set(persisted["markets"]), set(current["symbols"]))

    @patch("manage.systemctl")
    def test_status_lists_all_symbols_and_rejects_partial_or_stale_success(self, systemctl):
        systemctl.return_value.stdout = "active"
        current = config()
        status = {"status": "ok", "markets": {s: {**reading(0), "status": "ok"} for s in current["symbols"]}}
        with patch("monitor.load_config", return_value=current), patch("manage.read_status", return_value=status), redirect_stdout(StringIO()) as out:
            self.assertEqual(manage.show_status(), 0)
            self.assertTrue(all(s in out.getvalue() for s in current["symbols"]))
            status["markets"]["SPCXUSD1"]["checked_at"] = "2000-01-01T00:00:00+00:00"
            self.assertEqual(manage.show_status(), 1)
            status["markets"].pop("SPCXUSD1")
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
            return reading(0)
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
            delivered.append(cfg["symbol"])
            shutdown.set()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"ASTER_RUNTIME_DIR": directory}), \
                patch("monitor.sample", return_value=reading(15000)), patch("monitor.send_feishu", side_effect=send):
            m.run(current, shutdown=shutdown)
            self.assertTrue(delivered)
            saved = json.loads((Path(directory) / "alerts.json").read_text())
            for symbol in delivered:
                self.assertTrue(m.restore_gate(saved, current, symbol).state["notified"])


if __name__ == "__main__":
    unittest.main()
