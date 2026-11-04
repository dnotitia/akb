"""Marker evaluation in scripts/ci/check_image_lock.py follows PEP 508 / PEP 440.

The check decides which `uv export` lines apply to an image by evaluating each
line's environment marker against the image's own interpreter. A version
comparison that disagrees with PEP 440 either reports a correctly installed
package as "not locked" or skips a package the image should contain, so the
evaluator is compared here with `packaging`, the reference implementation that
pip uses (locked in the dev extra).
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pytest
from packaging.markers import Marker

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_DIR = REPO_ROOT / "scripts" / "ci"
sys.path.insert(0, str(CI_DIR))

from check_image_lock import evaluate_marker  # noqa: E402


def _env(python_version: str, python_full_version: str) -> dict[str, str]:
    return {
        "os_name": "posix",
        "sys_platform": "linux",
        "platform_machine": "x86_64",
        "platform_system": "Linux",
        "platform_python_implementation": "CPython",
        "implementation_name": "cpython",
        "python_version": python_version,
        "python_full_version": python_full_version,
    }


FINAL = _env("3.14", "3.14.0")
CANDIDATE = _env("3.14", "3.14.0rc1")


@pytest.mark.parametrize(
    ("marker", "env", "expected"),
    [
        # Release segments compare as if zero-padded.
        ("python_version >= '3.14.0'", FINAL, True),
        ("python_version == '3.14.0'", FINAL, True),
        ("python_full_version == '3.14'", FINAL, True),
        # `.*` is a prefix match on the release segment.
        ("python_full_version == '3.14.*'", FINAL, True),
        ("python_full_version != '3.14.*'", FINAL, False),
        # A release candidate sorts before its final release.
        ("python_full_version >= '3.14.0'", CANDIDATE, False),
        ("python_full_version >= '3.14.0rc1'", CANDIDATE, True),
        ("python_full_version == '3.14.*'", CANDIDATE, True),
    ],
)
def test_version_markers_follow_pep_440(marker: str, env: dict[str, str], expected: bool) -> None:
    assert evaluate_marker(marker, env) is expected
    assert Marker(marker).evaluate(env) is expected


_ENVS = [
    ("3.13", "3.13.9"),
    ("3.14", "3.14.0"),
    ("3.14", "3.14.0a1"),
    ("3.14", "3.14.0b3"),
    ("3.14", "3.14.0rc1"),
    ("3.14", "3.14.1"),
    ("3.14", "3.14.10"),
    ("3.15", "3.15.0a2"),
]
_SPECS = [
    "3",
    "3.13",
    "3.14",
    "3.14.0",
    "3.14.0.0",
    "3.14.1",
    "3.14.0a1",
    "3.14.0b3",
    "3.14.0rc1",
    "3.14.0rc2",
    "3.14.0.post1",
    "3.14.0.dev1",
    "3.15",
    "3.15.0a2",
]
_WILDCARDS = ["3.*", "3.14.*", "3.14.0.*", "3.14.1.*", "3.15.*", "3.1.*"]


def _differential_cases() -> list[tuple[str, tuple[str, str]]]:
    cases = []
    for variable, env in itertools.product(("python_version", "python_full_version"), _ENVS):
        for op, spec in itertools.product(("==", "!=", "<", "<=", ">", ">="), _SPECS):
            cases.append((f"{variable} {op} '{spec}'", env))
            cases.append((f"'{spec}' {op} {variable}", env))
        for op, spec in itertools.product(("==", "!="), _WILDCARDS):
            cases.append((f"{variable} {op} '{spec}'", env))
    return cases


def test_version_markers_agree_with_packaging() -> None:
    cases = _differential_cases()
    mismatches = []
    for marker, versions in cases:
        env = _env(*versions)
        ours, reference = evaluate_marker(marker, env), Marker(marker).evaluate(env)
        if ours is not reference:
            mismatches.append(f"{marker} with {versions}: {ours}, packaging {reference}")
    assert len(cases) == 2880
    assert mismatches == []


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ("sys_platform == 'linux' and python_full_version >= '3.14'", True),
        ("sys_platform == 'win32' or python_full_version < '3.14'", False),
        ("(python_version == '3.13.*' or python_version == '3.14.*') and platform_machine != 'arm64'", True),
        ("platform_machine in 'x86_64 aarch64'", True),
        ("platform_machine not in 'x86_64 aarch64'", False),
    ],
)
def test_compound_markers(marker: str, expected: bool) -> None:
    assert evaluate_marker(marker, FINAL) is expected
    assert Marker(marker).evaluate(FINAL) is expected


@pytest.mark.parametrize(
    "marker",
    [
        "python_version < '3.14.*'",  # a wildcard is defined for == and != only
        "python_full_version >= 'three'",  # not a PEP 440 version
        "python_full_version >= '3.14+local'",  # local versions are not emitted for markers
        "'3.14.*' == python_version",  # a wildcard on the candidate side
        "python_version ~= '3.14'",  # operator outside the subset uv emits
        "sys_platform < 'linux'",  # ordering on a string
        "extra == 'dev'",  # unknown variable
    ],
)
def test_unsupported_markers_are_refused(marker: str) -> None:
    with pytest.raises(ValueError):
        evaluate_marker(marker, FINAL)
