"""Offline, standard-library tests for the local dual-robot configuration command."""
from contextlib import nullcontext, redirect_stderr, redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import signal
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("feishu_configuration", ROOT / "deploy/configure-feishu.py")
configuration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configuration)
SCHEDULED_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/scheduled-fixture"
EVENT_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/event-fixture"
LEGACY_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/legacy-fixture"


def values(scheduled=SCHEDULED_URL, scheduled_secret="scheduled-secret", event=EVENT_URL,
           event_secret="event-secret"):
    return dict(zip(configuration.KEYS, (scheduled, scheduled_secret, event, event_secret)))


class FeishuInputTests(unittest.TestCase):
    def test_webhook_has_a_strict_origin_and_single_token_path(self):
        for url in ("", SCHEDULED_URL, EVENT_URL, "https://open.feishu.cn/open-apis/bot/v2/hook/A_9-x"):
            self.assertEqual(configuration.validate_webhook(url), url)
        for url in ("http://open.feishu.cn/open-apis/bot/v2/hook/token",
                    "https://open.feishu.cn:443/open-apis/bot/v2/hook/token",
                    "https://user@open.feishu.cn/open-apis/bot/v2/hook/token",
                    "https://open.feishu.cn.evil.invalid/open-apis/bot/v2/hook/token",
                    "https://OPEN.FEISHU.CN/open-apis/bot/v2/hook/token",
                    SCHEDULED_URL + "?query=1", SCHEDULED_URL + "#fragment",
                    SCHEDULED_URL + "/tail", SCHEDULED_URL + "/", SCHEDULED_URL + "%2fmore",
                    SCHEDULED_URL + "\n", " " + SCHEDULED_URL,
                    "https://open.feishu.cn/open-apis/bot/v2/hook/",
                    "https://open.feishu.cn/open-apis/bot/v2/hook/" + "a" * 257):
            with self.subTest(url=url), self.assertRaises(ValueError):
                configuration.validate_webhook(url)

    def test_signing_secret_limits_and_environment_round_trip(self):
        for secret in ("", "x" * 256, 'spaces  "quotes" \\ $dollar `backtick` #hash 中文'):
            self.assertEqual(configuration.validate_secret(secret), secret)
            encoded = configuration.encode_value(secret)
            self.assertEqual(configuration.decode_value(encoded), secret)
        for secret in ("x" * 257, "line\nnext", "line\rnext", "nul\0value", "tab\tvalue", "delete\x7f",
                       "next\x85line", "line\u2028separator", "paragraph\u2029separator"):
            with self.subTest(secret=repr(secret)), self.assertRaises(ValueError):
                configuration.validate_secret(secret)

    def test_legacy_values_are_not_copied_to_either_new_channel(self):
        original = f'FEISHU_WEBHOOK_URL={LEGACY_URL}\nFEISHU_SIGN_SECRET=legacy-secret\n'
        ask, tell = Mock(side_effect=["", ""]), Mock()
        result = configuration.collect_values(original, ask=ask, tell=tell)
        self.assertEqual(result, values("", "", "", ""))
        self.assertEqual(ask.call_count, 2)
        messages = repr(ask.call_args_list) + repr(tell.call_args_list)
        self.assertNotIn(LEGACY_URL, messages)
        self.assertNotIn("legacy-secret", messages)
        self.assertIn("不会复制", messages)

    def test_partial_new_configuration_is_strict_and_empty_channel_stays_disabled(self):
        original = (f'FEISHU_WEBHOOK_URL={LEGACY_URL}\nFEISHU_SIGN_SECRET=legacy-secret\n'
                    f'FEISHU_EVENT_WEBHOOK_URL="{EVENT_URL}"\nFEISHU_EVENT_SIGN_SECRET="event-secret"\n')
        result = configuration.collect_values(original, ask=Mock(side_effect=["", "", ""]), tell=Mock())
        self.assertEqual(result, values("", "", EVENT_URL, "event-secret"))

    def test_blank_input_preserves_existing_independent_urls_and_secrets(self):
        original = configuration.update_environment("# keep\n", values())
        result = configuration.collect_values(original, ask=Mock(side_effect=["", "", "", ""]), tell=Mock())
        self.assertEqual(result, values())

    def test_replacing_a_robot_does_not_preserve_its_previous_secret(self):
        original = configuration.update_environment("", values())
        tell = Mock()
        result = configuration.collect_values(original, ask=Mock(side_effect=[EVENT_URL, "", "", ""]), tell=tell)
        self.assertEqual(result, values(EVENT_URL, "", EVENT_URL, "event-secret"))
        self.assertIn("不沿用旧密钥", repr(tell.call_args_list))
        self.assertNotIn("scheduled-secret", repr(tell.call_args_list))

    def test_dash_clears_url_and_secret_or_just_the_optional_signature(self):
        original = configuration.update_environment("", values())
        result = configuration.collect_values(original, ask=Mock(side_effect=["-", "", "-"]), tell=Mock())
        self.assertEqual(result, values("", "", EVENT_URL, ""))

    def test_duplicate_target_keys_are_replaced_without_changing_other_lines(self):
        unrelated = ('# existing comment\r\nASTER_ALLOW_LIVE=0\r\n'
                     'ASTER_A_PRIVATE_KEY="keep-private"\r\n'
                     f'FEISHU_WEBHOOK_URL={LEGACY_URL}\r\nFEISHU_SIGN_SECRET="legacy-secret"\r\n'
                     '# FEISHU_EVENT_WEBHOOK_URL=commented-example\r\n'
                     'ASTER_CAPACITY_RELAY_CA_FILE="/etc/aster-desk/relay-ca.pem"\r\n')
        original = unrelated + f'FEISHU_EVENT_WEBHOOK_URL={SCHEDULED_URL}\nFEISHU_EVENT_WEBHOOK_URL={EVENT_URL}\n'
        updated = configuration.update_environment(original, values())
        self.assertTrue(updated.startswith(unrelated))
        parsed, counts = configuration.read_channel_values(updated)
        self.assertEqual(parsed, values())
        self.assertEqual(counts, dict.fromkeys(configuration.KEYS, 1))

    def test_unchanged_values_preserve_existing_format_and_bytes(self):
        original = '# preserve formatting\r\n' + ''.join(
            f'{key} = \'{value}\'\r\n' for key, value in reversed(list(values().items())))
        self.assertEqual(configuration.update_environment(original, values()), original)

    def test_all_keys_are_required_and_malformed_target_values_are_rejected(self):
        with self.assertRaises(ValueError):
            configuration.update_environment("", {configuration.KEYS[0]: SCHEDULED_URL})
        with self.assertRaises(ValueError):
            configuration.read_channel_values('FEISHU_EVENT_SIGN_SECRET="unterminated\n')


class FeishuTransactionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.environment = self.directory / "environment"
        self.original = (f'# keep this comment\nFEISHU_WEBHOOK_URL={LEGACY_URL}\n'
                         'FEISHU_SIGN_SECRET=legacy-secret\nASTER_ALLOW_LIVE=0\n').encode()
        self.environment.write_bytes(self.original)
        self.environment.chmod(0o600)
        self.ca_file = self.directory / "relay-ca.pem"
        self.ca_file.write_bytes(b"preserved relay certificate")
        self.directory.chmod(0o750)

    def test_success_atomically_writes_four_keys_and_preserves_directory_and_ca(self):
        before_mode = stat.S_IMODE(self.directory.stat().st_mode)
        observed = []
        real_replace = configuration.os.replace
        def checked_replace(source, destination):
            observed.append(stat.S_IMODE(Path(source).stat().st_mode))
            self.assertEqual(Path(destination), self.environment)
            return real_replace(source, destination)
        restart = Mock()
        with patch.object(configuration.os, "replace", side_effect=checked_replace):
            self.assertTrue(configuration.apply_configuration(self.environment, values(), restart=restart))
        restart.assert_called_once_with()
        self.assertEqual(configuration.read_channel_values(self.environment.read_text())[0], values())
        self.assertEqual(self.ca_file.read_bytes(), b"preserved relay certificate")
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), before_mode)
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()), ["environment", "relay-ca.pem"])
        if os.name == "posix":
            self.assertEqual(observed, [0o600])
            self.assertEqual(stat.S_IMODE(self.environment.stat().st_mode), 0o600)

    def test_unchanged_configuration_does_not_write_or_restart(self):
        original = configuration.update_environment(self.original.decode(), values()).encode()
        self.environment.write_bytes(original)
        restart = Mock()
        with patch.object(configuration, "atomic_write") as write:
            self.assertFalse(configuration.apply_configuration(self.environment, values(), restart=restart))
        write.assert_not_called()
        restart.assert_not_called()
        self.assertEqual(self.environment.read_bytes(), original)

    def test_failed_restart_restores_exact_original_file_and_restarts_original_service(self):
        restart = Mock(side_effect=[ValueError(EVENT_URL + " secret-detail"), None])
        with self.assertRaises(configuration.ConfigurationUpdateFailed) as caught:
            configuration.apply_configuration(self.environment, values(), restart=restart)
        self.assertTrue(caught.exception.recovered)
        self.assertNotIn(EVENT_URL, str(caught.exception))
        self.assertEqual(self.environment.read_bytes(), self.original)
        self.assertEqual(restart.call_count, 2)

    def test_keyboard_interrupt_rolls_back_and_repeated_signals_are_ignored_during_recovery(self):
        calls = []
        def restart():
            calls.append(1)
            if len(calls) == 1:
                raise KeyboardInterrupt()
            self.assertEqual(self.environment.read_bytes(), self.original)
            for signum in configuration.SIGNALS:
                self.assertEqual(signal.getsignal(signum), signal.SIG_IGN)
        with self.assertRaises(configuration.ConfigurationUpdateFailed) as caught:
            configuration.apply_configuration(self.environment, values(), restart=restart)
        self.assertTrue(caught.exception.recovered)
        self.assertTrue(caught.exception.interrupted)
        self.assertEqual(len(calls), 2)

    def test_signal_immediately_after_replace_is_detected_and_rolled_back(self):
        original_write = configuration.atomic_write
        writes = []
        def interrupted_write(path, data):
            original_write(path, data)
            writes.append(data)
            if len(writes) == 1:
                raise configuration.ConfigurationInterrupted(signal.SIGTERM)
        restart = Mock()
        with patch.object(configuration, "atomic_write", side_effect=interrupted_write):
            with self.assertRaises(configuration.ConfigurationUpdateFailed) as caught:
                configuration.apply_configuration(self.environment, values(), restart=restart)
        self.assertTrue(caught.exception.recovered)
        self.assertTrue(caught.exception.interrupted)
        self.assertEqual(writes[-1], self.original)
        self.assertEqual(self.environment.read_bytes(), self.original)
        restart.assert_called_once()

    def test_write_failure_before_replacement_does_not_restart_service(self):
        restart = Mock()
        with patch.object(configuration, "atomic_write", side_effect=OSError("disk full")):
            with self.assertRaises(configuration.ConfigurationUpdateFailed) as caught:
                configuration.apply_configuration(self.environment, values(), restart=restart)
        self.assertTrue(caught.exception.recovered)
        restart.assert_not_called()
        self.assertEqual(self.environment.read_bytes(), self.original)

    def test_failed_recovery_is_reported_without_claiming_success(self):
        restart = Mock(side_effect=[ValueError("failed"), ValueError("still failed")])
        with self.assertRaises(configuration.ConfigurationUpdateFailed) as caught:
            configuration.apply_configuration(self.environment, values(), restart=restart)
        self.assertFalse(caught.exception.recovered)
        self.assertEqual(self.environment.read_bytes(), self.original)

    def test_changed_file_is_not_overwritten_and_invalid_input_does_not_write(self):
        restart = Mock()
        self.environment.write_bytes(self.original + b"# simultaneous change\n")
        with self.assertRaises(configuration.ConfigurationConflict):
            configuration.apply_configuration(self.environment, values(), restart=restart, expected=self.original)
        with self.assertRaises(ValueError):
            configuration.apply_configuration(self.environment, values(event="https://invalid.test"), restart=restart)
        self.assertTrue(self.environment.read_bytes().endswith(b"# simultaneous change\n"))
        restart.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "Linux advisory lock and permissions")
    def test_concurrent_configuration_is_rejected_and_lock_releases_after_failure(self):
        path = self.directory / "upgrade.lock"
        with configuration.configuration_lock(path):
            with self.assertRaises(configuration.ConfigurationConflict):
                with configuration.configuration_lock(path):
                    self.fail("Concurrent operation entered the lock")
        with configuration.configuration_lock(path):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


