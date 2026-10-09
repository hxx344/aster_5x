"""Explicitly promote an existing, successful CI candidate to a stable release."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


WORKFLOWS = {
    'project-aggregation': 'ci.yml', 'gate-crossex-arbitrage': 'ci.yml',
    'market-spread-monitor': 'linux.yml', 'asset-ledger': 'verify.yml',
    'aster_5x': 'deployment.yml', 'variational-grid': 'test.yml', 'greeks': 'tests.yml',
}


def gh(*arguments, check=True):
    result = subprocess.run(['gh', *arguments], capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(result.stderr)
    return result


def api(repository, suffix):
    return json.loads(gh('api', f'repos/{repository}/{suffix}').stdout)


def main():
    repository = os.environ.get('GITHUB_REPOSITORY', '')
    commit = os.environ.get('RELEASE_COMMIT', '')
    name = repository.removeprefix('hxx344/')
    if repository != 'hxx344/' + name or name not in WORKFLOWS or not re.fullmatch(r'[a-f0-9]{40}', commit):
        raise ValueError('Choose a full CI commit from this repository')
    if os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch' or os.environ.get('GITHUB_REF') != 'refs/heads/main':
        raise ValueError('Stable publication requires an explicit main workflow dispatch')
    tag = 'deploy-' + commit
    release = api(repository, 'releases/tags/' + tag)
    if release['draft'] or release['tag_name'] != tag or api(repository, 'commits/' + tag)['sha'] != commit:
        raise ValueError('Candidate must be completely published and pinned to the selected commit')
    runs = api(repository, f'actions/runs?head_sha={commit}&per_page=100')['workflow_runs']
    expected = '.github/workflows/' + WORKFLOWS[name]
    if not any(run['head_sha'] == commit and run['head_branch'] == 'main' and
               run['event'] in ('push', 'workflow_dispatch') and run['path'] == expected and
               run['status'] == 'completed' and run['conclusion'] == 'success' for run in runs):
        raise ValueError('The required main CI workflow has not completed successfully for this commit')
    with tempfile.TemporaryDirectory(prefix='stable-release-') as temporary:
        directory = Path(temporary)
        gh('release', 'download', tag, '--repo', repository, '--pattern', 'release-manifest.json', '--dir', temporary)
        manifest = json.loads((directory / 'release-manifest.json').read_text(encoding='utf-8'))
        if (type(manifest['schema']) is not int or manifest['schema'] != 1 or
                manifest['repository'] != repository or manifest['commit'] != commit or manifest['tag'] != tag or
                set(manifest['artifacts']) != {'linux-x64', 'linux-arm64'}):
            raise ValueError('Candidate manifest identity or architecture set is invalid')
        assets = {item['name']: item for item in release['assets']}
        downloaded = set()
        for item in manifest['artifacts'].values():
            filename = item['file']
            if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*\.tar\.gz', filename) or
                    not re.fullmatch(r'[a-f0-9]{64}', item['sha256']) or
                    not re.fullmatch(r'[a-f0-9]{64}', item['application_key']) or
                    filename not in assets or assets[filename]['state'] != 'uploaded'):
                raise ValueError('Candidate is missing a checked runtime archive')
            if filename not in downloaded:
                gh('release', 'download', tag, '--repo', repository, '--pattern', filename, '--dir', temporary)
                downloaded.add(filename)
            checksum = hashlib.sha256()
            with (directory / filename).open('rb') as archive:
                for block in iter(lambda: archive.read(1024 * 1024), b''):
                    checksum.update(block)
            if checksum.hexdigest() != item['sha256']:
                raise ValueError('Candidate archive checksum mismatch')
    # Publication shares the same repository-wide lock as candidate uploads.
    latest = gh('api', f'repos/{repository}/releases/latest', check=False)
    if latest.returncode == 0:
        previous = json.loads(latest.stdout)['tag_name']
        if not re.fullmatch(r'deploy-[a-f0-9]{40}', previous):
            raise ValueError('Latest stable release is outside the deployment channel')
        status = api(repository, f'compare/{previous[7:]}...{commit}')['status']
        if status not in ('ahead', 'identical'):
            raise ValueError('Refusing to move the stable channel backwards or to a different history')
        if previous == tag and release.get('prerelease') is False:
            print('This verified version is already the stable release; nothing changed.')
            return
    elif '404' not in latest.stderr:
        raise RuntimeError(latest.stderr)
    gh('release', 'edit', tag, '--repo', repository, '--prerelease=false', '--latest=true',
       '--title', f'Stable {commit[:12]}')
    print('Explicitly published stable deployment ' + tag)


if __name__ == '__main__':
    main()
