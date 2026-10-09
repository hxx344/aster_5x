"""Candidate publication boundaries, with no GitHub network or credentials."""
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
PUBLISHER = Path(__file__).resolve().parents[1] / 'deploy/publish-release.py'


class PublishRelease(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='candidate-publish-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.commit = 'b' * 40
        self.repo, self.tag = 'hxx344/fixture', 'deploy-' + self.commit
        archive = self.root / 'fixture.tar.gz'; archive.write_bytes(b'fixture archive')
        item = {'file': archive.name, 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(), 'application_key': 'c' * 64}
        self.manifest = {'schema': 1, 'repository': self.repo, 'commit': self.commit, 'tag': self.tag,
                         'artifacts': {'linux-x64': item, 'linux-arm64': item}}
        (self.root / 'release-manifest.json').write_text(json.dumps(self.manifest), encoding='utf8')
        self.release = None
        self.calls = []
        self.upload_failure = False
        self.remote_manifest = self.manifest

    def fake_gh(self, args, **kwargs):
        self.calls.append(args)
        self.assertEqual(args[0], 'gh')
        self.assertNotIn('shell', kwargs)
        code, stdout, stderr = 0, '', ''
        operation = args[1:3]
        if operation == ['release', 'view']:
            if self.release:
                stdout = json.dumps({'isDraft': self.release['draft'], 'assets': self.release['assets']})
            else:
                code, stderr = 1, 'release not found'
        elif operation == ['release', 'create']:
            self.assertIn('--draft', args)
            self.release = {'tag_name': self.tag, 'draft': True, 'prerelease': True, 'assets': []}
        elif operation == ['release', 'upload']:
            self.assertTrue(self.release['draft'])
            if self.upload_failure:
                code, stderr = 1, 'fixture upload failure'
            else:
                source = next(Path(value) for value in args[4:] if Path(value).is_file())
                self.release['assets'].append({'name': source.name})
        elif operation == ['release', 'edit']:
            self.assertTrue(self.release['draft'])
            self.assertIn('--draft=false', args)
            self.assertIn('--prerelease=true', args)
            self.assertIn('--latest=false', args)
            self.release.update(draft=False, prerelease=True)
        elif operation == ['release', 'download']:
            directory = Path(args[args.index('--dir') + 1]); name = args[args.index('--pattern') + 1]
            if name == 'release-manifest.json':
                (directory / name).write_text(json.dumps(self.remote_manifest), encoding='utf8')
            else:
                (directory / name).write_bytes((self.root / name).read_bytes())
        else:
            self.fail('Candidate CI must not inspect or mutate latest: ' + str(args))
        if kwargs.get('check') and code:
            raise subprocess.CalledProcessError(code, args, stdout, stderr)
        return subprocess.CompletedProcess(args, code, stdout, stderr)

    def publish(self):
        environment = {'GITHUB_REPOSITORY': self.repo, 'GITHUB_SHA': self.commit,
                       'GITHUB_REF': 'refs/heads/main', 'GITHUB_EVENT_NAME': 'push'}
        with patch.dict(os.environ, environment), patch.object(sys, 'argv', [str(PUBLISHER), str(self.root)]), \
                patch('subprocess.run', self.fake_gh):
            try:
                runpy.run_path(str(PUBLISHER), run_name='__main__')
            except SystemExit as error:
                self.assertEqual(error.code, 0)

    def writes(self):
        return [call for call in self.calls if call[1:3] in (['release', 'create'], ['release', 'upload'], ['release', 'edit'])]

    def test_first_complete_candidate_never_advances_stable_or_compares_ancestry(self):
        self.publish()
        self.assertFalse(self.release['draft']); self.assertTrue(self.release['prerelease'])
        self.assertEqual({item['name'] for item in self.release['assets']}, {'fixture.tar.gz', 'release-manifest.json'})
        self.assertEqual(self.calls[-1][1:3], ['release', 'edit'])
        self.assertFalse(any(call[1] == 'api' for call in self.calls))

    def test_retry_completes_draft_before_publishing_candidate(self):
        self.release = {'tag_name': self.tag, 'draft': True, 'assets': []}
        self.publish()
        self.assertFalse(any(call[1:3] == ['release', 'create'] for call in self.calls))
        self.assertEqual(sum(call[1:3] == ['release', 'upload'] for call in self.calls), 2)
        self.assertTrue(self.release['prerelease'])

    def test_upload_failure_leaves_draft_unpublished(self):
        self.upload_failure = True
        with self.assertRaises((RuntimeError, subprocess.CalledProcessError)):
            self.publish()
        self.assertTrue(self.release['draft'])
        self.assertFalse(any(call[1:3] == ['release', 'edit'] for call in self.calls))

    def test_rerunning_public_candidate_or_stable_release_makes_no_changes(self):
        for prerelease in [True, False]:
            with self.subTest(prerelease=prerelease):
                self.calls.clear()
                self.release = {'tag_name': self.tag, 'draft': False, 'prerelease': prerelease,
                                'assets': [{'name': 'fixture.tar.gz'}, {'name': 'release-manifest.json'}]}
                self.publish()
                self.assertEqual(self.writes(), [])
                self.assertEqual(self.release['prerelease'], prerelease)

    def test_incomplete_or_different_public_release_is_never_modified(self):
        for assets, remote in [([], self.manifest), ([{'name': 'fixture.tar.gz'}, {'name': 'release-manifest.json'}], {**self.manifest, 'commit': 'a' * 40})]:
            with self.subTest(assets=assets):
                self.calls.clear(); self.release = {'tag_name': self.tag, 'draft': False, 'assets': assets}; self.remote_manifest = remote
                with self.assertRaises(ValueError):
                    self.publish()
                self.assertEqual(self.writes(), [])

    def test_checksum_failure_prevents_all_github_operations(self):
        (self.root / 'fixture.tar.gz').write_bytes(b'corrupt')
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
