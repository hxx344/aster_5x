"""Native build workspace tests: no real network, services or npm installation."""
from contextlib import redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dashboard_builder", ROOT / "deploy/build-dashboard.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class DashboardBuildWorkspaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "release"
        self.workspace = self.root / "cache/npm-deps-123456"
        self.workspace.mkdir(parents=True)
        (self.workspace / ".install-owned").touch()
        (self.source / "dashboard/lib").mkdir(parents=True)
        (self.source / "trading").mkdir()
        (self.source / "dashboard/package.json").write_text('{}')
        (self.source / "dashboard/tsconfig.json").write_text('{"compilerOptions":{"incremental":true}}')
        (self.source / "dashboard/lib/a.ts").write_text('export const a = 1;')
        (self.source / "trading/cycle-config.json").write_text('{}')
        self.calls, self.warm, self.packages = [], [], []
        self.failure, self.mutate = None, False

    def runner(self, args, directory):
        self.calls.append(tuple(args[:2]))
        modules = directory / "node_modules"
        if args[0] == "ci":
            modules.mkdir()
            (modules / "package.js").write_text('installed package')
        else:
            check = args[1]
            if check == "typecheck":
                self.warm.append((directory / builder.BUILD_INFO).exists())
                (directory / builder.BUILD_INFO).write_text('{"version":"fixture"}')
            elif check == "build":
                self.packages.append((modules / "package.js").stat().st_ino)
                scratch = modules / ".vite-rsc-temp"
                scratch.mkdir()
                (scratch / "bundle.js").write_text('generated')
                output = directory / "dist/client"
                output.mkdir(parents=True)
                (output / "index.html").write_text((directory / "lib/a.ts").read_text())
                if self.mutate:
                    (modules / "package.js").write_text('changed package')
            if check == self.failure:
                raise subprocess.CalledProcessError(42, args)

    def build(self, key="a" * 64):
        # Installer provides a fresh release target on every build attempt.
        output = self.source / "dashboard/dist"
        if output.exists():
            shutil.rmtree(output)
        log = io.StringIO()
        with redirect_stdout(log):
            builder.build(self.source, self.workspace, key, self.runner)
        return log.getvalue()

    def test_warm_build_keeps_package_inode_and_incremental_info_and_checks_changed_sources(self):
        self.build()
        (self.source / "dashboard/lib/a.ts").write_text('export const a = 2;')
        output = self.build()
        self.assertEqual(self.warm, [False, True])
        self.assertEqual(self.packages[0], self.packages[1])
        self.assertEqual(sum(call[0] == 'ci' for call in self.calls), 1)
        self.assertEqual(sum(call == ('run', 'typecheck') for call in self.calls), 2)
        self.assertIn('no dependency copy', output)
        self.assertIn('a = 2', (self.source / 'dashboard/dist/client/index.html').read_text())
        self.assertFalse((self.source / 'dashboard/node_modules').exists())

    def test_dependency_or_config_identity_discards_incremental_info(self):
        self.build()
        (self.source / "dashboard/tsconfig.json").write_text('{}')
        self.build()
        self.build(key="b" * 64)
        self.assertEqual(self.warm, [False, False, False])

    def test_deleted_source_old_outputs_and_scratch_are_not_carried_into_next_build(self):
        (self.source / 'dashboard/lib/deleted.ts').write_text('stale')
        (self.source / 'dashboard/.env').write_text('never copy this')
        self.build()
        dashboard = self.workspace / 'dashboard'
        (dashboard / 'dist/obsolete.js').write_text('obsolete')
        (dashboard / '.next').mkdir()
        (dashboard / '.next/old.ts').write_text('old generated type')
        (self.source / 'dashboard/lib/deleted.ts').unlink()
        self.build()
        for relative in ('lib/deleted.ts', '.env', 'dist/obsolete.js', '.next'):
            self.assertFalse((dashboard / relative).exists(), relative)

    def test_legacy_cache_is_moved_without_dependency_copy_or_install(self):
        old = self.workspace / 'node_modules'
        old.mkdir()
        (old / 'package.js').write_text('installed package')
        inode = (old / 'package.js').stat().st_ino
        (self.workspace / '.complete').touch()
        output = self.build()
        self.assertIn('Migrate npm dependency cache in place', output)
        self.assertEqual(self.packages, [inode])
        self.assertFalse(old.exists())
        self.assertFalse(any(call[0] == 'ci' for call in self.calls))

    def test_failure_leaves_workspace_incomplete_and_retry_installs_clean_dependencies(self):
        for check in ('typecheck', 'lint', 'build'):
            with self.subTest(check=check):
                self.failure = check
                with self.assertRaises(subprocess.CalledProcessError):
                    self.build()
                self.assertFalse((self.workspace / '.complete').exists())
                self.assertFalse((self.source / 'dashboard/dist/client/index.html').exists())
                self.failure = None
                self.build()
                self.assertFalse(self.warm[-1])
                self.assertTrue((self.workspace / '.complete').is_file())

    def test_package_mutation_discards_cache_but_checked_assets_remain_publishable(self):
        self.build()
        self.mutate = True
        output = self.build()
        self.assertIn('discard workspace cache', output)
        self.assertFalse((self.workspace / '.complete').exists())
        self.assertTrue((self.source / 'dashboard/dist/client/index.html').is_file())
        self.mutate = False
        self.build()
        self.assertEqual(sum(call[0] == 'ci' for call in self.calls), 2)
        self.assertFalse(self.warm[-1])

    def test_content_digest_detects_same_size_rewrite_even_with_restored_mtime(self):
        self.build()
        modules = self.workspace / 'dashboard/node_modules'
        before = builder.modules_digest(modules)
        path = modules / 'package.js'
        stat = path.stat()
        path.write_text('X' * stat.st_size)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertNotEqual(builder.modules_digest(modules), before)

    def test_unknown_and_nested_temp_names_are_not_excluded_from_digest(self):
        self.build()
        modules = self.workspace / 'dashboard/node_modules'
        before = builder.modules_digest(modules)
        (modules / '.unknown').mkdir()
        self.assertNotEqual(builder.modules_digest(modules), before)
        nested = modules / '.unknown/.vite'
        nested.mkdir()
        again = builder.modules_digest(modules)
        (nested / 'code.js').write_text('changed')
        self.assertNotEqual(builder.modules_digest(modules), again)

    def test_unmanaged_workspace_is_rejected_before_any_command(self):
        (self.workspace / '.install-owned').unlink()
        with self.assertRaisesRegex(ValueError, 'installer-owned'):
            self.build()
        self.assertEqual(self.calls, [])

    def test_stale_dependency_link_is_removed_without_touching_its_target(self):
        external = self.root / 'external-packages'
        external.mkdir()
        sentinel = external / 'package.js'
        sentinel.write_text('must survive')
        dashboard = self.workspace / 'dashboard'
        dashboard.mkdir()
        try:
            (dashboard / 'node_modules').symlink_to(external, target_is_directory=True)
        except OSError as error:
            if getattr(error, 'winerror', None) == 1314:
                self.skipTest('Windows cannot create symlinks; exercised in Linux CI')
            raise
        (self.workspace / '.complete').touch()
        (self.workspace / '.dependencies.sha256').write_text('a' * 64)
        self.build()
        self.assertEqual(sentinel.read_text(), 'must survive')
        self.assertFalse((dashboard / 'node_modules').is_symlink())
        self.assertEqual(sum(call[0] == 'ci' for call in self.calls), 1)

    def test_slow_steps_report_heartbeat_and_failures_with_duration(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(RuntimeError):
            with builder.step('Fixture typecheck', interval=.01):
                time.sleep(.04)
                raise RuntimeError('fixture')
        self.assertIn('still running', output.getvalue())
        self.assertIn('FAILED after', output.getvalue())


if __name__ == '__main__':
    unittest.main()
