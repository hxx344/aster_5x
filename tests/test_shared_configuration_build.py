"""Shared runtime metadata participates in the installer's frontend cache key."""
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "install-trading.sh").read_text(encoding="utf-8")
FINGERPRINTS = SCRIPT.split('fingerprints=$(python3 - "$source_dir" "$node_identity" "$npm_identity" <<\'PY\'\n', 1)[1].split('\nPY\n)', 1)[0]


class SharedConfigurationBuildTests(unittest.TestCase):
    def test_shared_metadata_invalidates_only_build_and_release_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            files = ["requirements.txt", "requirements.lock", "monitor.py", "config.json", "DEPLOYMENT.md", "install-trading.sh",
                     "dashboard/package.json", "dashboard/package-lock.json", "dashboard/lib/cycle.ts", "trading/cycle-config.json"]
            for name in files:
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}", encoding="utf-8")
            def keys():
                output = io.StringIO()
                with patch("sys.argv", ["fingerprint", str(source), "node-fixture", "npm-fixture"]), redirect_stdout(output):
                    exec(compile(FINGERPRINTS, "installer-fingerprint", "exec"), {})
                return output.getvalue().splitlines()
            before = keys()
            (source / "trading/cycle-config.json").write_text('{"changed":true}', encoding="utf-8")
            after = keys()
            self.assertEqual(len(after), 4)
            self.assertEqual(before[:2], after[:2])
            self.assertNotEqual(before[2], after[2])
            self.assertNotEqual(before[3], after[3])
            self.assertEqual(after, keys())
