"""Native checks for the exact archive and content keys published by CI."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_package", ROOT / "deploy/package-release.py")
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


class ReleasePackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="runtime-release-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source, self.output = self.root / "source", self.root / "output"
        self.source.mkdir()
        for name in {*package.REQUIRED_FILES, "README.md", "DEPLOYMENT.md", "pyproject.toml", "uv.lock"}:
            self.write(name, "fixture: " + name)
        self.write("variational_grid/cli.py", "fixture engine")
        self.write("variational_grid/web/app.js", "fixture web")
        self.git("init", "-q")
        self.git("config", "user.email", "release@example.invalid")
        self.git("config", "user.name", "Release fixture")
        self.commit = self.save()

    def git(self, *arguments):
        return subprocess.check_output(["git", "-C", str(self.source), *arguments], stderr=subprocess.STDOUT).decode().strip()

    def write(self, name, content):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def save(self):
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        return self.git("rev-parse", "HEAD")

    def build(self, commit=None):
        return package.build_release(self.source, commit or self.commit, self.output)

    def test_manifest_and_archive_have_exact_provenance_without_environment(self):
        self.write(".env.secret", "must not enter a runtime package")
        self.write("node_modules/private.txt", "not a runtime input")
        manifest = self.build()
        self.assertEqual(manifest["tag"], "deploy-" + self.commit)
        self.assertEqual(manifest["artifacts"]["linux-x64"], manifest["artifacts"]["linux-arm64"])
        item = manifest["artifacts"]["linux-x64"]
        path = self.output / item["file"]
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), item["sha256"])
        with tarfile.open(path) as archive:
            self.assertTrue(all(entry.isfile() for entry in archive.getmembers()))
            self.assertNotIn(".env.secret", archive.getnames())
            self.assertNotIn("node_modules/private.txt", archive.getnames())
            self.assertEqual(archive.extractfile(".release-commit").read().decode().strip(), self.commit)
            self.assertEqual(archive.extractfile(".release-application-key").read().decode().strip(), item["application_key"])
        self.assertEqual(self.build(), manifest, "Identical input must produce identical archive bytes")

    def test_docs_commit_keeps_application_key_but_runtime_change_invalidates_it(self):
        original = self.build()["artifacts"]["linux-x64"]["application_key"]
        self.write("README.md", "updated documentation")
        self.write("DEPLOYMENT.md", "updated installation help")
        docs = self.save()
        self.assertEqual(self.build(docs)["artifacts"]["linux-x64"]["application_key"], original)
        self.write(package.REQUIRED_FILES[0], "changed runtime")
        runtime = self.save()
        self.assertNotEqual(self.build(runtime)["artifacts"]["linux-x64"]["application_key"], original)

    def test_variational_web_change_preserves_the_engine_tree(self):
        if not package.REPOSITORY.endswith("/variational-grid"):
            self.skipTest("Independent engine/web input trees belong to Variational")
        before = package.inputs(self.source, self.commit)
        self.write("variational_grid/web/app.js", "changed web")
        after = package.inputs(self.source, self.save())
        self.assertEqual(before["engine_tree"], after["engine_tree"])
        self.assertNotEqual(before["web_tree"], after["web_tree"])

    def test_greeks_dependency_key_matches_the_existing_source_install_recipe(self):
        if not package.REPOSITORY.endswith("/greeks"):
            self.skipTest("The existing locked environment cache belongs to Greeks")
        expected = hashlib.sha256(package.tree(self.source, self.commit, "pyproject.toml", "uv.lock")).hexdigest()
        self.assertEqual(package.inputs(self.source, self.commit)["dependency_tree"], expected)


if __name__ == "__main__":
    unittest.main()
