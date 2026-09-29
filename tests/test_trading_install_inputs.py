"""Native tests of the exact fingerprint recipe executed by the Linux installer."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / 'install-trading.sh').read_text(encoding='utf-8')
RECIPE = SCRIPT.split('# INPUTS_BEGIN:', 1)[1].split('\n', 1)[1].split('# INPUTS_END', 1)[0]


class TradingInstallInputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name)
        for name in ('requirements.txt', 'requirements.lock', 'monitor.py', 'config.json',
                     'install-trading.sh', 'DEPLOYMENT.md', 'trading/server.py',
                     'trading/cycle-config.json', 'deploy/build-dashboard.py',
                     'deploy/aster-desk.service', 'dashboard/package.json',
                     'dashboard/package-lock.json', 'dashboard/app/page.tsx'):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('original', encoding='utf-8')

    def keys(self):
        result = subprocess.run([sys.executable, '-c', RECIPE, str(self.source), 'node-runtime', 'npm-version'],
                                check=True, capture_output=True, text=True)
        return result.stdout.strip().splitlines()

    def write(self, name, content):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')

    def test_docs_and_tests_do_not_change_runtime_or_build_inputs(self):
        before = self.keys()
        self.assertEqual(len(before), 4)
        for name in ('README.md', 'DEPLOYMENT.md', 'dashboard/README.md',
                     'dashboard/tests/fixture.test.mjs', 'dashboard/tests/fixture.test.ts',
                     'deploy/relay.env.example', 'deploy/aster-capacity-relay.service'):
            self.write(name, 'test or documentation edit')
            self.assertEqual(self.keys(), before, name)

    def test_backend_frontend_shared_config_and_lock_have_separate_effects(self):
        before = self.keys()
        self.write('trading/server.py', 'backend edit')
        backend = self.keys()
        self.assertEqual(backend[:3], before[:3])
        self.assertNotEqual(backend[3], before[3])
        self.write('dashboard/app/page.tsx', 'frontend edit')
        frontend = self.keys()
        self.assertEqual(frontend[:2], backend[:2])
        self.assertNotEqual(frontend[2:], backend[2:])
        self.write('trading/cycle-config.json', 'shared config edit')
        shared = self.keys()
        self.assertNotEqual(shared[2], frontend[2])
        self.write('requirements.lock', 'Python dependency edit')
        python = self.keys()
        self.assertNotEqual(python[0], shared[0])
        self.assertEqual(python[1:3], shared[1:3])

    def test_public_assets_and_service_changes_are_not_ignored(self):
        before = self.keys()
        self.write('dashboard/public/readme.md', 'served asset')
        asset = self.keys()
        self.assertNotEqual(asset[2], before[2])
        self.write('deploy/aster-desk.service', 'service edit')
        service = self.keys()
        self.assertEqual(service[:3], asset[:3])
        self.assertNotEqual(service[3], asset[3])


if __name__ == '__main__':
    unittest.main()
