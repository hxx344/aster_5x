"""Build the self-contained runtime archive consumed by the one-file installer."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile

REPOSITORY = "hxx344/aster_5x"
PAYLOAD_PATHS = ("trading", "requirements.txt", "requirements.lock", "monitor.py", "config.json",
                 "deploy/aster-desk", "deploy/aster-desk.service", "deploy/trading.env.example",
                 "deploy/configure-feishu.py", "deploy/configure-capacity-relay.py", "DEPLOYMENT.md")
REQUIRED_FILES = ("trading/server.py", "requirements.lock", "dashboard/dist/client/index.html")


def git(root: Path, *arguments: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *arguments])


def tree(root: Path, commit: str, *paths: str) -> bytes:
    return git(root, "ls-tree", "-r", commit, "--", *paths)


def inputs(root: Path, commit: str) -> dict[str, str]:
    if REPOSITORY.endswith("/greeks"):
        return {"dependency_tree": hashlib.sha256(tree(root, commit, "pyproject.toml", "uv.lock")).hexdigest()}
    if REPOSITORY.endswith("/variational-grid"):
        complete = tree(root, commit, "variational_grid").decode("utf-8")
        engine = "".join(line + "\n" for line in complete.splitlines()
                         if not line.split("\t", 1)[1].startswith("variational_grid/web/")
                         and line.split("\t", 1)[1] not in {"variational_grid/dashboard.py", "variational_grid/hub.py"})
        return {"engine_tree": engine, "web_tree": complete}
    return {}


def runtime_files(root: Path, commit: str) -> dict[str, tuple[bytes, int]]:
    files = {}
    for record in git(root, "ls-tree", "-rz", commit, "--", *PAYLOAD_PATHS).split(b"\0"):
        if not record:
            continue
        metadata, raw_name = record.split(b"\t", 1)
        mode, kind, identity = metadata.split()
        name = raw_name.decode("utf-8")
        if mode not in {b"100644", b"100755"} or kind != b"blob":
            raise ValueError(f"Runtime source must be a regular file: {name}")
        files[name] = (git(root, "cat-file", "blob", identity.decode()), 0o755 if mode == b"100755" else 0o644)
    if REPOSITORY.endswith("/aster_5x"):
        directory = root / "dashboard/dist/client"
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("The checked dashboard/client build is required")
        for path in sorted(directory.rglob("*")):
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                raise ValueError("Runtime assets cannot contain links")
            if path.is_file():
                files[path.relative_to(root).as_posix()] = (path.read_bytes(), 0o644)
    for name in REQUIRED_FILES:
        if name not in files or not files[name][0]:
            raise ValueError(f"Missing runtime input: {name}")
    return files


def application_key(files: dict[str, tuple[bytes, int]]) -> str:
    result = hashlib.sha256(b"ci-runtime-v1\0")
    for name, (content, mode) in sorted(files.items()):
        if name.endswith(".md") or name.startswith("tests/"):
            continue
        for value in (name.encode(), str(mode).encode(), content):
            result.update(len(value).to_bytes(8, "big"))
            result.update(value)
    return result.hexdigest()


def build_release(root: Path, commit: str, output: Path) -> dict:
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("A full immutable commit is required")
    files = runtime_files(root, commit)
    key = application_key(files)
    files[".release-commit"] = ((commit + "\n").encode(), 0o644)
    files[".release-application-key"] = ((key + "\n").encode(), 0o644)
    files[".release-inputs.json"] = ((json.dumps(inputs(root, commit), sort_keys=True) + "\n").encode(), 0o644)
    output.mkdir(parents=True, exist_ok=True)
    filename = REPOSITORY.split("/")[1] + "-linux.tar.gz"
    destination = output / filename
    # Canonical metadata avoids archive changes caused by runner clocks and owners.
    with destination.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT, dereference=True) as archive:
            for name, (content, mode) in sorted(files.items()):
                entry = tarfile.TarInfo(name)
                entry.mode, entry.size, entry.mtime = mode, len(content), 0
                archive.addfile(entry, io.BytesIO(content))
    item = {"file": filename, "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(), "application_key": key}
    manifest = {"schema": 1, "repository": REPOSITORY, "commit": commit, "tag": "deploy-" + commit,
                "artifacts": {architecture: item for architecture in ("linux-x64", "linux-arm64")}}
    (output / "release-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--commit")
    parser.add_argument("--output", type=Path, default=Path("release-output"))
    arguments = parser.parse_args()
    commit = arguments.commit or git(arguments.root, "rev-parse", "HEAD").decode().strip()
    if git(arguments.root, "rev-parse", "HEAD").decode().strip() != commit:
        raise ValueError("The runtime build must belong to the requested checkout")
    manifest = build_release(arguments.root, commit, arguments.output)
    print(f"Packaged {manifest['repository']} {commit[:12]} ({manifest['artifacts']['linux-x64']['application_key'][:12]})")


if __name__ == "__main__":
    main()
