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


# PEP 440 public versions (no local segment), as `packaging` spells the pattern.
_PEP440 = re.compile(
    r"""v?(?:(?P<epoch>\d+)!)?(?P<release>\d+(?:\.\d+)*)
    (?:[-_.]?(?P<pre_l>alpha|a|beta|b|preview|pre|c|rc)[-_.]?(?P<pre_n>\d+)?)?
    (?:-(?P<post_n1>\d+)|[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n2>\d+)?)?
    (?:[-_.]?(?P<dev_l>dev)[-_.]?(?P<dev_n>\d+)?)?""",
    re.VERBOSE | re.IGNORECASE,
)
_PRE_RANK = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "rc": 2, "pre": 2, "preview": 2}


def _pep440(value: str) -> tuple[int, tuple[int, ...], tuple[int, int] | None, int | None, int | None]:
    """Parse a version into (epoch, release, pre, post, dev); refuse anything else."""
    match = _PEP440.fullmatch(value.strip())
    if not match:
        raise ValueError(f"not a PEP 440 version: {value!r}")
    pre = None
    if match["pre_l"]:
        pre = (_PRE_RANK[match["pre_l"].lower()], int(match["pre_n"] or 0))
    post = None
    if match["post_n1"] or match["post_l"]:
        post = int(match["post_n1"] or match["post_n2"] or 0)
    dev = int(match["dev_n"] or 0) if match["dev_l"] else None
    release = tuple(int(part) for part in match["release"].split("."))
    return int(match["epoch"] or 0), release, pre, post, dev


def _base(version: tuple) -> tuple[int, tuple[int, ...]]:
    """Epoch and release with trailing zeros dropped: 3.14 and 3.14.0 are one version."""
    release = list(version[1])
    while release and release[-1] == 0:
        release.pop()
    return version[0], tuple(release)


def _sort_key(version: tuple) -> tuple:
    _, _, pre, post, dev = version
    if pre is None:
        # A dev-only release sorts before its pre-releases, a final one after them.
        pre = (-1, 0) if post is None and dev is not None else (3, 0)
    return (*_base(version), pre, -1 if post is None else post, (1, 0) if dev is None else (0, dev))


def _version_holds(candidate: str, op: str, spec: str) -> bool:
    """`candidate op spec` under PEP 440, as `packaging` (pip's marker evaluator) decides it.

    Stdlib only, so the check runs on any `python3` without `packaging`
    installed; backend/tests/test_check_image_lock_unit.py holds it to
    `packaging`. A comparison it cannot decide raises ValueError (exit 2).
    """
    have = _pep440(candidate)
    if spec.endswith(".*"):
        if op not in ("==", "!=") or not re.fullmatch(r"\d+(?:\.\d+)*", spec[:-2]):
            raise ValueError(f"invalid wildcard comparison {op} {spec!r}")
        prefix = tuple(int(part) for part in spec[:-2].split("."))
        matches = have[0] == 0 and (have[1] + (0,) * len(prefix))[: len(prefix)] == prefix
        return matches == (op == "==")
    want = _pep440(spec)
    a, b = _sort_key(have), _sort_key(want)
    if op == "<":
        # `< V` excludes pre-releases of V itself (anything from V.dev0 up)
        # unless V is a pre-release.
        is_pre = have[2] is not None or have[4] is not None
        if is_pre and want[2] is None and want[4] is None and a >= _sort_key((*want[:4], 0)):
            return False
        return a < b
    if op == ">":
        # `> V` excludes post-releases of V itself unless V is a post-release.
        if have[3] is not None and want[3] is None and _sort_key((*have[:3], None, None)) == b:
            return False
        return a > b
    return {"==": a == b, "!=": a != b, "<=": a <= b, ">=": a >= b}[op]


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
            # As in `packaging`, the right operand is the specifier.
            return _version_holds(left, op, right)
        if op not in ("==", "!="):
            raise ValueError(f"ordering on a string in {marker!r}")
        return (left == right) == (op == "==")

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
