"""Opt-in real npm build check, run by deployment CI in an isolated workspace."""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('dashboard_builder', ROOT / 'deploy/build-dashboard.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def main():
    with tempfile.TemporaryDirectory(prefix='aster-real-build-') as temporary:
        root = Path(temporary).resolve()
        source, workspace = root / 'release', root / 'cache/npm-deps-123456'
        source.mkdir()
        workspace.mkdir(parents=True)
        (workspace / '.install-owned').touch()
        shutil.copytree(ROOT / 'dashboard', source / 'dashboard',
                        ignore=shutil.ignore_patterns(*builder.GENERATED, builder.BUILD_INFO, '.env*'))
        (source / 'trading').mkdir()
        shutil.copy2(ROOT / 'trading/cycle-config.json', source / 'trading/cycle-config.json')
        package = workspace / 'dashboard/node_modules/typescript/lib/tsc.js'
        initial_inode = None
        for label in ('cold', 'warm'):
            if label == 'warm':
                shutil.rmtree(source / 'dashboard/dist')
                with (source / 'dashboard/lib/cycle-quality.ts').open('a') as stream:
                    stream.write('\n// Real incremental build fixture.\n')
            started = time.monotonic()
            builder.build(source, workspace, 'a' * 64)
            assert (workspace / '.complete').is_file(), 'Genuine build changed installed package files'
            assert (workspace / 'dashboard/tsconfig.tsbuildinfo').is_file()
            assert (source / 'dashboard/dist/client/index.html').is_file()
            if initial_inode is not None:
                assert package.stat().st_ino == initial_inode, 'Warm build reinstalled/copied dependencies'
            initial_inode = package.stat().st_ino
            print(f'REAL {label} BUILD PASS: {time.monotonic() - started:.2f}s', flush=True)
        shutil.rmtree(source / 'dashboard/dist')
        with (source / 'dashboard/lib/cycle-quality.ts').open('a') as stream:
            stream.write('\nexport const typecheckRegression: number = "must fail";\n')
        try:
            builder.build(source, workspace, 'a' * 64)
        except subprocess.CalledProcessError:
            assert not (workspace / '.complete').exists()
            assert not (source / 'dashboard/dist/client/index.html').exists()
            print('WARM TYPE ERROR BLOCKED PUBLICATION: PASS', flush=True)
        else:
            raise AssertionError('Incremental typecheck missed a new type error')


if __name__ == '__main__':
    main()
