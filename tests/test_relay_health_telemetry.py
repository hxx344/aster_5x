"""WS health evidence is local, monotonic and independent of HTTP fallback."""
import json
import os
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

from tests import test_capacity_relay_status as fixtures


class RelayHealthTelemetryTests(unittest.TestCase):
    setUp = fixtures.RelayStatusTests.setUp
    request = fixtures.RelayStatusTests.request
    envelope = fixtures.RelayStatusTests.envelope
    advance = fixtures.RelayStatusTests.advance

    def connected(self):
        self.client._connected = True
        self.client._ws_connected_ticks = self.ticks
        self.client._ws_disconnected_ticks = None

    def test_instance_and_durations_are_read_only_and_ignore_wall_clock_changes(self):
        original = self.client.status()
        self.assertEqual(str(UUID(original["instance_id"])), original["instance_id"])
        self.wall += 10000
        self.ticks += 7
        self.assertEqual(self.client.status()["ws"]["disconnected_age_seconds"], 7)
        self.connected()
        self.wall -= 20000
        self.ticks += 4
        current = self.client.status()
        self.assertEqual(current["instance_id"], original["instance_id"])
        self.assertEqual(current["ws"]["connected_age_seconds"], 4)
        self.assertEqual(current["ws"]["oi_idle_seconds"], 4)
        self.assertFalse(current["ws"]["has_oi_sample"])
        self.assertEqual(self.requests, [])

    def test_only_new_valid_ws_oi_updates_health(self):
        self.connected()
        original = self.envelope()
        self.assertTrue(self.client._accept(original))
        self.advance(2)
        self.assertTrue(self.client._accept(self.envelope(kind="brackets")))
        self.assertTrue(self.client._accept(self.envelope(sequence=2), source="http"))
        self.assertFalse(self.client._accept(self.envelope(sequence=2)))
        self.assertFalse(self.client._accept(self.envelope(sequence=3, payload=None)))
        self.assertFalse(self.client._accept(self.envelope(sequence=3, age=9)))
        health = self.client.status()["ws"]
        self.assertEqual(health["last_oi_sample_at"], 1000)
        self.assertEqual(health["oi_idle_seconds"], 2)
        self.assertTrue(health["has_oi_sample"])
        self.assertTrue(self.client._accept(self.envelope(sequence=3)))
        self.assertEqual(self.client.status()["ws"]["oi_idle_seconds"], 0)
        self.assertEqual(self.client.status()["ws"]["last_oi_sample_at"], 1002)

    def test_handshake_failures_count_once_each_and_preserve_disconnected_duration(self):
        attempts, snapshots = [], []
        def connect(*_, **__):
            attempts.append(self.ticks)
            raise RuntimeError("private transport detail")
        def retry(delay):
            snapshots.append(self.client.status())
            if len(attempts) == 3:
                return True
            self.advance(delay)
            return False
        self.client._connect = connect
        with patch.object(self.client._stop, "wait", side_effect=retry):
            self.client._run()
        self.assertEqual([s["ws"]["failure_count"] for s in snapshots], [1, 2, 3])
        self.assertEqual(snapshots[-1]["ws"]["disconnected_age_seconds"], 1.5)
        self.assertEqual(snapshots[-1]["ws"]["connection_attempts"], 3)
        self.assertNotIn("private transport detail", json.dumps(snapshots))
        self.assertEqual(self.requests, [])

    def test_short_connections_keep_failures_and_reset_current_connection_oi(self):
        outer, connections, starts, received = self, [], [], []
        class Connection:
            def __enter__(self):
                self.step = 0
                connections.append(self)
                return self
            def __exit__(self, *args):
                return False
            def recv(self, timeout):
                self.step += 1
                if self.step == 1:
                    starts.append(outer.client.status()["ws"])
                    return json.dumps(outer.envelope(sequence=len(connections)))
                received.append(outer.client.status()["ws"])
                outer.advance(.1)
                raise RuntimeError("disconnected")
        self.client._connect = lambda *_, **__: Connection()
        def retry(delay):
            if len(connections) == 3:
                return True
            self.advance(delay)
            return False
        with patch.object(self.client._stop, "wait", side_effect=retry):
            self.client._run()
        self.assertEqual([s["failure_count"] for s in starts], [0, 1, 2])
        self.assertTrue(all(not s["has_oi_sample"] and s["oi_idle_seconds"] == 0 for s in starts))
        self.assertTrue(all(s["has_oi_sample"] for s in received))
        self.assertEqual(self.client.status()["ws"]["failure_count"], 3)
        self.assertIsNone(self.client.status()["ws"]["oi_idle_seconds"])

    def test_intentional_close_does_not_count_failure_or_accept_late_oi(self):
        outer = self
        class Connection:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def recv(self, timeout):
                outer.client.close()
                return json.dumps(outer.envelope())
        self.client._connect = lambda *_, **__: Connection()
        self.client._run()
        status = self.client.status()
        self.assertTrue(status["closed"])
        self.assertEqual(status["ws"]["failure_count"], 0)
        self.assertFalse(status["ws"]["has_oi_sample"])
        self.assertIsNone(status["ws"]["last_oi_sample_at"])

    def test_real_telemetry_drives_fault_and_recovery_without_exchange_or_http_requests(self):
        from tests.helpers import Fixture
        from trading.engine import Engine
        from trading.exchange import MarketData
        fixture = Fixture()
        self.addCleanup(fixture.close)
        api = Mock()
        engine = Engine(fixture.store, market=MarketData(api=api, capacity_relay=self.client))
        self.addCleanup(engine.dashboard_reports.close)
        self.client._connect = Mock(side_effect=RuntimeError("handshake failed"))
        with patch.object(self.client._stop, "wait", side_effect=[False, False, True]):
            self.client._run()
        outer = self
        class Connection:
            def __enter__(self):
                self.step = 0
                return self
            def __exit__(self, *args):
                return False
            def recv(self, timeout):
                self.step += 1
                if self.step == 1:
                    return json.dumps(outer.envelope())
                engine.notify()
                outer.advance(30)
                engine.notify()
                outer.client.close()
                return "closing"
        with patch.dict(os.environ, {"FEISHU_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/hook/unused-test",
                                     "ASTER_CAPACITY_ALERT_ENABLED": "0"}), \
             patch("trading.engine.time.time", side_effect=lambda: self.wall), \
             patch("monitor.send_feishu") as send, \
             patch.object(self.client._http, "stream", side_effect=AssertionError("HTTP not allowed")) as http:
            engine.notify()
            engine.notify()
            self.assertEqual(send.call_count, 1)
            self.assertIn("WS 异常", send.call_args.args[1])
            self.client._connect = lambda *_, **__: Connection()
            self.client._run()
            self.assertEqual(send.call_count, 2)
            self.assertIn("WS 已恢复", send.call_args.args[1])
            http.assert_not_called()
        self.assertEqual(api.mock_calls, [])
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
