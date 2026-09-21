"""Native filesystem fixtures for the standalone installer's storage collector."""
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = (Path(__file__).resolve().parents[1] / "install-trading.sh").read_text(encoding="utf-8")
COLLECTOR = SCRIPT.split("# STORAGE_BEGIN:", 1)[1].split("\n", 1)[1].split("# STORAGE_END", 1)[0]


class InstallerStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "opt"
        self.releases = self.root / "releases"
        self.cache = self.root / "cache"
        self.releases.mkdir(parents=True)
        self.cache.mkdir()
        self.data = self.base / "data"
        self.data.mkdir()
        self.tick = 1

    def release(self, number, *, ready=True):
        path = self.releases / f"release-20260921T120000Z-{number:06d}"
        path.mkdir()
        (path / ".install-owned").touch()
        if ready:
            marker = path / ".install-ready"
            marker.touch()
            os.utime(marker, ns=(self.tick, self.tick))
            self.tick += 1
        return path

    def artifact(self, kind, number, *, complete=True):
        path = self.cache / f"{kind}-{number:06d}"
        path.mkdir()
        (path / ".install-owned").touch()
        if complete:
            (path / ".complete").touch()
        os.utime(path, ns=(self.tick, self.tick))
        self.tick += 1
        return path

    def link(self, target, path):
        try:
            path.symlink_to(target, target_is_directory=True)
        except OSError as error:
            if getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows account cannot create symlinks; exercised by Linux CI")
            raise

    def collect(self, *, current=None, running=None, source=None, stage=None, previous=None, data=None, pressure=False):
        link = self.root / "current"
        if current:
            if link.is_symlink():
                link.unlink()
            self.link(current, link)
        args = ["storage", self.root, running or "", source or "", stage or "", previous or "", data or self.data, str(int(pressure))]
        with patch.object(sys, "argv", [str(p) for p in args]), redirect_stdout(io.StringIO()):
            exec(compile(COLLECTOR, "install-storage-fixture", "exec"), {})

    def test_current_rollback_running_and_their_python_environments_survive(self):
        obsolete = self.release(1)
        running = self.release(2)
        previous = self.release(3)
        current = self.release(4)
        environments = []
        for number, release in enumerate((obsolete, running, previous, current), 1):
            environment = self.artifact("python-env", number)
            environments.append(environment)
            self.link(environment, release / ".venv")
        self.collect(current=current, running=running / "trading", previous=previous, pressure=True)
        self.assertFalse(obsolete.exists())
        self.assertFalse(environments[0].exists())
        for path in (current, previous, running, *environments[1:]):
            self.assertTrue(path.exists(), path)

    def test_failed_releases_and_partial_caches_are_reclaimed(self):
        failed = self.release(1, ready=False)
        partial = self.artifact("python-env", 1, complete=False)
        self.collect()
        self.assertFalse(failed.exists())
        self.assertFalse(partial.exists())

    def test_failed_health_candidate_is_not_treated_as_legacy_backup(self):
        failed = self.release(1, ready=False)
        for name in (".venv/bin/python", "dashboard/dist/client/index.html"):
            path = failed / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        self.collect()
        self.assertFalse(failed.exists())

    def test_data_path_protects_ancestors_and_descendants(self):
        data_release = self.release(1, ready=False)
        (data_release / "records").mkdir()
        self.collect(data=data_release / "records")
        self.assertTrue(data_release.exists())
        cached = self.artifact("python-env", 1, complete=False)
        self.collect(data=self.root, pressure=True)
        self.assertTrue(data_release.exists())
        self.assertTrue(cached.exists())

    def test_active_source_and_stage_are_retained_without_success_marker(self):
        source = self.release(1, ready=False)
        stage = self.release(2, ready=False)
        obsolete = self.release(3, ready=False)
        self.collect(source=source / "trading", stage=stage)
        self.assertTrue(source.exists())
        self.assertTrue(stage.exists())
        self.assertFalse(obsolete.exists())

    def test_source_stage_data_and_unknown_paths_are_preserved(self):
        current = self.release(10)
        previous = self.release(9)
        source = self.release(1, ready=False)
        stage = self.release(2, ready=False)
        data_release = self.release(3, ready=False)
        data = data_release / "records"
        data.mkdir()
        unknown = self.releases / "user-backup"
        unknown.mkdir()
        env = self.artifact("python-env", 1)
        self.link(env, unknown / ".venv")
        external = self.base / "external"
        external.mkdir()
        sentinel = external / "keep"
        sentinel.write_text("untouched")
        self.link(external, self.releases / "release-20260921T120000Z-999999")
        self.collect(current=current, previous=previous, source=source / "trading", stage=stage, data=data, pressure=True)
        for path in (source, stage, data_release, unknown, env, sentinel):
            self.assertTrue(path.exists(), path)

    def test_data_ancestor_protects_cache_directories_and_dangling_aliases(self):
        item = self.artifact("npm-deps", 1)
        alias = self.cache / ("npm-" + "a" * 64)
        self.link(self.cache / "npm-deps-999999", alias)
        self.collect(data=self.root, pressure=True)
        self.assertTrue(item.exists())
        self.assertTrue(alias.is_symlink())

    def test_unused_caches_have_count_and_size_limits_and_pressure_reclaims_them(self):
        paths = [self.artifact(kind, number) for kind in ("python-env", "npm-deps", "frontend-build") for number in (1, 2, 3)]
        oversized = self.artifact("npm-deps", 4)
        with (oversized / "sparse-fixture").open("wb") as stream:
            stream.truncate(1024 * 1024 * 1024 + 1)
        self.collect()
        self.assertFalse(oversized.exists())
        for path in paths:
            self.assertEqual(path.exists(), path.name.endswith("000003"), path)
        self.collect(pressure=True)
        self.assertFalse(any(path.exists() for path in paths))

    def test_only_owned_cache_aliases_are_removed(self):
        owned = self.artifact("frontend-build", 1, complete=False)
        alias = self.cache / ("frontend-" + "a" * 64)
        self.link(owned, alias)
        user_owned = self.artifact("frontend-build", 2, complete=False)
        user_alias = self.cache / "my-cache"
        self.link(user_owned, user_alias)
        external_alias = self.cache / ("python-" + "b" * 64)
        self.link(self.base / "missing", external_alias)
        self.collect(pressure=True)
        self.assertFalse(alias.is_symlink())
        self.assertTrue(user_alias.is_symlink())
        self.assertTrue(user_owned.exists())
        self.assertTrue(external_alias.is_symlink())

    def test_data_symlink_preserves_both_access_path_and_real_contents(self):
        release = self.release(1, ready=False)
        real_data = self.data / "actual"
        real_data.mkdir()
        (real_data / "sentinel").write_text("keep")
        self.link(real_data, release / "data")
        self.collect(data=release / "data", pressure=True)
        self.assertTrue((release / "data").exists())
        self.assertEqual((real_data / "sentinel").read_text(), "keep")

    def test_root_and_nested_mounts_are_not_removed(self):
        mounted = self.release(1, ready=False)
        nested = self.release(2, ready=False)
        mountpoint = nested / "mounted-data"
        mountpoint.mkdir()
        mounts = {mounted, mountpoint}
        with patch("os.path.ismount", side_effect=lambda value: Path(value) in mounts):
            self.collect()
        self.assertTrue(mounted.exists())
        self.assertTrue(nested.exists())

    def test_symlinked_release_root_is_not_traversed(self):
        self.releases.rmdir()
        external = self.base / "other-releases"
        external.mkdir()
        self.link(external, self.releases)
        candidate = external / "release-20260921T120000Z-000001"
        candidate.mkdir()
        (candidate / ".install-owned").touch()
        self.collect()
        self.assertTrue(candidate.exists())

    def test_legacy_completed_release_can_be_retained_as_migration_backup(self):
        current = self.release(10)
        legacy = self.release(9, ready=False)
        (legacy / ".install-owned").unlink()
        for name in ("trading/server.py", "dashboard/package-lock.json", "deploy/aster-desk.service", ".venv/bin/python", "dashboard/dist/client/index.html"):
            path = legacy / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        failed = self.release(1, ready=False)
        self.collect(current=current)
        self.assertTrue(legacy.exists())
        self.assertFalse(failed.exists())
