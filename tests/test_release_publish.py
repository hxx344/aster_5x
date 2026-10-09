"""Release ordering checks use no GitHub network or credentials."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_publish", ROOT / "deploy/publish-release.py")
publish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publish)


class ReleaseOrderTests(unittest.TestCase):
    def response(self, value=None, code=0, error=""):
        return SimpleNamespace(returncode=code, stdout=json.dumps(value), stderr=error)

    def test_first_passing_release_can_be_promoted(self):
        with patch.object(publish, "gh", return_value=self.response(code=1, error="HTTP 404")):
            self.assertTrue(publish.should_promote_latest("owner/repo", "b" * 40))

    def test_only_descendants_can_move_latest_forward(self):
        for status, expected in (("ahead", True), ("identical", True), ("behind", False), ("diverged", False)):
            with self.subTest(status=status), patch.object(publish, "gh", side_effect=[
                self.response({"tag_name": "deploy-" + "a" * 40}), self.response({"status": status})
            ]) as mocked:
                self.assertEqual(publish.should_promote_latest("owner/repo", "b" * 40), expected)
                self.assertEqual(mocked.call_args.args, ("api", "repos/owner/repo/compare/" + "a" * 40 + "..." + "b" * 40))

    def test_api_failure_and_foreign_release_fail_closed(self):
        for response in (self.response(code=1, error="HTTP 503"), self.response({"tag_name": "other-channel"})):
            with self.subTest(response=response), patch.object(publish, "gh", return_value=response):
                with self.assertRaises((ValueError, RuntimeError)):
                    publish.should_promote_latest("owner/repo", "b" * 40)


if __name__ == "__main__":
    unittest.main()
