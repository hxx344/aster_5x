"""CI package upgrades use the actual installer and the existing isolated harness."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from tests.test_trading_install import InstallerHarness, ROOT

spec = importlib.util.spec_from_file_location("ci_package", ROOT / "deploy/package-release.py")
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


@unittest.skipUnless(sys.platform == "linux" and os.geteuid() == 0, "Linux root required for isolated installer fixture")
class CiInstallerTests(unittest.TestCase):
    def setUp(self):
        self.h = InstallerHarness(self)
        self.h.write("requirements.lock", "# fixture has no external dependencies\n")
        curl = self.h.commands / "curl"
        script = curl.read_text()
        script = script.replace('case "$*" in', '''case "$*" in
*github.com/hxx344/aster_5x/releases/*)
  url='' target=''
  for value in "$@"; do [[ $value != https://* ]] || url=$value; done
  while [[ $# -gt 0 ]]; do if [[ $1 == -o ]]; then target=$2; break; fi; shift; done
  name=${url##*/}
  [[ $name == release-manifest.json ]] || printf '1\\n' >> "$HARNESS_COUNTERS/ci-archive"
  cp "$HARNESS_BASE/ci-release/$name" "$target"
  ;;''', 1)
        curl.write_text(script)

    def release(self):
        source = self.h.source
        if not (source / ".git").exists():
            for arguments in (("init", "-q"), ("config", "user.email", "ci@example.invalid"), ("config", "user.name", "CI fixture")):
                subprocess.run(["git", "-C", str(source), *arguments], check=True, capture_output=True)
            public = source / "dashboard/dist/client"
            public.mkdir(parents=True)
            (public / "index.html").write_text("<html>Prebuilt CI dashboard</html>")
        subprocess.run(["git", "-C", str(source), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(source), "commit", "--allow-empty", "-qm", "CI runtime"], check=True, capture_output=True)
        commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"]).decode().strip()
        return package.build_release(source, commit, self.h.base / "ci-release")

    def ci(self, **arguments):
        return self.h.run(mode=None, **arguments)

    def test_source_to_ci_reuses_python_and_never_installs_or_builds_node(self):
        original = self.h.success()
        environment = (original / ".venv").resolve()
        manifest = self.release()
        result = self.ci()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotEqual(self.h.current, original)
        self.assertEqual((self.h.current / ".venv").resolve(), environment)
        self.assertEqual((self.h.current / ".release-commit").read_text().strip(), manifest["commit"])
        self.h.assert_counts(pip=1, npm_ci=1, build=1)
        current = self.h.current
        calls = (self.h.base / "service-calls").read_text()
        repeated = self.ci()
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual(self.h.current, current)
        self.assertEqual(self.h.count("ci-archive"), 1)
        new_calls = (self.h.base / "service-calls").read_text()[len(calls):]
        self.assertNotIn("stop aster-desk", new_calls)
        self.h.write("README.md", "documentation only\n")
        self.release()
        documented = self.ci()
        self.assertEqual(documented.returncode, 0, documented.stdout + documented.stderr)
        self.assertEqual(self.h.current, current)
        self.assertEqual(self.h.count("ci-archive"), 1)

    def test_corrupt_download_and_failed_activation_preserve_current_and_data(self):
        original = self.h.success()
        manifest = self.release()
        archive = self.h.base / "ci-release" / manifest["artifacts"]["linux-x64"]["file"]
        good = archive.read_bytes()
        archive.write_bytes(b"corrupt deployment archive")
        rejected = self.ci()
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(self.h.current, original)
        archive.write_bytes(good)
        failed = self.ci(fail="health")
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self.h.current, original)
        self.assertEqual((self.h.base / "service-state").read_text(), "active")
        self.h.assert_counts(pip=1, npm_ci=1, build=1)


if __name__ == "__main__":
    unittest.main()
