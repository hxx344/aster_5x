"""Exercise repeatable Linux installs with isolated paths, fake runtimes and services."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class InstallerHarness:
    def __init__(self, test):
        self.test = test
        temporary = tempfile.TemporaryDirectory()
        test.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.source = self.base / "source"
        self.commands = self.base / "commands"
        self.root = self.base / "opt"
        self.etc = self.base / "etc"
        self.runtime = self.base / "runtime"
        self.counters = self.base / "counters"
        for path in (self.source, self.commands, self.etc, self.runtime, self.counters):
            path.mkdir()
        self.old = self.root / "releases" / "old"
        self.old.mkdir(parents=True)
        (self.root / "current").symlink_to(self.old)
        self.environment = "ASTER_ALLOW_LIVE=0\nASTER_A_PRIVATE_KEY=test-preserved-only\n"
        (self.etc / "environment").write_text(self.environment)
        (self.runtime / "trading.sqlite3").write_bytes(b"preserved database fixture")
        self.unit = self.base / "service"
        self.unit.write_text("old unit")
        (self.base / "service-state").write_text("active")
        self.node_version = self.base / "node-version"
        self.npm_version = self.base / "npm-version"
        self.node_version.write_text("v22.23.2\n")
        self.npm_version.write_text("10.9.2\n")
        (self.base / "remote-revision").write_text("a" * 40)
        self.node = self.root / "node-v22.23.2" / "bin"
        self.node.mkdir(parents=True)
        for name in ("trading", "dashboard", "dashboard/app", "dashboard/lib", "deploy"):
            (self.source / name).mkdir()
        self.write("trading/server.py", "def create_app(): pass\n")
        self.write("trading/cycle-config.json", (ROOT / "trading/cycle-config.json").read_text())
        self.write("dashboard/app/page.tsx", "initial frontend\n")
        self.write("dashboard/lib/helper.ts", "initial helper\n")
        self.write("dashboard/tsconfig.json", '{"compilerOptions":{"incremental":true}}\n')
        self.write("dashboard/package.json", '{"name":"installer-fixture","version":"1.0.0","private":true}\n')
        self.write("dashboard/package-lock.json", '{"name":"installer-fixture","lockfileVersion":3,"packages":{}}\n')
        for name in ("requirements.txt", "requirements.lock", "monitor.py", "config.json", "DEPLOYMENT.md"):
            self.write(name, "")
        for name in ("aster-desk.service", "aster-desk", "trading.env.example", "build-dashboard.py"):
            self.write("deploy/" + name, (ROOT / "deploy" / name).read_text())
        script = (ROOT / "install-trading.sh").read_text()
        mappings = {"/opt/aster-desk": str(self.root), "/etc/aster-desk": str(self.etc),
                    "/var/lib/aster-desk": str(self.runtime), "/etc/systemd/system/aster-desk.service": str(self.unit),
                    "/usr/local/bin/aster-desk": str(self.base / "cli"), "/run/systemd/system": str(self.base),
                    "/run/lock/aster-desk-install.lock": str(self.base / "install.lock"),
                    '"/proc/$pid/cwd"': '"' + str(self.base / 'proc') + '/$pid/cwd"'}
        for before, after in mappings.items():
            script = script.replace(before, after)
        self.write("install-trading.sh", script)
        self.install_commands()

    def write(self, path, text):
        (self.source / path).write_text(text)

    def append(self, path, text):
        with (self.source / path).open("a") as stream:
            stream.write(text)

    def executable(self, path, body):
        path.write_text("#!/bin/bash\nset -e\n" + body + "\n")
        path.chmod(0o755)

    def install_commands(self):
        for name in ("chown", "id", "useradd", "sleep"):
            self.executable(self.commands / name, "exit 0")
        self.executable(self.commands / "journalctl", 'printf "fixture service diagnostics\\n"')
        self.executable(self.commands / "systemd-run", 'printf "%s\\n" "$HARNESS_BASE/runtime"')
        self.executable(self.commands / "df", '''if [[ -f "$HARNESS_BASE/fail-space" ]]; then
  printf 'Filesystem Blocks Used Available Capacity Mounted\\nfixture 100 100 0 100%% /\\n'
elif [[ -f "$HARNESS_BASE/fail-inodes" && $1 == -Pi ]]; then
  printf 'Filesystem Inodes Used Available Capacity Mounted\\nfixture 100 100 0 100%% /\\n'
else
  exec /bin/df "$@"
fi''')
        for name in ("apt-get", "dnf"):
            self.executable(self.commands / name, 'printf "1\\n" >> "$HARNESS_COUNTERS/apt"')
        # An incompatible command earlier on PATH forces discovery of the existing
        # pinned runtime, instead of accidentally relying on the host's Node.
        self.executable(self.commands / "node", 'exit 1')
        self.executable(self.commands / "npm", 'exit 1')
        self.executable(self.node / "node", '''case "${1:-}" in
--version|-v) cat "$HARNESS_NODE_VERSION" ;;
-e) exit 0 ;;
-p|--print) printf '{"version":"%s","abi":"127","platform":"linux","arch":"x64"}\\n' "$(cat "$HARNESS_NODE_VERSION")" ;;
*) exit 0 ;;
esac''')
        self.executable(self.commands / "python3", '''if [[ ${1:-} == -m && ${2:-} == venv ]]; then
  printf '1\\n' >> "$HARNESS_COUNTERS/venv"
  mkdir -p "$3/bin"
  printf '%s\\n' "$3" > "$3/created-path"
  cp "$HARNESS_COMMANDS/venv-python" "$3/bin/python"
  cp "$HARNESS_COMMANDS/pip" "$3/bin/pip"
else
  exec "$HARNESS_REAL_PYTHON" "$@"
fi''')
        self.executable(self.commands / "venv-python", '''if [[ ${1:-} == -m && ${2:-} == pip ]]; then
  shift 2
  exec "$HARNESS_COMMANDS/pip" "$@"
fi
if [[ ${1:-} == -c && ${2:-} == 'import fastapi,'* ]]; then exit 0; fi
exec "$HARNESS_REAL_PYTHON" "$@"''')
        self.executable(self.commands / "pip", '''if [[ ${1:-} == install ]]; then
  printf '1\\n' >> "$HARNESS_COUNTERS/pip"
  [[ ! -f "$HARNESS_BASE/fail-pip" ]] || exit 47
fi''')
        self.executable(self.node / "npm", '''case "${1:-}" in
--version|-v) cat "$HARNESS_NPM_VERSION" ;;
ci)
  printf '1\\n' >> "$HARNESS_COUNTERS/npm-ci"
  mkdir -p node_modules
  printf 'dependency install %s\\n' "$(wc -l < "$HARNESS_COUNTERS/npm-ci")" > node_modules/install-token
  [[ ! -f "$HARNESS_BASE/fail-npm-ci" ]] || exit 46
  ;;
run)
  printf '1\\n' >> "$HARNESS_COUNTERS/npm-${2:-unknown}"
  if [[ ${2:-} == typecheck ]]; then
    if [[ -f tsconfig.tsbuildinfo ]]; then printf '1\\n' >> "$HARNESS_COUNTERS/ts-warm"; fi
    printf '{"version":"fixture"}\\n' > tsconfig.tsbuildinfo
    [[ ! -f "$HARNESS_BASE/fail-typecheck" ]] || exit 50
  fi
  if [[ ${2:-} == lint ]]; then [[ ! -f "$HARNESS_BASE/fail-lint" ]] || exit 51; fi
  if [[ ${2:-} == build ]]; then
    [[ -f node_modules/install-token ]] || exit 45
    [[ ! -L node_modules ]] || exit 48
    [[ $PWD == "$HARNESS_BASE"/opt/cache/npm-deps-*/dashboard ]] || exit 49
    mkdir -p node_modules/.vite-rsc-temp/rsc
    printf 'temporary build output\\n' > node_modules/.vite-rsc-temp/rsc/output.js
    if [[ -f "$HARNESS_BASE/mutate-package" ]]; then printf 'modified by build\\n' >> node_modules/install-token; fi
    mkdir -p dist/client
    printf '<html>' > dist/client/index.html
    cat app/page.tsx lib/helper.ts >> dist/client/index.html
    printf '</html>' >> dist/client/index.html
    [[ ! -f "$HARNESS_BASE/fail-build" ]] || exit 44
  fi
  ;;
*) exit 43 ;;
esac''')
        self.executable(self.commands / "curl", '''case "$*" in
*nodejs.org*) printf '1\\n' >> "$HARNESS_COUNTERS/node-curl"; exit 42 ;;
*api.github.com/repos/hxx344/aster_5x/commits/main*)
  printf '{"sha":"%s"}' "$(cat "$HARNESS_BASE/remote-revision")" ;;
*codeload.github.com/hxx344/aster_5x/tar.gz/*)
  printf '1\\n' >> "$HARNESS_COUNTERS/source-curl"
  while [[ $# -gt 0 ]]; do
    if [[ $1 == -o ]]; then target=$2; break; fi
    shift
  done
  tar -C "$HARNESS_BASE" -czf "$target" source
  ;;
*127.0.0.1:8765/api/health*)
  printf '1\\n' >> "$HARNESS_COUNTERS/health"
  if [[ -f "$HARNESS_BASE/fail-health" ]] || { [[ -f "$HARNESS_BASE/slow-health" ]] && [[ $(wc -l < "$HARNESS_COUNTERS/health") -le 2 ]]; }; then
    printf "curl: (7) Failed to connect to 127.0.0.1 port 8765: Couldn't connect to server\\n" >&2
    exit 7
  fi
  printf '{"status":"ok"}'
  ;;
*) printf 'Unexpected network request blocked by installer harness\\n' >&2; exit 41 ;;
esac''')
        self.executable(self.commands / "systemctl", '''printf '%s\\n' "$*" >> "$HARNESS_BASE/service-calls"
case "$1" in
is-active) [[ $(cat "$HARNESS_BASE/service-state") == active ]] ;;
is-enabled) exit 0 ;;
show)
  [[ ! -f "$HARNESS_BASE/fail-inspect" ]] || exit 1
  if [[ $* == *MainPID* ]]; then
    if [[ $(cat "$HARNESS_BASE/service-state") == active ]]; then
      mkdir -p "$HARNESS_BASE/proc/123"
      ln -sfn "$(readlink -f "$HARNESS_BASE/opt/current")" "$HARNESS_BASE/proc/123/cwd"
      printf '123\\n'
    else printf '0\\n'; fi
  else printf 'no\\n'; fi
  ;;
stop)
  printf stopped > "$HARNESS_BASE/service-state"
  if [[ -f "$HARNESS_BASE/rollback-pending" ]]; then
    rm "$HARNESS_BASE/rollback-pending"
    for signal in HUP INT TERM; do
      if [[ -f "$HARNESS_BASE/rollback-signal-$signal" ]]; then kill -"$signal" "$PPID"; fi
    done
  fi
  ;;
start|enable)
  printf active > "$HARNESS_BASE/service-state"
  if [[ $1 == enable ]]; then
    for signal in HUP INT TERM; do
      if [[ -f "$HARNESS_BASE/fail-signal-$signal" ]]; then
        touch "$HARNESS_BASE/rollback-pending"
        kill -"$signal" "$PPID"
      fi
    done
  fi
  ;;
list-unit-files) printf 'aster-5x.service enabled\\n' ;;
esac''')

    def run(self, fail=None, args=(), remote=False):
        if fail:
            (self.base / ("fail-" + fail)).touch()
        try:
            command = ["bash", "-s", "--", *args] if remote else ["bash", str(self.source / "install-trading.sh"), *args]
            result = subprocess.run(command, cwd=self.base if remote else self.source,
                                    env={**os.environ, "PATH": str(self.commands) + ":/usr/bin:/bin",
                                         "HARNESS_BASE": str(self.base), "HARNESS_COMMANDS": str(self.commands),
                                         "HARNESS_COUNTERS": str(self.counters), "HARNESS_REAL_PYTHON": sys.executable,
                                         "HARNESS_NODE_VERSION": str(self.node_version), "HARNESS_NPM_VERSION": str(self.npm_version)},
                                    input=(self.source / "install-trading.sh").read_text() if remote else None,
                                    capture_output=True, text=True, timeout=45)
        finally:
            if fail:
                (self.base / ("fail-" + fail)).unlink()
        self.test.assertEqual((self.etc / "environment").read_text(), self.environment)
        self.test.assertEqual((self.runtime / "trading.sqlite3").read_bytes(), b"preserved database fixture")
        return result

    def success(self):
        result = self.run()
        self.test.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)
        self.test.assertEqual((self.etc / "environment").stat().st_mode & 0o777, 0o600)
        return self.current

    @property
    def current(self):
        return (self.root / "current").resolve()

    def count(self, name):
        path = self.counters / name
        return len(path.read_text().splitlines()) if path.exists() else 0

    def assert_counts(self, *, pip, npm_ci, build):
        self.test.assertEqual({name: self.count(name) for name in ("apt", "node-curl", "pip", "npm-ci", "npm-build")},
                              {"apt": 0, "node-curl": 0, "pip": pip, "npm-ci": npm_ci, "npm-build": build})

    def cache_modules(self):
        return {path: path.read_bytes() for path in self.root.rglob("install-token") if "releases" not in path.parts}

    def assert_stable_venv(self, release):
        link = release / ".venv"
        self.test.assertTrue(link.is_symlink(), "release must reference a completed dependency environment")
        environment = link.resolve()
        self.test.assertEqual((environment / "created-path").read_text().strip(), str(environment),
                              "a venv cannot be renamed after creation: its installed scripts retain absolute paths")
        self.test.assertTrue((environment / "bin/python").is_file())
        return environment


@unittest.skipUnless(sys.platform == "linux" and os.geteuid() == 0, "Linux root required for isolated installer harness")
class TradingInstallerTests(unittest.TestCase):
    def setUp(self):
        self.h = InstallerHarness(self)

    def test_success_and_warm_upgrade_reuse_runtimes_dependencies_and_frontend(self):
        first = self.h.success()
        self.assertNotEqual(first, self.h.old)
        self.assertIn("disable --now aster-5x", (self.h.base / "service-calls").read_text())
        self.assertNotEqual(self.h.unit.read_text(), "old unit")
        self.h.assert_counts(pip=1, npm_ci=1, build=1)
        environment = self.h.assert_stable_venv(first)
        before_calls = (self.h.base / "service-calls").read_text().splitlines()
        second = self.h.success()
        self.assertEqual(first, second, "an unchanged install must not create another release")
        after_calls = (self.h.base / "service-calls").read_text().splitlines()[len(before_calls):]
        self.assertFalse(any(line.split()[0] in {"start", "stop", "restart", "enable"} for line in after_calls))
        self.h.assert_counts(pip=1, npm_ci=1, build=1)
        self.assertEqual(self.h.assert_stable_venv(second), environment)
        self.assertEqual((first / "dashboard/dist/client/index.html").read_bytes(),
                         (second / "dashboard/dist/client/index.html").read_bytes())
        # The real installation lives below traversable /opt. Make only this
        # temporary ancestor traversable before testing the runtime as nobody.
        self.h.base.chmod(0o755)
        result = subprocess.run([sys.executable, "-c", "from pathlib import Path; import sys; print(Path(sys.argv[1]).read_text())",
                                 str(environment / "created-path")], cwd="/tmp", user=65534, group=65534, extra_groups=[],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(environment))

    def test_backend_frontend_and_javascript_dependency_changes_invalidate_only_needed_work(self):
        self.h.success()
        self.h.append("trading/server.py", "# backend-only change\n")
        self.h.success()
        self.h.assert_counts(pip=1, npm_ci=1, build=1)
        modules = self.h.cache_modules()
        self.assertTrue(modules, "npm dependencies must have a reusable cache outside releases")
        self.h.append("dashboard/lib/helper.ts", "frontend helper changed\n")
        frontend = self.h.success()
        self.h.assert_counts(pip=1, npm_ci=1, build=2)
        self.assertIn("frontend helper changed", (frontend / "dashboard/dist/client/index.html").read_text())
        self.assertEqual(self.h.cache_modules(), modules, "builds must not mutate the shared npm cache")
        self.assertFalse((frontend / "dashboard/node_modules").exists(), "production releases need only static assets")
        self.h.append("dashboard/package-lock.json", "\n")
        self.h.success()
        self.h.assert_counts(pip=1, npm_ci=2, build=3)
        self.h.append("dashboard/package.json", "\n")
        self.h.success()
        self.h.assert_counts(pip=1, npm_ci=3, build=4)

    def test_shared_cycle_configuration_rebuilds_frontend_without_reinstalling_dependencies(self):
        self.h.success()
        self.h.append("trading/cycle-config.json", "\n")
        self.h.success()
        self.h.assert_counts(pip=1, npm_ci=1, build=2)
        self.h.success()
        self.h.assert_counts(pip=1, npm_ci=1, build=2)

    def test_python_lock_change_rebuilds_only_dependency_environment_at_final_creation_path(self):
        previous = self.h.success()
        old_environment = self.h.assert_stable_venv(previous)
        self.h.append("requirements.lock", "# new dependency resolution\n")
        current = self.h.success()
        self.h.assert_counts(pip=2, npm_ci=1, build=1)
        self.assertNotEqual(self.h.assert_stable_venv(current), old_environment)
        self.assertTrue((old_environment / "bin/python").is_file())
        self.h.success()
        self.h.assert_counts(pip=2, npm_ci=1, build=1)

    def test_node_and_npm_version_changes_invalidate_javascript_caches(self):
        self.h.success()
        self.h.node_version.write_text("v22.23.3\n")
        self.h.success()
        self.h.assert_counts(pip=1, npm_ci=2, build=2)
        self.h.npm_version.write_text("10.9.3\n")
        self.h.success()
        self.h.assert_counts(pip=1, npm_ci=3, build=3)

    def test_failed_build_does_not_switch_or_publish_reusable_frontend(self):
        previous = self.h.success()
        environment = self.h.assert_stable_venv(previous)
        modules = self.h.cache_modules()
        self.h.append("dashboard/app/page.tsx", "build to retry\n")
        result = self.h.run(fail="build")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.h.current, previous)
        self.assertTrue((environment / "bin/python").is_file())
        self.assertTrue(modules)
        self.assertFalse(self.h.cache_modules(), "failed build workspace must be reclaimed, never reused")
        self.h.assert_counts(pip=1, npm_ci=1, build=2)
        current = self.h.success()
        self.assertNotEqual(current, previous)
        self.h.assert_counts(pip=1, npm_ci=2, build=3)
        self.assertIn("build to retry", (current / "dashboard/dist/client/index.html").read_text())

    def test_typescript_cache_survives_source_changes_but_not_config_changes(self):
        self.h.success()
        self.assertEqual(self.h.count("ts-warm"), 0)
        modules = self.h.cache_modules()
        self.h.append("dashboard/lib/helper.ts", "source edit\n")
        result = self.h.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.h.count("ts-warm"), 1)
        self.assertEqual(self.h.cache_modules(), modules)
        for message in ("no dependency copy", "Reuse TypeScript incremental cache", "Typecheck dashboard:",
                        "Lint dashboard:", "Build dashboard assets:"):
            self.assertIn(message, result.stdout)
        self.h.append("dashboard/tsconfig.json", "\n")
        self.h.success()
        self.assertEqual(self.h.count("ts-warm"), 1)
        self.h.assert_counts(pip=1, npm_ci=1, build=3)

    def test_changed_package_invalidates_workspace_without_affecting_current_assets(self):
        self.h.success()
        self.h.append("dashboard/lib/helper.ts", "source edit\n")
        marker = self.h.base / "mutate-package"
        marker.touch()
        result = self.h.run()
        marker.unlink()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("discard workspace cache", result.stdout)
        self.assertFalse(self.h.cache_modules())
        previous = self.h.current
        self.assertTrue((previous / "dashboard/dist/client/index.html").is_file())
        self.h.append("dashboard/lib/helper.ts", "next source edit\n")
        self.h.success()
        self.h.assert_counts(pip=1, npm_ci=2, build=3)

    def test_typecheck_or_lint_failure_stops_before_build_and_service_switch(self):
        previous = self.h.success()
        for check in ("typecheck", "lint"):
            self.h.append("dashboard/lib/helper.ts", check + " edit\n")
            result = self.h.run(fail=check)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self.h.current, previous)
            self.assertEqual(self.h.count("npm-build"), 1)
            self.assertIn("FAILED after", result.stdout)

    def test_failed_dependency_installs_are_not_reused(self):
        previous = self.h.success()
        self.h.append("requirements.lock", "# changed python dependencies\n")
        result = self.h.run(fail="pip")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.h.current, previous)
        self.h.success()
        self.h.assert_counts(pip=3, npm_ci=1, build=1)
        previous = self.h.current
        self.h.append("dashboard/package-lock.json", "\n")
        result = self.h.run(fail="npm-ci")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.h.current, previous)
        self.h.success()
        self.h.assert_counts(pip=3, npm_ci=3, build=2)

    def test_failed_health_restores_previous_service_without_disabling_monitor(self):
        result = self.h.run(fail="health")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.h.current, self.h.old)
        self.assertEqual(self.h.unit.read_text(), "old unit")
        self.assertEqual((self.h.base / "service-state").read_text(), "active")
        self.assertNotIn("disable --now aster-5x", (self.h.base / "service-calls").read_text())
        self.assertEqual(self.h.count("health"), 30)
        self.assertIn("Service health check failed after 30 attempts", result.stderr)
        self.assertIn("fixture service diagnostics", result.stdout)
        self.assertNotIn("Couldn't connect", result.stderr)
        self.assertNotIn("Aster Desk installed", result.stdout)

    def test_slow_http_start_retries_quietly_and_finishes_upgrade(self):
        (self.h.base / "slow-health").touch()
        result = self.h.run()
        self.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)
        self.assertEqual(self.h.count("health"), 3)
        self.assertEqual(result.stderr, "")
        self.assertIn("Aster Desk installed", result.stdout)
        self.assertNotIn("fixture service diagnostics", result.stdout)
        self.assertNotEqual(self.h.current, self.h.old)

    def test_termination_during_switch_restores_previous_service(self):
        for signal, status in (("HUP", 129), ("INT", 130), ("TERM", 143)):
            with self.subTest(signal=signal):
                result = self.h.run(fail="signal-" + signal)
                self.assertEqual(self.h.current, self.h.old)
                self.assertEqual(result.returncode, status)
                self.assertEqual(self.h.unit.read_text(), "old unit")
                self.assertEqual((self.h.base / "service-state").read_text(), "active")
                self.assertNotIn("disable --now aster-5x", (self.h.base / "service-calls").read_text())

    def test_additional_signal_during_rollback_preserves_service_and_original_exit_status(self):
        for initial, status in (("HUP", 129), ("INT", 130), ("TERM", 143)):
            for additional in ("HUP", "INT", "TERM"):
                with self.subTest(initial=initial, additional=additional):
                    harness = InstallerHarness(self)
                    marker = harness.base / ("rollback-signal-" + additional)
                    marker.touch()
                    try:
                        result = harness.run(fail="signal-" + initial)
                    finally:
                        marker.unlink()
                    self.assertEqual(result.returncode, status)
                    self.assertEqual(harness.current, harness.old)
                    self.assertEqual(harness.unit.read_text(), "old unit")
                    self.assertEqual((harness.base / "service-state").read_text(), "active")
                    self.assertNotIn("disable --now aster-5x", (harness.base / "service-calls").read_text())

    def test_failed_health_after_dependency_upgrade_preserves_previous_runtime(self):
        previous = self.h.success()
        environment = self.h.assert_stable_venv(previous)
        previous_unit = self.h.unit.read_bytes()
        self.h.append("requirements.lock", "# new environment should not replace the old runtime\n")
        self.h.append("deploy/aster-desk.service", "\n# upgraded unit\n")
        result = self.h.run(fail="health")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.h.current, previous)
        self.assertEqual(self.h.unit.read_bytes(), previous_unit)
        self.assertEqual(self.h.assert_stable_venv(previous), environment)
        self.assertEqual((self.h.base / "service-state").read_text(), "active")

    def test_missing_completed_cache_marker_rebuilds_without_relocating_old_runtime(self):
        previous = self.h.success()
        environment = self.h.assert_stable_venv(previous)
        (environment / ".complete").unlink()
        current = self.h.success()
        self.h.assert_counts(pip=2, npm_ci=1, build=1)
        self.assertNotEqual(self.h.assert_stable_venv(current), environment)
        self.assertEqual((previous / ".venv").resolve(), environment)
        self.assertTrue((environment / "bin/python").is_file())
        self.assertEqual((environment / "created-path").read_text().strip(), str(environment))

    def test_concurrent_upgrade_lock_rejects_second_install_before_release_or_service_changes(self):
        import fcntl

        before = set((self.h.root / "releases").iterdir())
        with (self.h.root / "upgrade.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.h.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("upgrade", result.stderr.lower())
        self.assertEqual(set((self.h.root / "releases").iterdir()), before)
        self.assertEqual(self.h.current, self.h.old)
        self.assertFalse((self.h.base / "service-calls").exists())
        self.h.assert_counts(pip=0, npm_ci=0, build=0)

    def test_upgrade_keeps_current_and_one_successful_rollback_with_referenced_environments(self):
        first = self.h.success()
        first_env = self.h.assert_stable_venv(first)
        self.h.append("requirements.lock", "# second runtime\n")
        second = self.h.success()
        second_env = self.h.assert_stable_venv(second)
        self.h.append("requirements.lock", "# third runtime\n")
        third = self.h.success()
        self.assertEqual(set(self.h.root.joinpath("releases").glob("release-*")), {second, third})
        # Explicit pressure cleanup drops unused dependency caches, never the
        # rollback's environment or the currently running environment.
        result = self.h.run(args=("--cleanup",))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(first_env.exists())
        self.assertTrue(second_env.exists())
        self.assertTrue(self.h.assert_stable_venv(third).exists())

    def test_failed_build_and_failed_health_do_not_leave_managed_release_directories(self):
        previous = self.h.success()
        before = set(self.h.root.joinpath("releases").iterdir())
        for failure in ("build", "health"):
            with self.subTest(failure=failure):
                self.h.append("dashboard/app/page.tsx", "updated frontend\n")
                result = self.h.run(fail=failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.h.current, previous)
                self.assertEqual(set(self.h.root.joinpath("releases").iterdir()), before)

    def test_cleanup_does_not_build_or_restart_and_retains_unknown_files(self):
        previous = self.h.success()
        unknown = self.h.root / "releases" / "user-backup"
        unknown.mkdir()
        sentinel = unknown / "keep"
        sentinel.write_text("untouched")
        calls = (self.h.base / "service-calls").read_text().splitlines()
        result = self.h.run(args=("--cleanup",))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.h.current, previous)
        self.assertEqual(sentinel.read_text(), "untouched")
        self.h.assert_counts(pip=1, npm_ci=1, build=1)
        new_calls = (self.h.base / "service-calls").read_text().splitlines()[len(calls):]
        self.assertFalse(any(line.split()[0] in {"start", "stop", "restart", "enable"} for line in new_calls))

    def test_space_inode_and_service_inspection_failures_stop_before_build_or_switch(self):
        previous = self.h.success()
        self.h.append("trading/server.py", "# require an upgrade\n")
        before = set(self.h.root.joinpath("releases").iterdir())
        for failure in ("space", "inodes", "inspect"):
            with self.subTest(failure=failure):
                result = self.h.run(fail=failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.h.current, previous)
                self.assertEqual(set(self.h.root.joinpath("releases").iterdir()), before)
                self.assertEqual((self.h.base / "service-state").read_text(), "active")
                self.h.assert_counts(pip=1, npm_ci=1, build=1)

    def test_docs_only_remote_revision_is_remembered_without_release_or_repeat_download(self):
        first = self.h.run(remote=True)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        previous = self.h.current
        self.h.write("README.md", "documentation-only update\n")
        revision = "b" * 40
        (self.h.base / "remote-revision").write_text(revision)
        second = self.h.run(remote=True)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self.h.current, previous)
        self.assertEqual((previous / ".install-revision").read_text().strip(), revision)
        self.assertEqual(self.h.count("source-curl"), 2)
        third = self.h.run(remote=True)
        self.assertEqual(third.returncode, 0, third.stdout + third.stderr)
        self.assertEqual(self.h.current, previous)
        self.assertEqual(self.h.count("source-curl"), 2)
        self.assertIn("skipping archive download", third.stdout)
        self.h.assert_counts(pip=1, npm_ci=1, build=1)
