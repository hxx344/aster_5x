"""Exclusive, reusable frontend workspace. Caller holds the installer flock.

Only checked static output is copied to a release. A workspace is reusable only
after a successful build that leaves installed package contents unchanged.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import threading
import time


GENERATED = {"node_modules", "dist", ".wrangler", ".vinext", ".next", ".git", "__pycache__"}
MODULE_TEMP = {".vite", ".vite-temp", ".vite-rsc-temp"}
BUILD_INFO = "tsconfig.tsbuildinfo"


def log(message):
    print("[upgrade] " + message, flush=True)


@contextmanager
def step(name, interval=15):
    started = time.monotonic()
    stop = threading.Event()
    def progress():
        while not stop.wait(interval):
            log(f"{name}: still running ({time.monotonic() - started:.0f}s)")
    worker = threading.Thread(target=progress, daemon=True)
    log(name + "...")
    worker.start()
    try:
        yield
    except BaseException:
        log(f"{name}: FAILED after {time.monotonic() - started:.1f}s")
        raise
    else:
        log(f"{name}: {time.monotonic() - started:.1f}s")
    finally:
        stop.set()
        worker.join()


def linked(path):
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def remove(path, root):
    # Never follow a stale workspace link while cleaning generated/source files.
    if not path.parent.resolve().is_relative_to(root):
        raise ValueError("Cleanup escaped the build workspace")
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif getattr(path, "is_junction", lambda: False)():
        path.rmdir()
    elif path.exists():
        if os.path.ismount(path):
            raise ValueError("Refusing to remove a mounted workspace directory")
        shutil.rmtree(path)


def modules_digest(modules):
    """Hash package bytes/paths/modes/links, not mtimes or tool scratch output."""
    digest = hashlib.sha256(b"npm-workspace-v1")
    def entry(path):
        relative = path.relative_to(modules).as_posix()
        info = path.lstat()
        kind = stat.S_IFMT(info.st_mode)
        for value in (relative, str(kind), str(stat.S_IMODE(info.st_mode))):
            encoded = value.encode()
            digest.update(len(encoded).to_bytes(8, "big") + encoded)
        if stat.S_ISLNK(info.st_mode):
            digest.update(os.readlink(path).encode())
        elif stat.S_ISREG(info.st_mode):
            digest.update(info.st_size.to_bytes(8, "big"))
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
        elif stat.S_ISDIR(info.st_mode) and getattr(info, "st_reparse_tag", 0) != getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -1):
            for child in sorted(path.iterdir()):
                entry(child)
        else:
            raise ValueError("Unsupported entry in installed npm dependencies")
    if linked(modules) or not modules.is_dir():
        raise ValueError("npm dependencies must be a real workspace directory")
    for path in sorted(modules.iterdir()):
        if path.name not in MODULE_TEMP:
            entry(path)
    return digest.hexdigest()


def mirror_source(source, workspace, keep_info):
    dashboard = workspace / "dashboard"
    if linked(dashboard):
        raise ValueError("Dashboard workspace cannot be a link")
    dashboard.mkdir(exist_ok=True)
    for path in dashboard.iterdir():
        if path.name != "node_modules" and not (keep_info and path.name == BUILD_INFO):
            remove(path, workspace)
    def ignored(_directory, names):
        return {name for name in names if name in GENERATED or name == BUILD_INFO or name.startswith(".env")}
    shutil.copytree(source / "dashboard", dashboard, dirs_exist_ok=True, ignore=ignored)
    trading = workspace / "trading"
    remove(trading, workspace)
    trading.mkdir()
    shutil.copy2(source / "trading/cycle-config.json", trading / "cycle-config.json")
    modules = dashboard / "node_modules"
    if modules.is_dir() and not linked(modules):
        for name in MODULE_TEMP:
            remove(modules / name, workspace)
    return dashboard


def read_text(path):
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def typecheck_key(source, dependency_key):
    digest = hashlib.sha256(("typecheck-v1:" + dependency_key).encode())
    for path in sorted((source / "dashboard").glob("tsconfig*.json")):
        digest.update(path.name.encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def build(source, workspace, dependency_key, run=None):
    source, workspace = Path(source).resolve(), Path(workspace).absolute()
    if workspace.resolve() != workspace or linked(workspace) or workspace.parent.name != "cache" \
            or not re.fullmatch(r"npm-deps-[A-Za-z0-9]{6}", workspace.name) \
            or not (workspace / ".install-owned").is_file():
        raise ValueError("Expected an installer-owned npm workspace")
    if source == workspace or source.is_relative_to(workspace) or workspace.is_relative_to(source):
        raise ValueError("Build source and workspace must be separate")
    if not re.fullmatch(r"[a-f0-9]{64}", dependency_key):
        raise ValueError("Invalid npm dependency identity")
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if run is None:
        if not npm:
            raise ValueError("npm is unavailable")
        def run(arguments, directory):
            subprocess.run([npm, *arguments], cwd=directory, check=True)
    marker = workspace / ".complete"
    completed = marker.is_file()
    marker.unlink(missing_ok=True)  # A crash or interruption never publishes a reusable cache.
    dashboard = workspace / "dashboard"
    modules = dashboard / "node_modules"
    baseline_path = workspace / ".dependencies.sha256"
    baseline = read_text(baseline_path)
    old_modules = workspace / "node_modules"
    legacy = completed and old_modules.is_dir() and not linked(old_modules) and not dashboard.exists()
    if legacy:
        dashboard.mkdir()
        old_modules.rename(modules)  # Same filesystem, no traversal/copy of package files.
        baseline = ""
        log("Migrate npm dependency cache in place (no dependency copy)")
    reusable = completed and modules.is_dir() and not linked(modules) and (legacy or re.fullmatch(r"[a-f0-9]{64}", baseline))
    check_key = typecheck_key(source, dependency_key)
    keep_info = bool(reusable and read_text(workspace / ".typecheck-key") == check_key
                     and (dashboard / BUILD_INFO).is_file() and not linked(dashboard / BUILD_INFO))
    with step("Prepare dashboard workspace"):
        dashboard = mirror_source(source, workspace, keep_info)
    with step("Prepare npm dependencies"):
        if reusable:
            log("Reuse npm dependencies in place (no dependency copy)")
        else:
            remove(modules, workspace)
            run(["ci", "--include=dev", "--no-audit", "--no-fund", "--prefer-offline", "--cache", str(workspace / ".npm")], dashboard)
            remove(workspace / ".npm", workspace)
        if not reusable or legacy:
            baseline = modules_digest(modules)
            baseline_path.write_text(baseline, encoding="utf-8")
    with step("Typecheck dashboard"):
        log("Reuse TypeScript incremental cache; changed files are still checked" if keep_info
            else "No compatible TypeScript cache; full typecheck")
        run(["run", "typecheck"], dashboard)
    with step("Lint dashboard"):
        run(["run", "lint"], dashboard)
    with step("Build dashboard assets"):
        run(["run", "build"], dashboard)
        if not (dashboard / "dist/client/index.html").is_file():
            raise ValueError("Dashboard build did not produce index.html")
    with step("Verify reusable npm dependencies"):
        if modules_digest(modules) == baseline:
            (workspace / ".typecheck-key").write_text(check_key, encoding="utf-8")
            marker.touch()
        else:
            log("Build changed installed packages; discard workspace cache after this release")
    with step("Publish dashboard assets"):
        destination = source / "dashboard/dist/client"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(dashboard / "dist/client", destination)


if __name__ == "__main__":
    try:
        build(*sys.argv[1:])
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode if error.returncode > 0 else 1)
