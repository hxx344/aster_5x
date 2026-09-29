"""Offline relay configuration tests and isolated Linux installer fixtures."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("relay_configuration", ROOT / "deploy/configure-capacity-relay.py")
configuration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configuration)
OPENSSL = shutil.which("openssl") or (r"D:\Git\usr\bin\openssl.exe" if Path(r"D:\Git\usr\bin\openssl.exe").is_file() else None)


@unittest.skipUnless(OPENSSL, "Native OpenSSL required for certificate fixtures")
class RelayConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.server = Path(cls.temporary.name) / "server"
        with patch.dict(os.environ, {"PATH": str(Path(OPENSSL).parent) + os.pathsep + os.environ.get("PATH", "")}):
            configuration.prepare_server(cls.server, "127.0.0.1")
        cls.config = configuration.server_connection(cls.server)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.original = '# Existing dashboard settings\nASTER_ALLOW_LIVE=0\nASTER_A_PRIVATE_KEY="unchanged-private-value"\n# Preserve this comment\n'
        (self.directory / "environment").write_text(self.original, encoding="utf-8")
        self.restart = Mock()
        self.verify = Mock()

    def apply(self, config, **kwargs):
        return configuration.apply_connection(config, self.directory, restart=kwargs.get("restart", self.restart),
            verify=kwargs.get("verify", self.verify), service_group=None)

    def test_import_is_idempotent_and_disconnect_preserves_all_other_settings(self):
        self.assertTrue(self.apply(self.config))
        imported = (self.directory / "environment").read_text()
        self.assertTrue(imported.startswith(self.original))
        values = configuration.read_environment(self.directory / "environment")
        self.assertEqual(values["ASTER_CAPACITY_RELAY_TOKEN"], self.config["token"])
        self.assertEqual((self.directory / "relay-ca.pem").read_text(), self.config["ca_pem"])
        self.assertFalse(self.apply(self.config))
        self.restart.assert_called_once()
        if os.name == "posix":
            self.assertEqual((self.directory / "environment").stat().st_mode & 0o777, 0o600)
            self.assertEqual((self.directory / "relay-ca.pem").stat().st_mode & 0o777, 0o640)
        self.assertTrue(self.apply(None))
        self.assertEqual((self.directory / "environment").read_text(), self.original)
        self.assertFalse((self.directory / "relay-ca.pem").exists())

    def test_failed_restart_restores_environment_and_previous_certificate(self):
        self.apply(self.config)
        original = (self.directory / "environment").read_bytes()
        certificate = (self.directory / "relay-ca.pem").read_bytes()
        restart = Mock(side_effect=[ValueError("fixture health failure"), None])
        with self.assertRaises(ValueError):
            self.apply({**self.config, "token": "replacement-" + "a" * 32}, restart=restart)
        self.assertEqual((self.directory / "environment").read_bytes(), original)
        self.assertEqual((self.directory / "relay-ca.pem").read_bytes(), certificate)
        self.assertEqual(restart.call_count, 2)

    def test_failed_preflight_does_not_write_or_restart(self):
        with self.assertRaises(ValueError):
            self.apply(self.config, verify=Mock(side_effect=ValueError("fixture unreachable")))
        self.assertEqual((self.directory / "environment").read_text(), self.original)
        self.assertFalse((self.directory / "relay-ca.pem").exists())
        self.restart.assert_not_called()

    def test_rejects_unsafe_addresses_tokens_and_certificates(self):
        for field, value in (("url", "http://127.0.0.1:8766"), ("url", "https://user:password@example.com"),
                ("url", "https://example.com/path"), ("url", "https://example.com?token=a"),
                ("url", "https://example.com:99999"), ("token", "a" * 32 + "\r\nHeader:value"),
                ("ca_pem", "not a certificate")):
            with self.subTest(field=field, value=value), self.assertRaises((ValueError, configuration.ssl.SSLError)):
                configuration.validate_connection(json.dumps({**self.config, field: value}))

    def test_token_with_shell_metacharacters_remains_literal(self):
        token = "$(not-executed);" + "a" * 32
        config = configuration.validate_connection(json.dumps({**self.config, "token": token}))
        self.apply(config)
        self.assertEqual(configuration.read_environment(self.directory / "environment")["ASTER_CAPACITY_RELAY_TOKEN"], token)
        self.restart.assert_called_once()

    def test_repeat_server_setup_preserves_token_private_key_and_configuration(self):
        files = {name: (self.server / name).read_bytes() for name in ("environment", "server.key", "server.crt", "connection.json")}
        configuration.prepare_server(self.server, "127.0.0.1")
        self.assertEqual({name: (self.server / name).read_bytes() for name in files}, files)
        with self.assertRaises(ValueError):
            configuration.prepare_server(self.server, "127.0.0.2")

    def test_generated_certificate_supports_strict_hostname_verified_tls(self):
        import http.server
        import ssl
        import threading
        config = self.config

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.headers.get("Authorization") != "Bearer " + config["token"]:
                    self.send_error(401)
                    return
                body = b'{"status":"ok"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.server / "server.crt", self.server / "server.key")
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.shutdown)
        secure_config = {**config, "url": "https://127.0.0.1:" + str(server.server_port)}
        real_create_context = ssl.create_default_context

        def strict_context(*args, **kwargs):
            result = real_create_context(*args, **kwargs)
            result.verify_flags |= ssl.VERIFY_X509_STRICT
            return result

        with patch.object(configuration.ssl, "create_default_context", side_effect=strict_context):
            self.assertEqual(configuration.request_health(secure_config), {"status": "ok"})
            with self.assertRaises(ssl.SSLError):
                configuration.request_health({**secure_config, "url": secure_config["url"].replace("127.0.0.1", "localhost")})


class RelayInstallerHarness:
    def __init__(self, test):
        self.test = test
        temporary = tempfile.TemporaryDirectory()
        test.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.source, self.commands, self.root = (self.base / name for name in ("source", "commands", "opt"))
        self.etc, self.unit, self.cli = (self.base / name for name in ("etc", "service", "cli"))
        for path in (self.source / "trading", self.source / "deploy", self.commands):
            path.mkdir(parents=True)
        self.mappings = {"/opt/aster-capacity-relay": str(self.root), "/etc/aster-capacity-relay": str(self.etc),
            "/etc/systemd/system/aster-capacity-relay.service": str(self.unit), "/usr/local/bin/aster-capacity-relay": str(self.cli),
            "/run/systemd/system": str(self.base)}
        for name in ("install-relay.sh", "deploy/configure-capacity-relay.py", "deploy/aster-capacity-relay.service",
                     "deploy/aster-capacity-relay", "deploy/relay.env.example"):
            text = (ROOT / name).read_text()
            for before, after in self.mappings.items():
                text = text.replace(before, after)
            (self.source / name).write_text(text)
        for name in ("monitor.py", "trading/__init__.py", "trading/capacity_relay_server.py", "requirements-relay.lock"):
            (self.source / name).write_text("")
        (self.base / "remote-revision").write_text("a" * 40)
        (self.base / "state").write_text("stopped")
        self.install_commands()

    def executable(self, name, body):
        path = self.commands / name
        path.write_text("#!/bin/bash\nset -e\n" + body + "\n")
        path.chmod(0o755)

    def install_commands(self):
        for name in ("id", "chown", "useradd", "sleep"):
            self.executable(name, "exit 0")
        self.executable("install", '''args=()
while (($#)); do
  case $1 in -o|-g) shift 2 ;; *) args+=("$1"); shift ;; esac
done
exec /usr/bin/install "${args[@]}"''')
        self.executable("python3", '''if [[ ${1:-} == -m && ${2:-} == venv ]]; then
  printf '1\\n' >> "$HARNESS_BASE/dependencies"
  mkdir -p "$3/bin"
  cp "$HARNESS_BASE/commands/venv-python" "$3/bin/python"
elif [[ ${1:-} == */deploy/configure-capacity-relay.py && ${2:-} == health ]]; then
  [[ ! -f "$HARNESS_BASE/fail-health" ]] || exit 44
  printf '{"status":"ok"}\\n'
else exec "$HARNESS_PYTHON" "$@"; fi''')
        self.executable("venv-python", '''if [[ ${1:-} == -m && ${2:-} == pip ]]; then
  [[ ! -f "$HARNESS_BASE/fail-pip" ]] || exit 42
  exit 0
fi
exec "$HARNESS_PYTHON" "$@"''')
        self.executable("systemd-run", '''printf '1\\n' >> "$HARNESS_BASE/validation"
[[ ! -f "$HARNESS_BASE/fail-validation" ]] || exit 43''')
        self.executable("systemctl", '''printf '%s\\n' "$*" >> "$HARNESS_BASE/service-calls"
case $1 in
  is-active) [[ $(cat "$HARNESS_BASE/state") == active ]] ;;
  is-enabled) exit 0 ;;
  show) printf 'no\\n' ;;
  restart|start) printf active > "$HARNESS_BASE/state" ;;
  stop) printf stopped > "$HARNESS_BASE/state" ;;
esac''')
        self.executable("curl", '''case "$*" in
  *api.github.com/repos/hxx344/aster_5x/commits/main*) printf '{"sha":"%s"}' "$(cat "$HARNESS_BASE/remote-revision")" ;;
  *codeload.github.com/hxx344/aster_5x/tar.gz/*)
    printf '1\\n' >> "$HARNESS_BASE/downloads"
    while (($#)); do if [[ $1 == -o ]]; then target=$2; break; fi; shift; done
    tar -C "$HARNESS_BASE" -czf "$target" source ;;
  *) exit 41 ;;
esac''')

    def run(self, *, fail=None, remote=False):
        if fail:
            (self.base / ("fail-" + fail)).touch()
        try:
            command = ["bash", "-s", "--", "--address", "127.0.0.1"] if remote else ["bash", str(self.source / "install-relay.sh"), "--address", "127.0.0.1"]
            return subprocess.run(command, cwd=self.base if remote else self.source, capture_output=True, text=True, timeout=45,
                input=(self.source / "install-relay.sh").read_text() if remote else None,
                env={**os.environ, "PATH": str(self.commands) + ":/usr/bin:/bin", "HARNESS_BASE": str(self.base), "HARNESS_PYTHON": sys.executable})
        finally:
            if fail:
                (self.base / ("fail-" + fail)).unlink()

    def success(self, **kwargs):
        result = self.run(**kwargs)
        self.test.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)
        return self.current

    @property
    def current(self):
        return (self.root / "current").resolve()

    def count(self, name):
        path = self.base / name
        return len(path.read_text().splitlines()) if path.exists() else 0


@unittest.skipUnless(sys.platform == "linux" and hasattr(os, "geteuid") and os.geteuid() == 0,
                     "Linux root required for isolated installer fixture; never run through WSL")
class RelayInstallerTests(unittest.TestCase):
    def setUp(self):
        self.h = RelayInstallerHarness(self)

    def test_first_install_then_unchanged_repeat_skips_dependencies_validation_and_restart(self):
        first = self.h.success()
        calls = (self.h.base / "service-calls").read_text().count("restart ")
        self.h.success()
        self.assertEqual(self.h.current, first)
        self.assertEqual(self.h.count("dependencies"), 1)
        self.assertEqual(self.h.count("validation"), 1)
        self.assertEqual((self.h.base / "service-calls").read_text().count("restart "), calls)
        self.assertEqual((self.h.etc / "environment").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.h.etc / "server.key").stat().st_mode & 0o777, 0o640)

    def test_remote_unchanged_revision_skips_archive_download(self):
        first = self.h.success(remote=True)
        self.assertEqual(self.h.success(remote=True), first)
        self.assertEqual(self.h.count("downloads"), 1)

    def test_code_upgrade_reuses_dependencies_and_preserves_credentials_and_certificate(self):
        old = self.h.success()
        saved = {name: (self.h.etc / name).read_bytes() for name in ("environment", "server.key", "server.crt", "connection.json")}
        (self.h.source / "trading/capacity_relay_server.py").write_text("# Changed implementation\n")
        new = self.h.success()
        self.assertNotEqual(old, new)
        self.assertTrue(old.is_dir())
        self.assertEqual(self.h.count("dependencies"), 1)
        self.assertEqual({name: (self.h.etc / name).read_bytes() for name in saved}, saved)

    def test_failed_health_check_restores_previous_release_and_service(self):
        old = self.h.success()
        unit, cli = self.h.unit.read_bytes(), self.h.cli.read_bytes()
        (self.h.source / "trading/capacity_relay_server.py").write_text("# Changed implementation\n")
        result = self.h.run(fail="health")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.h.current, old)
        self.assertEqual((self.h.base / "state").read_text(), "active")
        self.assertEqual((self.h.unit.read_bytes(), self.h.cli.read_bytes()), (unit, cli))
        self.assertEqual(len(list((self.h.root / "releases").iterdir())), 1)

    def test_failed_config_validation_does_not_switch_or_restart(self):
        old = self.h.success()
        calls = (self.h.base / "service-calls").read_text().count("restart ")
        (self.h.source / "trading/capacity_relay_server.py").write_text("# Changed implementation\n")
        self.assertNotEqual(self.h.run(fail="validation").returncode, 0)
        self.assertEqual(self.h.current, old)
        self.assertEqual((self.h.base / "service-calls").read_text().count("restart "), calls)


if __name__ == "__main__":
    unittest.main()
