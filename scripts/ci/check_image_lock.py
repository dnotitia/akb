#!/usr/bin/env python3
"""Fail when a backend image's installed packages differ from backend/uv.lock.

The backend image installs its third-party packages from the lock, so the
image runs the versions CI tested. This check reads what an image actually
contains and compares it with what the lock selects for that image's
platform. An install that resolves on its own picks whatever is newest on the
build day; this reports every package where that differs, by name.

usage: python3 scripts/ci/check_image_lock.py IMAGE [--project backend]

Needs `docker` and `uv` on PATH; the image is run with its own `python`.
Exit 0: identical. Exit 1: drift, one line per package. Exit 2: the check
could not run (missing tool, unreadable export, marker it cannot evaluate).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

# The project itself is installed from the source tree, not from the lock.
PROJECT = "akb"
# The base image's own installer; the lock does not and should not list it.
BASE_IMAGE_TOOLS = {"pip"}

PROBE = r"""
import importlib.metadata, json, os, platform, sys
env = {
    "os_name": os.name,
    "sys_platform": sys.platform,
    "platform_machine": platform.machine(),
    "platform_system": platform.system(),
    "platform_python_implementation": platform.python_implementation(),
    "implementation_name": sys.implementation.name,
    "python_version": "%d.%d" % sys.version_info[:2],
    "python_full_version": platform.python_version(),
}
dists = {}
for dist in importlib.metadata.distributions():
    dists.setdefault(dist.metadata["Name"], dist.version)
print(json.dumps({"env": env, "dists": dists}))
"""

_TOKEN = re.compile(r"\s*(\(|\)|'[^']*'|\"[^\"]*\"|==|!=|<=|>=|<|>|not in\b|in\b|and\b|or\b|[a-z_]+)")
_VERSION_VARS = {"python_version", "python_full_version"}


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _version(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", value))


def evaluate_marker(marker: str, env: dict[str, str]) -> bool:
    """Evaluate the PEP 508 marker subset `uv export` emits; refuse anything else."""
    tokens: list[str] = []
    pos = 0
    while pos < len(marker):
        match = _TOKEN.match(marker, pos)
        if not match:
            raise ValueError(f"cannot parse marker: {marker!r}")
        tokens.append(match.group(1))
        pos = match.end()
        while pos < len(marker) and marker[pos].isspace():
            pos += 1

    def take() -> str:
        if not tokens:
            raise ValueError(f"truncated marker: {marker!r}")
        return tokens.pop(0)

    def operand(token: str) -> tuple[str, str]:
        if token[0] in "'\"":
            return "literal", token[1:-1]
        if token not in env:
            raise ValueError(f"unknown marker variable {token!r} in {marker!r}")
        return token, env[token]

    def compare() -> bool:
        token = take()
        if token == "(":
            result = disjunction()
            if take() != ")":
                raise ValueError(f"unbalanced marker: {marker!r}")
            return result
        left_name, left = operand(token)
        op = take()
        right_name, right = operand(take())
        if op in ("in", "not in"):
            return (left in right) == (op == "in")
        if left_name in _VERSION_VARS or right_name in _VERSION_VARS:
            a, b = _version(left), _version(right)
        else:
            if op not in ("==", "!="):
                raise ValueError(f"ordering on a string in {marker!r}")
            a, b = left, right  # type: ignore[assignment]
        return {"==": a == b, "!=": a != b, "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]

    def conjunction() -> bool:
        result = compare()
        while tokens and tokens[0] == "and":
            take()
            result = compare() and result
        return result

    def disjunction() -> bool:
        result = conjunction()
        while tokens and tokens[0] == "or":
            take()
            result = conjunction() or result
        return result

    result = disjunction()
    if tokens:
        raise ValueError(f"trailing tokens in marker: {marker!r}")
    return result


def locked_versions(project: str, env: dict[str, str]) -> dict[str, str]:
    export = subprocess.run(
        ["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--no-hashes",
         "--no-header", "--no-annotate", "--project", project],
        check=True, capture_output=True, text=True,
    ).stdout
    locked: dict[str, str] = {}
    for line in export.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        requirement, _, marker = line.partition(";")
        name, sep, version = requirement.strip().partition("==")
        if not sep:
            raise ValueError(f"export line is not an exact pin: {line!r}")
        if marker.strip() and not evaluate_marker(marker.strip(), env):
            continue
        locked[normalize(name)] = version.strip()
    return locked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("image")
    parser.add_argument("--project", default="backend", help="directory holding pyproject.toml and uv.lock")
    args = parser.parse_args()
    try:
        probe = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", "python", args.image, "-I", "-c", PROBE],
            check=True, capture_output=True, text=True,
        )
        image = json.loads(probe.stdout)
        locked = locked_versions(args.project, image["env"])
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError) as exc:
        detail = getattr(exc, "stderr", None) or exc
        print(f"check_image_lock: cannot compare: {detail}", file=sys.stderr)
        return 2

    installed = {normalize(n): v for n, v in image["dists"].items()}
    installed.pop(PROJECT, None)
    drift = []
    for name in sorted(set(locked) | (set(installed) - BASE_IMAGE_TOOLS)):
        have, want = installed.get(name), locked.get(name)
        if have != want:
            drift.append(f"  {name}: image {have or 'not installed'}, lock {want or 'not locked'}")
    if drift:
        print(f"{args.image}: {len(drift)} package(s) differ from {args.project}/uv.lock:")
        print("\n".join(drift))
        return 1
    print(f"{args.image}: all {len(locked)} locked packages installed at the locked version, nothing else")
    return 0


if __name__ == "__main__":
    sys.exit(main())
