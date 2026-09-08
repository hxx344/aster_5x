import base64
from decimal import Decimal
import hashlib
import hmac
import unittest
from unittest.mock import patch

import monitor as m


def payloads(value="12000", cap=5000000):
    return ({"success": True, "code": "000000", "data": {
        "symbol": "XAUUSD1", "leverageOiRemainingMap": {"1": "999999", "5": value}}},
        {"success": True, "code": "000000", "data": {"brackets": [{"symbol": "XAUUSD1", "riskBrackets": [
            {"minOpenPosLeverage": 5, "maxOpenPosLeverage": 5, "bracketNotionalCap": cap}]}]}})


class CapacityTests(unittest.TestCase):
    def test_exact_tier_and_bracket_cap(self):
        self.assertEqual(m.extract_capacity(*payloads(), "XAUUSD1", 5)[0], Decimal(12000))
        self.assertEqual(m.extract_capacity(*payloads(cap=11000), "XAUUSD1", 5)[0], Decimal(11000))

    def test_zero_valid_but_malformed_values_fail(self):
        self.assertEqual(m.extract_capacity(*payloads("0"), "XAUUSD1", 5)[0], 0)
        for value in (None, "NaN", "Infinity", "-1", True, "", "garbage"):
            with self.subTest(value=value), self.assertRaises(m.MonitorError):
                m.extract_capacity(*payloads(value), "XAUUSD1", 5)

    def test_wrong_symbol_missing_tier_and_business_failure(self):
        for mutation in (
            lambda p: p[0]["data"].update(symbol="BTCUSDT"),
            lambda p: p[0]["data"]["leverageOiRemainingMap"].pop("5"),
            lambda p: p[0].update(success=False),
            lambda p: p[1]["data"].update(brackets=[]),
            lambda p: p[1]["data"].update(brackets=[None]),
            lambda p: p[1]["data"]["brackets"][0].update(riskBrackets=None),
            lambda p: p[1]["data"]["brackets"][0].update(riskBrackets=[None]),
        ):
            p = payloads()
            mutation(p)
            with self.assertRaises(m.MonitorError):
                m.extract_capacity(*p, "XAUUSD1", 5)


class AlertTests(unittest.TestCase):
    def test_strict_threshold_crossing_and_no_flood(self):
        gate = m.AlertGate()
        sent = []
        for i, value in enumerate([0, 10000, Decimal("10000.01"), 15000, 10000, 15000]):
            gate.observe(value, 10000, 1000 + i * 301, 300, lambda: sent.append(value))
        self.assertEqual(sent, [Decimal("10000.01"), 15000])

    def test_cooldown_does_not_lose_pending_crossing(self):
        gate = m.AlertGate()
        sent = []
        deliver = lambda: sent.append(True)
        gate.observe(12000, 10000, 1000, 300, deliver)
        gate.observe(0, 10000, 1001, 300, deliver)
        gate.observe(12000, 10000, 1002, 300, deliver)
        self.assertEqual(len(sent), 1)
        gate.observe(12000, 10000, 1300, 300, deliver)
        self.assertEqual(len(sent), 2)

    def test_delivery_failure_remains_retryable(self):
        gate = m.AlertGate()
        with self.assertRaises(m.MonitorError):
            gate.observe(12000, 10000, 1000, 300, lambda: (_ for _ in ()).throw(m.MonitorError("failure")))
        self.assertFalse(gate.state["notified"])
        self.assertTrue(gate.observe(12000, 10000, 1005, 300, lambda: None))

    def test_restart_preserves_notification(self):
        gate = m.AlertGate({"notified": True, "last_alert": 1000})
        self.assertFalse(gate.observe(12000, 10000, 2000, 300, lambda: self.fail("Duplicate")))


class FeishuTests(unittest.TestCase):
    def test_signing_uses_empty_message(self):
        result = m.feishu_payload("hello", "example-secret", 1700000000)
        expected = base64.b64encode(hmac.new(b"1700000000\nexample-secret", b"", hashlib.sha256).digest()).decode()
        self.assertEqual(result["sign"], expected)
        self.assertEqual(result["timestamp"], "1700000000")
        self.assertNotIn("sign", m.feishu_payload("hello", "", 1700000000))

    @patch("monitor.request_json")
    def test_http_success_requires_business_ack(self, request):
        config = {"webhook": "unused", "secret": "", "timeout_seconds": 10}
        for response in ({"code": 19024}, {}, [], {"StatusCode": 1}):
            request.return_value = response
            with self.assertRaises(m.MonitorError):
                m.send_feishu(config, "test")
        for response in ({"code": 0}, {"StatusCode": 0}):
            request.return_value = response
            m.send_feishu(config, "test")


if __name__ == "__main__":
    unittest.main()
