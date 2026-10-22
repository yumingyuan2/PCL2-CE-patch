#!/usr/bin/env python3
"""Work out which upstream PCL-CE releases still need a patched build.

GitHub Actions cannot subscribe to another repository's ``release`` event, so
the workflow runs this script on a schedule (and on workflow_dispatch).  The
script reads the upstream release list, keeps the newest stable and the newest
beta, and drops anything that already has a complete patched release in this
repository.

Required environment variables:
    GITHUB_REPOSITORY   owner/name of this repository
    GH_TOKEN            token used for the GitHub API (GITHUB_TOKEN is fine)
    GITHUB_OUTPUT       file to append workflow outputs to

Optional:
    UPSTREAM_REPOSITORY default: PCL-Community/PCL-CE
    INPUT_TAG           build one specific upstream tag instead of auto-detect
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

API_ROOT = "https://api.github.com"
UPSTREAM = os.environ.get("UPSTREAM_REPOSITORY", "PCL-Community/PCL-CE")
REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")
TOKEN = os.environ.get("GH_TOKEN", "")
INPUT_TAG = os.environ.get("INPUT_TAG", "").strip()
FORCE_REBUILD = os.environ.get("FORCE_REBUILD", "").strip().lower() in {"1", "true", "yes"}
EDITION = os.environ.get("EDITION", "ce").strip().lower()
ASSET_PREFIX = "PCL2_OE" if EDITION == "oe" else "PCL2_CE"
RELEASE_SUFFIX = "-oe-patched" if EDITION == "oe" else "-patched"


def api(path: str, *, allow_not_found: bool = False):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "PCL2-CE-patch-planner",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    request = urllib.request.Request(API_ROOT + path, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if allow_not_found and error.code == 404:
            return None
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API {path} failed: HTTP {error.code}: {detail}") from error


def release_version(tag: str) -> str:
    return tag[1:] if tag.startswith("v") else tag


def release_configuration(release: dict) -> str:
    return "Beta" if release.get("prerelease") else "Release"


def patched_tag(version: str) -> str:
    return f"v{version}{RELEASE_SUFFIX}"


def has_complete_patched_release(version: str, configuration: str) -> bool:
    release = api(f"/repos/{REPOSITORY}/releases/tags/{patched_tag(version)}", allow_not_found=True)
    if not release:
        return False
    names = {asset.get("name", "") for asset in release.get("assets", [])}
    return {
        f"{ASSET_PREFIX}_{configuration}_x64_patched.exe",
        f"{ASSET_PREFIX}_{configuration}_ARM64_patched.exe",
    }.issubset(names)


def choose_candidates() -> list[dict]:
    if INPUT_TAG:
        tag = INPUT_TAG if INPUT_TAG.startswith("v") else f"v{INPUT_TAG}"
        release = api(f"/repos/{UPSTREAM}/releases/tags/{tag}", allow_not_found=True)
        if not release:
            raise RuntimeError(f"Upstream release {tag} was not found in {UPSTREAM}")
        return [release]

    releases = api(f"/repos/{UPSTREAM}/releases?per_page=100")
    published = [
        release
        for release in releases
        if not release.get("draft") and release.get("published_at")
    ]
    if not published:
        return []

    stable = next((release for release in published if not release.get("prerelease")), None)
    beta = next((release for release in published if release.get("prerelease")), None)

    selected: list[dict] = []
    if stable:
        selected.append(stable)
    if beta:
        # Always keep a patched build for the newest beta.  A beta can be
        # older than the newest stable and still be the version installed by
        # users on the beta channel, so do not skip it based on publish time.
        selected.append(beta)

    return selected


def main() -> int:
    if not REPOSITORY:
        print("error: GITHUB_REPOSITORY is not set", file=sys.stderr)
        return 2

    output_path = Path(os.environ.get("GITHUB_OUTPUT", "github_output.txt"))
    candidates = choose_candidates()

    releases: list[dict] = []
    builds: list[dict] = []
    for release in candidates:
        tag = release.get("tag_name", "")
        if not tag:
            continue
        version = release_version(tag)
        configuration = release_configuration(release)
        if not FORCE_REBUILD and has_complete_patched_release(version, configuration):
            print(f"skip {tag}: {patched_tag(version)} already has both patched assets")
            continue

        item = {
            "tag": tag,
            "version": version,
            "configuration": configuration,
            "prerelease": bool(release.get("prerelease")),
        }
        releases.append(item)
        for architecture in ("x64", "ARM64"):
            builds.append({**item, "arch": architecture})
        print(f"plan {tag}: {configuration}, version {version}")

    has_builds = bool(builds)
    with output_path.open("a", encoding="utf-8", newline="\n") as output:
        output.write(f"builds={json.dumps(builds, separators=(',', ':'))}\n")
        output.write(f"releases={json.dumps(releases, separators=(',', ':'))}\n")
        output.write(f"has_builds={'true' if has_builds else 'false'}\n")

    if not has_builds:
        print("nothing to build")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001 - surface API errors in the workflow log
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
