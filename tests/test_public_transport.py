import unittest
from urllib.error import HTTPError
from unittest.mock import patch

import monitor


class PublicTransportTests(unittest.TestCase):
    def test_invalid_retry_after_cannot_freeze_the_standalone_monitor(self):
        for status, expected in ((429, 180), (418, 86400)):
            for value in ("Infinity", "NaN", "garbage", "-1"):
                with self.subTest(status=status, value=value):
                    error = HTTPError("https://example.invalid", status, "limited", {"Retry-After": value}, None)
                    with patch.object(monitor, "build_opener") as opener:
                        opener.return_value.open.side_effect = error
                        with self.assertRaises(monitor.MonitorError) as caught:
                            monitor.request_json("https://example.invalid")
                        self.assertEqual(caught.exception.retry_after, expected)
                        self.assertNotIn("example.invalid", str(caught.exception))

    def test_numeric_payload_cannot_expand_to_unbounded_output(self):
        for value in ("9" * 129, "1e1000000000", "1e-1000000000", "0e1000000000"):
            with self.subTest(value=value[:30]), self.assertRaises(monitor.MonitorError):
                monitor.number(value)
