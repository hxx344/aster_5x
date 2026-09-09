"""Exercise installer switch/rollback in a temporary filesystem with fake services."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "linux" and os.geteuid() == 0, "Linux root required for isolated installer harness")
class TradingInstallerTests(unittest.TestCase):
    def run_install(self, fail=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        source, commands = base / "source", base / "commands"
        source.mkdir()
        commands.mkdir()
        install_root, etc, state = base / "opt", base / "etc", base / "state"
        old = install_root / "releases" / "old"
        old.mkdir(parents=True)
        (install_root / "current").symlink_to(old)
        etc.mkdir()
        (etc / "environment").write_text("ASTER_ALLOW_LIVE=0\nASTER_A_PRIVATE_KEY=test-preserved-only\n")
        unit = base / "service"
        unit.write_text("old unit")
        state.write_text("active")
        for name in ("trading", "dashboard", "deploy"):
            (source / name).mkdir()
        (source / "trading" / "server.py").write_text("def create_app(): pass\n")
        for name in ("requirements.txt", "requirements.lock", "monitor.py", "config.json", "DEPLOYMENT.md", "dashboard/package-lock.json"):
            (source / name).write_text("")
        for name in ("aster-desk.service", "aster-desk", "trading.env.example"):
            (source / "deploy" / name).write_text((ROOT / "deploy" / name).read_text())
        script = (ROOT / "install-trading.sh").read_text()
        mappings = {"/opt/aster-desk": str(install_root), "/etc/aster-desk": str(etc),
                    "/var/lib/aster-desk": str(base / "runtime"), "/etc/systemd/system/aster-desk.service": str(unit),
                    "/usr/local/bin/aster-desk": str(base / "cli"), "/run/systemd/system": str(base)}
        for before, after in mappings.items():
            script = script.replace(before, after)
        (source / "install-trading.sh").write_text(script)

        def executable(name, body):
            path = commands / name
            path.write_text("#!/bin/bash\nset -e\n" + body + "\n")
            path.chmod(0o755)

        for name in ("apt-get", "dnf", "chown", "id", "useradd", "node", "sleep", "journalctl"):
            executable(name, "exit 0")
        executable("python3", f'''if [[ ${{1:-}} == -m && ${{2:-}} == venv ]]; then
mkdir -p "$3/bin"
ln -s '{sys.executable}' "$3/bin/python"
printf '#!/bin/bash\\nexit 0\\n' > "$3/bin/pip"
chmod +x "$3/bin/pip"
else exec '{sys.executable}' "$@"; fi''')
        executable("npm", 'mkdir -p dist/client; printf "<html>test</html>" > dist/client/index.html')
        executable("curl", "exit 22" if fail else '''printf '{"status":"ok"}' ''')
        executable("systemctl", f'''printf '%s\\n' "$*" >> '{base / "calls"}'
case "$1" in
is-active) [[ $(cat '{state}') == active ]] ;;
stop) printf stopped > '{state}' ;;
start|enable) printf active > '{state}' ;;
list-unit-files) printf 'aster-5x.service enabled\\n' ;;
esac''')
        result = subprocess.run(["bash", str(source / "install-trading.sh")], cwd=source,
                                env={**os.environ, "PATH": str(commands) + ":/usr/bin:/bin"}, capture_output=True, text=True)
        self.assertEqual((etc / "environment").read_text(), "ASTER_ALLOW_LIVE=0\nASTER_A_PRIVATE_KEY=test-preserved-only\n")
        self.assertEqual((etc / "environment").stat().st_mode & 0o777, 0o600)
        return result, install_root, old, unit, base

    def test_success_switches_release_and_preserves_credentials(self):
        result, root, old, unit, base = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual((root / "current").resolve(), old)
        self.assertIn("disable --now aster-5x", (base / "calls").read_text())
        self.assertNotEqual(unit.read_text(), "old unit")

    def test_failed_health_restores_previous_service_without_disabling_monitor(self):
        result, root, old, unit, base = self.run_install(fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((root / "current").resolve(), old)
        self.assertEqual(unit.read_text(), "old unit")
        self.assertEqual((base / "state").read_text(), "active")
        self.assertNotIn("disable --now aster-5x", (base / "calls").read_text())
