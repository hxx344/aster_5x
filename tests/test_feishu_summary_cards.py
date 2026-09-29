"""Native cards survive the existing queue; event/legacy text remains compatible."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import monitor
from trading.hourly_summary import format_hourly_summary
from trading.store import Store
from tests.test_hourly_summary import NOW, paired_state


class FeishuSummaryCardTests(unittest.TestCase):
    def test_signed_card_and_legacy_text_share_signature_without_changing_text(self):
        envelope = format_hourly_summary(paired_state(), NOW)
        payload = monitor.feishu_payload(envelope, "test-secret", int(NOW))
        text = monitor.feishu_payload("事件消息", "test-secret", int(NOW))
        self.assertEqual(payload["msg_type"], "interactive")
        self.assertEqual(payload["card"]["schema"], "2.0")
        self.assertEqual(payload["sign"], text["sign"])
        self.assertEqual(payload["timestamp"], text["timestamp"])
        self.assertEqual(text["content"], {"text": "事件消息"})
        self.assertNotIn("content", payload)
        self.assertNotIn("sign", monitor.feishu_payload(envelope, "", int(NOW)))

    def test_json_looking_text_is_not_promoted_to_a_card(self):
        message = '{"schema":"2.0","body":{"elements":[]}}'
        self.assertEqual(monitor.feishu_payload(message, "", 0),
                         {"msg_type": "text", "content": {"text": message}})

    def test_malformed_envelopes_fail_without_echoing_private_data(self):
        for message in ("private-token", "[]", '{"schema":"1.0"}',
                        '{"schema":"2.0","body":[]}', '{"schema":"2.0","body":{}}'):
            with self.subTest(message=message), self.assertRaisesRegex(monitor.MonitorError, "^Invalid queued Feishu card$"):
                monitor.feishu_payload(monitor.FEISHU_CARD_PREFIX + message, "", 0)

    def test_oversize_wire_payload_is_rejected_before_http(self):
        card = {"schema": "2.0", "body": {"elements": [{"tag": "div", "text": {
            "tag": "plain_text", "content": "长" * 4000}}]}}
        message = monitor.FEISHU_CARD_PREFIX + json.dumps(card, ensure_ascii=False)
        with patch("monitor.request_json") as request, self.assertRaisesRegex(monitor.MonitorError, "size limit"):
            monitor.send_feishu({"webhook": "unused", "secret": "", "timeout_seconds": 10}, message)
        request.assert_not_called()

    def test_card_retry_and_restart_use_persisted_envelope_and_original_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            store.hourly_summary_due(available=True, now=NOW-3600)
            token = store.hourly_summary_due(available=True, now=NOW)
            envelope = format_hourly_summary(paired_state(), NOW)
            self.assertTrue(store.enqueue_hourly_summary(token, envelope, now=NOW))
            with patch("trading.store.time.time", return_value=NOW):
                item = store.due_notifications()[0]
                store.notification_result(item, False)
            restarted = Store(store.path)
            with patch("trading.store.time.time", return_value=NOW+10):
                retry = restarted.due_notifications()[0]
                self.assertEqual(retry["message"], envelope)
                self.assertEqual(retry["attempts"], 1)
                self.assertEqual(retry["expires_at"], item["expires_at"])
                with patch("monitor.request_json", return_value={"code": 0}) as request:
                    monitor.send_feishu({"webhook": "unused", "secret": "", "timeout_seconds": 10}, retry["message"])
                self.assertEqual(request.call_args.args[1]["msg_type"], "interactive")
                restarted.notification_result(retry, True)
                self.assertEqual(restarted.pending_notifications(), 0)


if __name__ == "__main__":
    unittest.main()
