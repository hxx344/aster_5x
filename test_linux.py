"""Configuration and signal checks; Linux-specific checks skip on Windows."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import manage
import monitor as m


class ConfigTests(unittest.TestCase):
    def test_external_config_overrides_defaults_without_changing_original(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text('{"threshold":"20000","poll_seconds":10}', encoding="utf-8")
            with patch.dict(os.environ, {"ASTER_CONFIG_FILE": str(config)}, clear=True):
                result = m.load_config()
            self.assertEqual(result["threshold"], 20000)
            self.assertEqual(result["poll_seconds"], 10)
            self.assertFalse(result["feishu_enabled"])

    def test_invalid_external_config_fails_without_printing_its_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            for content in ('not-json-secret', '[]'):
                config.write_text(content, encoding="utf-8")
                with patch.dict(os.environ, {"ASTER_CONFIG_FILE": str(config)}, clear=True):
                    with self.assertRaises(m.MonitorError) as caught:
                        m.load_config()
                    self.assertNotIn(content, str(caught.exception))

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux file ownership")
    def test_config_permissions_preservation_and_rejected_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            manage.save_config(config, {"threshold": "10000", "feishu_enabled": False})
            original = config.read_bytes()
            self.assertEqual(config.stat().st_mode & 0o777, 0o640)
            with self.assertRaises(m.MonitorError):
                manage.save_config(config, {"threshold": "1", "poll_seconds": 1})
            self.assertEqual(config.read_bytes(), original)
            self.assertEqual(list(Path(directory).glob('.config-*')), [])

    def test_runtime_directory_override(self):
        with patch.dict(os.environ, {"ASTER_RUNTIME_DIR": "/example/runtime"}):
            self.assertEqual(m.runtime_dir(), Path("/example/runtime"))

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux configuration CLI")
    def test_configure_preserves_values_and_can_disable_feishu(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text(json.dumps({"threshold": "12345", "poll_seconds": 7,
                "feishu_enabled": True,
                "feishu_webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/test-placeholder",
                "feishu_sign_secret": "placeholder"}), encoding="utf-8")
            with patch.dict(os.environ, {"ASTER_CONFIG_FILE": str(config)}, clear=True), \
                    patch("manage.require_root"), patch("builtins.input", side_effect=["", ""]), \
                    patch("getpass.getpass", return_value="-"), patch("builtins.print"):
                manage.configure(restart=False)
            saved = json.loads(config.read_text())
            self.assertEqual(saved["threshold"], "12345")
            self.assertEqual(saved["poll_seconds"], 7)
            self.assertFalse(saved["feishu_enabled"])
            self.assertEqual(saved["feishu_webhook"], "")
            self.assertEqual(saved["feishu_sign_secret"], "")


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux signal handling")
class ShutdownTests(unittest.TestCase):
    def test_sigterm_marks_stopped_without_waiting_for_poll_interval(self):
        # Stub only external data and the OS lock, keeping the actual daemon loop,
        # CLI, signal handler and state files. No network calls or notifications.
        code = '''
import monitor as m
class Lock:
    def setsockopt(self, *args): pass
    def bind(self, *args): pass
    def close(self): pass
m.socket.socket = Lock
m.sample = lambda config: {v: {"value": "0", "global_remaining": "0", "bracket_cap": "5000000", "checked_at": m.now_iso()} for v in config["leverages"]}
raise SystemExit(m.main())
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config = path / "config.json"
            config.write_text('{"poll_seconds":60,"feishu_enabled":false}', encoding="utf-8")
            env = dict(os.environ, ASTER_CONFIG_FILE=str(config), ASTER_RUNTIME_DIR=str(path / "runtime"))
            process = subprocess.Popen([sys.executable, "-c", code], cwd=m.ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 10
                status_file = path / "runtime" / "status.json"
                while not status_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(status_file.exists())
                started = time.monotonic()
                process.send_signal(signal.SIGTERM)
                _, error = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, error.decode())
                self.assertLess(time.monotonic() - started, 5)
                self.assertEqual(json.loads(status_file.read_text())["status"], "stopped")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()


if __name__ == "__main__":
    unittest.main()
