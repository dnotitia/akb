#!/usr/bin/env python3
"""Choose PR packaging checks; main and manual runs build every application image.

PostgreSQL is built and exercised by the existing live-DB and runtime E2E jobs.
This selector only removes redundant builds, never selects application tests.
"""

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys


IMAGES = ("backend", "frontend", "all-in-one")


def select_images(paths: list[str]) -> set[str]:
    selected: set[str] = set()
    for path in paths:
        if path in {".dockerignore", ".github/workflows/images.yml", "scripts/ci/image_scope.py"}:
            return set(IMAGES)
        if path.startswith("backend/tests/") or path == "backend/CHANGELOG.md":
            continue
        if path.startswith("backend/"):
            selected.update(("backend", "all-in-one"))
        elif path.startswith("frontend/"):
            selected.update(("frontend", "all-in-one"))
        elif path.startswith("deploy/all-in-one/"):
            selected.add("all-in-one")
        elif path.startswith((
            "docs/", "packages/", "config/", "eval/", ".agents/", ".claude/", ".codex/", ".github/",
            "deploy/postgres/", "deploy/k8s/", "deploy/helm/", "deploy/keycloak-dev/",
        )) or ("/" not in path and path.endswith(".md")):
            continue
        else:
            # New inputs are checked until their consumers are classified.
            return set(IMAGES)
    return selected


def changed_paths(base: str, head: str) -> list[str] | None:
    if not all(re.fullmatch(r"[0-9a-f]{40}", revision) for revision in (base, head)):
        return None
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--no-renames", "-z", f"{base}...{head}", "--"],
            check=True, capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    # Disable rename detection so a move out of a build context retains the
    # deleted path as well. NUL delimiters preserve spaces/newlines in names.
    return [path.decode("utf-8", errors="surrogateescape") for path in result.stdout.split(b"\0") if path]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", required=True)
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    paths = changed_paths(args.base, args.head) if args.event == "pull_request" else None
    selected = set(IMAGES) if paths is None else select_images(paths)
    if args.event == "pull_request" and paths is None:
        print("PR diff unavailable; checking every application image.", file=sys.stderr)
    print(json.dumps(sorted(selected)))
    if args.github_output:
        with args.github_output.open("a") as output:
            for name in IMAGES:
                output.write(f"{name}={str(name in selected).lower()}\n")
            output.write(f"any={str(bool(selected)).lower()}\n")


if __name__ == "__main__":
    main()