class FeishuCommandTests(unittest.TestCase):
    def test_local_restart_health_only_uses_loopback_and_suppresses_subprocess_output(self):
        response = Mock(status=200)
        response.read.return_value = b'{"status":"ok"}'
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.object(configuration.http.client, "HTTPConnection", return_value=connection) as connect, \
                patch.object(configuration.subprocess, "run") as run:
            configuration.restart_desk(attempts=1)
        connect.assert_called_once_with("127.0.0.1", 8765, timeout=2)
        connection.request.assert_called_once_with("GET", "/api/health")
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["stdout"], subprocess.DEVNULL)
            self.assertEqual(call.kwargs["stderr"], subprocess.DEVNULL)
            self.assertNotIn("feishu", " ".join(call.args[0]))

    def test_cli_does_not_echo_credential_arguments_or_exception_details(self):
        output = io.StringIO()
        with redirect_stderr(output):
            self.assertEqual(configuration.main([EVENT_URL, "private-secret"]), 2)
        self.assertNotIn(EVENT_URL, output.getvalue())
        self.assertNotIn("private-secret", output.getvalue())
        output = io.StringIO()
        with patch.object(configuration.os, "name", "posix"), \
                patch.object(configuration.os, "geteuid", return_value=0, create=True), \
                patch.object(configuration.sys.stdin, "isatty", return_value=True), \
                patch.object(configuration, "configuration_lock", return_value=nullcontext()), \
                patch.object(configuration, "read_environment", side_effect=ValueError(EVENT_URL + " private-secret")), \
                redirect_stderr(output):
            self.assertEqual(configuration.main([]), 1)
        self.assertNotIn(EVENT_URL, output.getvalue())
        self.assertNotIn("private-secret", output.getvalue())

    def test_cli_requires_root_and_a_terminal_before_reading_configuration(self):
        with patch.object(configuration.os, "name", "posix"), \
                patch.object(configuration.os, "geteuid", return_value=1, create=True), \
                patch.object(configuration, "read_environment") as read, redirect_stderr(io.StringIO()):
            self.assertEqual(configuration.main([]), 1)
            read.assert_not_called()
        with patch.object(configuration.os, "name", "posix"), \
                patch.object(configuration.os, "geteuid", return_value=0, create=True), \
                patch.object(configuration.sys.stdin, "isatty", return_value=False), \
                patch.object(configuration, "read_environment") as read, redirect_stderr(io.StringIO()):
            self.assertEqual(configuration.main([]), 1)
            read.assert_not_called()

    def test_no_terminal_echo_fallback_is_allowed(self):
        import warnings
        def fallback(_text):
            warnings.warn("hidden input unavailable", configuration.getpass.GetPassWarning)
        with patch.object(configuration.os, "name", "posix"), \
                patch.object(configuration.os, "geteuid", return_value=0, create=True), \
                patch.object(configuration.sys.stdin, "isatty", return_value=True), \
                patch.object(configuration, "configuration_lock", return_value=nullcontext()), \
                patch.object(configuration, "read_environment", return_value=b""), \
                patch.object(configuration, "collect_values", side_effect=fallback), \
                patch.object(configuration, "apply_configuration") as apply, \
                redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            self.assertEqual(configuration.main([]), 1)
            apply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
