"""Publish all checked assets together; never replace an immutable release asset."""
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile


def gh(*arguments, check=True):
    return subprocess.run(["gh", *arguments], check=check, capture_output=True, text=True)


def should_promote_latest(repository, commit):
    # The workflow serializes publication. A newer failing build must not hide
    # this checked package; an older successful job must not move latest back.
    latest = gh("api", f"repos/{repository}/releases/latest", check=False)
    if latest.returncode:
        if "404" in latest.stderr:
            return True
        raise RuntimeError(latest.stderr)
    tag = json.loads(latest.stdout)["tag_name"]
    if not re.fullmatch(r"deploy-[a-f0-9]{40}", tag):
        raise ValueError("Latest release is outside the deployment channel")
    previous = tag.removeprefix("deploy-")
    status = json.loads(gh("api", f"repos/{repository}/compare/{previous}...{commit}").stdout)["status"]
    if status not in {"ahead", "identical", "behind", "diverged"}:
        raise ValueError("Unknown release ancestry")
    return status in {"ahead", "identical"}


def main():
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "release-output")
    manifest = json.loads((output / "release-manifest.json").read_text(encoding="utf-8"))
    repository, commit, tag = (manifest[key] for key in ("repository", "commit", "tag"))
    if repository != os.environ["GITHUB_REPOSITORY"] or commit != os.environ["GITHUB_SHA"] or tag != "deploy-" + commit:
        raise ValueError("Release package provenance differs from this workflow")
    if os.environ.get("GITHUB_REF") != "refs/heads/main" or os.environ.get("GITHUB_EVENT_NAME") not in {"push", "workflow_dispatch"}:
        raise ValueError("Only the main push/dispatch workflow can publish deployment assets")

    for item in manifest["artifacts"].values():
        if hashlib.sha256((output / item["file"]).read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError("Release archive hash differs from its manifest")
    names = sorted({"release-manifest.json", *(item["file"] for item in manifest["artifacts"].values())})
    existing = gh("release", "view", tag, "--repo", repository, "--json", "isDraft,assets", check=False)
    if existing.returncode == 0:
        release = json.loads(existing.stdout)
        if not release["isDraft"]:
            if not set(names) <= {item["name"] for item in release["assets"]}:
                raise ValueError("Published deployment release is incomplete")
            with tempfile.TemporaryDirectory(prefix="deploy-existing-") as temporary:
                gh("release", "download", tag, "--repo", repository, "--pattern", "release-manifest.json", "--dir", temporary)
                if json.loads((Path(temporary) / "release-manifest.json").read_text(encoding="utf-8")) != manifest:
                    raise ValueError("Published release differs; refusing to overwrite it")
            print(f"{tag} is already published; immutable assets remain unchanged.")
            return
    else:
        gh("release", "create", tag, "--repo", repository, "--target", commit, "--draft",
           "--title", f"Deployment {commit[:12]}", "--notes", f"Checked runtime package for commit {commit}.")
        release = {"assets": []}
    existing_names = {item["name"] for item in release["assets"]}
    with tempfile.TemporaryDirectory(prefix="deploy-publish-") as temporary:
        for name in names:
            path = output / name
            if name in existing_names:
                gh("release", "download", tag, "--repo", repository, "--pattern", name, "--dir", temporary)
                if hashlib.sha256((Path(temporary) / name).read_bytes()).digest() != hashlib.sha256(path.read_bytes()).digest():
                    raise ValueError(f"Refusing to overwrite an existing draft asset: {name}")
            else:
                gh("release", "upload", tag, str(path), "--repo", repository)
    promote = should_promote_latest(repository, commit)
    gh("release", "edit", tag, "--repo", repository, "--draft=false", "--latest=" + str(promote).lower())
    print(f"Published all checked deployment assets for {commit}.")


if __name__ == "__main__":
    main()
