"""Explicit stable promotion is read-only until all candidate checks succeed."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location('promote_release', Path(__file__).with_name('promote-release.py'))
promote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(promote)
WORKFLOWS = {'project-aggregation': 'ci.yml', 'gate-crossex-arbitrage': 'ci.yml', 'market-spread-monitor': 'linux.yml',
             'asset-ledger': 'verify.yml', 'aster_5x': 'deployment.yml', 'variational-grid': 'test.yml', 'greeks': 'tests.yml'}


class StablePromotion(unittest.TestCase):
    def setUp(self):
        self.commit = 'b' * 40; self.previous = 'a' * 40; self.tag = 'deploy-' + self.commit
        self.repository = 'hxx344/project-aggregation'
        self.content = {'runtime-linux-x64.tar.gz': b'x64 verified archive', 'runtime-linux-arm64.tar.gz': b'arm64 verified archive'}
        self.manifest = {'schema': 1, 'repository': self.repository, 'commit': self.commit, 'tag': self.tag,
                         'artifacts': {arch: {'file': name, 'sha256': hashlib.sha256(self.content[name]).hexdigest(), 'application_key': 'c' * 64}
                                       for arch, name in zip(['linux-x64', 'linux-arm64'], self.content)}}
        self.release = {'id': 42, 'draft': False, 'prerelease': True, 'tag_name': self.tag,
                        'assets': [{'name': name, 'state': 'uploaded', 'size': len(content)} for name, content in self.content.items()]}
        self.release['assets'].append({'name': 'release-manifest.json', 'state': 'uploaded', 'size': 500})
        self.run = {'head_sha': self.commit, 'head_branch': 'main', 'event': 'push', 'path': '.github/workflows/ci.yml',
                    'status': 'completed', 'conclusion': 'success'}
        self.runs = [self.run]; self.latest = None; self.comparison = 'ahead'; self.tag_commit = self.commit
        self.latest_error = 'HTTP 404 Not Found'
        self.environment = {'GITHUB_REPOSITORY': self.repository, 'RELEASE_COMMIT': self.commit,
                            'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_REF': 'refs/heads/main'}
        self.calls = []

    def fake_gh(self, args, **kwargs):
        self.calls.append(args)
        self.assertEqual(args[0], 'gh'); self.assertNotIn('shell', kwargs)
        code, output, error = 0, '', ''
        if args[1] == 'api':
            prefix = 'repos/' + self.repository + '/'
            self.assertTrue(args[2].startswith(prefix))
            suffix = args[2][len(prefix):]
            if suffix == 'releases/tags/' + self.tag:
                output = json.dumps(self.release)
            elif suffix == 'commits/' + self.tag:
                output = json.dumps({'sha': self.tag_commit})
            elif suffix == f'actions/runs?head_sha={self.commit}&per_page=100':
                output = json.dumps({'workflow_runs': self.runs})
            elif suffix == 'releases/latest':
                if self.latest:
                    output = json.dumps({'tag_name': self.latest})
                else:
                    code, error = 1, self.latest_error
            elif suffix == f'compare/{self.latest[7:]}...{self.commit}':
                output = json.dumps({'status': self.comparison})
            else:
                self.fail('Unexpected API request: ' + suffix)
        elif args[1:3] == ['release', 'download']:
            self.assertEqual(args[3], self.tag)
            self.assertEqual(args[args.index('--repo') + 1], self.repository)
            directory = Path(args[args.index('--dir') + 1]); name = args[args.index('--pattern') + 1]
            if name == 'release-manifest.json':
                (directory / name).write_text(json.dumps(self.manifest), encoding='utf8')
            else:
                self.assertIn(name, self.content)
                (directory / name).write_bytes(self.content[name])
        elif args[1:3] == ['release', 'edit']:
            self.assertIn('--prerelease=false', args); self.assertIn('--latest=true', args)
            self.assertEqual(args[3], self.tag)
            self.release['prerelease'] = False; self.latest = self.tag
        else:
            self.fail('Stable promotion must not create, upload, rebuild or run a shell: ' + str(args))
        return subprocess.CompletedProcess(args, code, output, error)

    def promote(self):
        with patch.dict(os.environ, self.environment), patch.object(promote.subprocess, 'run', self.fake_gh):
            promote.main()

    def writes(self):
        return [call for call in self.calls if call[1:3] == ['release', 'edit']]

    def reject(self):
        with self.assertRaises((ValueError, KeyError, TypeError, RuntimeError)):
            self.promote()
        self.assertEqual(self.writes(), [])

    def test_explicit_dispatch_promotes_checked_assets_for_all_seven_fixed_repositories(self):
        for repository, workflow in WORKFLOWS.items():
            with self.subTest(repository=repository):
                self.setUp(); self.repository = 'hxx344/' + repository
                self.environment['GITHUB_REPOSITORY'] = self.repository
                self.manifest['repository'] = self.repository; self.run['path'] = '.github/workflows/' + workflow
                self.promote()
                self.assertEqual(len(self.writes()), 1)
                self.assertFalse(self.release['prerelease'])
                self.assertEqual(self.latest, self.tag)
                downloads = [call[call.index('--pattern') + 1] for call in self.calls if call[1:3] == ['release', 'download']]
                self.assertCountEqual(downloads, ['release-manifest.json', *self.content])

    def test_only_full_commit_fixed_repository_and_explicit_main_dispatch_are_accepted(self):
        invalid = [{'GITHUB_REPOSITORY': 'other/project-aggregation'}, {'GITHUB_REPOSITORY': 'hxx344/unknown'},
                   {'RELEASE_COMMIT': 'main'}, {'RELEASE_COMMIT': 'b' * 39 + ';'}, {'RELEASE_COMMIT': '$(id)'},
                   {'GITHUB_EVENT_NAME': 'push'}, {'GITHUB_EVENT_NAME': 'pull_request'}, {'GITHUB_REF': 'refs/heads/feature'}]
        for change in invalid:
            with self.subTest(change=change):
                self.setUp(); self.environment.update(change); self.reject(); self.assertEqual(self.calls, [])

    def test_ci_must_be_successful_main_run_of_exact_required_workflow(self):
        invalid = [{'status': 'in_progress'}, {'conclusion': 'failure'}, {'conclusion': 'cancelled'},
                   {'head_sha': self.previous}, {'head_branch': 'feature'}, {'event': 'pull_request'},
                   {'path': '.github/workflows/promote-release.yml'}, {'path': '.github/workflows/another.yml'}]
        for change in invalid:
            with self.subTest(change=change):
                self.setUp(); self.run.update(change); self.reject()
        self.setUp(); self.runs = []; self.reject()

    def test_draft_mismatched_tag_and_moved_tag_commit_are_never_promoted(self):
        for scenario in ['draft', 'tag', 'commit']:
            with self.subTest(scenario=scenario):
                self.setUp()
                if scenario == 'draft': self.release['draft'] = True
                if scenario == 'tag': self.release['tag_name'] = 'deploy-' + self.previous
                if scenario == 'commit': self.tag_commit = self.previous
                self.reject()

    def test_manifest_identity_architectures_and_downloaded_hashes_must_match(self):
        for scenario in ['schema', 'repository', 'commit', 'tag', 'missing-arch', 'extra-arch', 'path', 'sha', 'application-key', 'missing-asset', 'unfinished-asset', 'corrupt-archive']:
            with self.subTest(scenario=scenario):
                self.setUp(); item = self.manifest['artifacts']['linux-x64']
                if scenario == 'schema': self.manifest['schema'] = True
                if scenario == 'repository': self.manifest['repository'] = 'hxx344/other'
                if scenario == 'commit': self.manifest['commit'] = self.previous
                if scenario == 'tag': self.manifest['tag'] = 'deploy-' + self.previous
                if scenario == 'missing-arch': del self.manifest['artifacts']['linux-arm64']
                if scenario == 'extra-arch': self.manifest['artifacts']['windows-x64'] = copy.deepcopy(item)
                if scenario == 'path': item['file'] = '../other.tar.gz'
                if scenario == 'sha': item['sha256'] = 'invalid'
                if scenario == 'application-key': item['application_key'] = 'invalid'
                if scenario == 'missing-asset': self.release['assets'].pop(0)
                if scenario == 'unfinished-asset': self.release['assets'][0]['state'] = 'new'
                if scenario == 'corrupt-archive': self.content[item['file']] = b'tampered downloaded bytes'
                self.reject()

    def test_older_diverged_foreign_latest_or_api_failure_never_move_stable(self):
        for status in ['behind', 'diverged', 'unknown']:
            with self.subTest(status=status):
                self.setUp(); self.latest = 'deploy-' + self.previous; self.comparison = status; self.reject()
        self.setUp(); self.latest = 'other-channel'; self.reject()
        self.setUp(); self.latest_error = 'HTTP 503 Service Unavailable'; self.reject()

    def test_only_descendant_can_advance_existing_stable(self):
        self.latest = 'deploy-' + self.previous
        self.promote()
        self.assertEqual(len(self.writes()), 1)
        self.assertIn(['gh', 'api', f'repos/{self.repository}/compare/{self.previous}...{self.commit}'], self.calls)

    def test_repeat_dispatch_for_current_stable_is_idempotent(self):
        self.promote(); self.calls.clear()
        self.promote()
        self.assertEqual(self.writes(), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
