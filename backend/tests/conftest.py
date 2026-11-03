"""Test bootstrap: ensure `app.config` can import in any environment.

`app.config` loads `./config/app.yaml` at module import time (CWD-relative).
When pytest is invoked as `cd backend && uv run pytest`, the CWD is
`backend/` and `./config` resolves to `backend/config` — which doesn't
exist by default. Without a config file present, importing anything that
transitively imports `app.config` (e.g. `app.services.git_service`)
raises RuntimeError at collection time.

This conftest materialises a minimal `backend/config/app.yaml` from the
tracked example *only if* one isn't already present. It does not
overwrite an existing config. Tests still pass their own paths/values
into service constructors — settings here exist only to satisfy module
import.

It also owns the live-PG lane's skip rule (see `_FAIL_ON_SKIP_ENV`).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _BACKEND_DIR.parent

_BACKEND_CFG_DIR = _BACKEND_DIR / "config"
_BACKEND_APP_YAML = _BACKEND_CFG_DIR / "app.yaml"
_BACKEND_LOCAL_KEY_DIR = _BACKEND_CFG_DIR / "local-session"

_EXAMPLE_APP_YAML = _REPO_ROOT / "config" / "app.yaml.example"

if not _BACKEND_APP_YAML.exists() and _EXAMPLE_APP_YAML.exists():
    _BACKEND_CFG_DIR.mkdir(parents=True, exist_ok=True)
    bootstrap_config = yaml.safe_load(_EXAMPLE_APP_YAML.read_text())
    # Collection exercises legacy Git services. Pin only this import fixture;
    # Settings defaults and isolated Native subprocess tests stay untouched.
    bootstrap_config["document_revision_backend"] = "bare_git"
    # The example says `auto`, which startup decides against a database before
    # building the vector store; importing for collection starts none. Tests
    # that read the process-global shape get a named one, and the tests of
    # `auto` build their own Settings.
    bootstrap_config["vector_store_sparse_shape"] = "posting"
    bootstrap_config["local_session_private_key_path"] = str(
        _BACKEND_LOCAL_KEY_DIR / "private.pem"
    )
    bootstrap_config["local_session_jwks_path"] = str(
        _BACKEND_LOCAL_KEY_DIR / "jwks.json"
    )
    _BACKEND_APP_YAML.write_text(
        yaml.safe_dump(bootstrap_config, sort_keys=False),
        encoding="utf-8",
    )

if not _BACKEND_LOCAL_KEY_DIR.exists():
    # This module has no app.config import side effect, so the key fixture can
    # be created before test collection imports the process-global settings.
    from app.services.local_session_keys import generate_local_session_keyset

    generate_local_session_keyset(_BACKEND_LOCAL_KEY_DIR)


@pytest.fixture
def git_http():
    """A running in-process smart-HTTP git server (external-git runner
    tests). Serves bare repos over ``git http-backend``; torn down after the
    test. See ``tests.extgit_http``."""
    from tests.extgit_http import GitHttpFixture

    fx = GitHttpFixture()
    try:
        yield fx
    finally:
        fx.close()


# ── The live-PG lane's skip rule ─────────────────────────────────────────────
#
# The live-PG CI job runs the whole backend suite except the tests that declare
# `@pytest.mark.needs_resource("...")`, and it sets AKB_TEST_FAIL_ON_SKIP=1.
# Under that flag a skip is a failure. A test that skips there reads as green
# while it gates nothing, which is how PostgreSQL-backed tests used to run in
# no job at all. An expected failure (xfail, xpass) is treated the same way:
# its result gates nothing either.

_FAIL_ON_SKIP_ENV = "AKB_TEST_FAIL_ON_SKIP"
_NEEDS_RESOURCE = "needs_resource"
_HOW_TO_FIX = (
    "Make it run in this lane, or declare what it needs with "
    f'@pytest.mark.{_NEEDS_RESOURCE}("<the resource and how to provide it>"), '
    "which this lane deselects."
)
_DESELECTED_RESOURCES = pytest.StashKey[dict[str, int]]()


def _fail_on_skip() -> bool:
    return os.environ.get(_FAIL_ON_SKIP_ENV) == "1"


def _skip_reason(longrepr: object) -> str:
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2])
    return str(longrepr)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if not _fail_on_skip():
        return report
    if hasattr(report, "wasxfail"):
        what = f"is marked as an expected failure ({report.wasxfail or 'xfail'})"
        del report.wasxfail
    elif report.skipped:
        what = f"skipped ({_skip_reason(report.longrepr)})"
    else:
        return report
    report.outcome = "failed"
    report.longrepr = (
        f"{item.nodeid} {what}, and {_FAIL_ON_SKIP_ENV}=1 makes that a "
        f"failure. {_HOW_TO_FIX}"
    )
    return report


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_make_collect_report(collector):
    report = yield
    if _fail_on_skip() and report.skipped:
        report.outcome = "failed"
        report.longrepr = (
            f"{collector.nodeid} skipped while it was collected "
            f"({_skip_reason(report.longrepr)}), and {_FAIL_ON_SKIP_ENV}=1 "
            "makes that a failure. A module that skips on import cannot be "
            "deselected by a marker, so move that check into a fixture first. "
            + _HOW_TO_FIX
        )
    return report


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    # A declaration is the only record of why a test is absent from the lane,
    # so it must say what the test needs.
    malformed = sorted(
        {
            item.nodeid
            for item in items
            for mark in item.iter_markers(_NEEDS_RESOURCE)
            if mark.kwargs
            or len(mark.args) != 1
            or not isinstance(mark.args[0], str)
            or not mark.args[0].strip()
        }
    )
    if malformed:
        raise pytest.UsageError(
            f"@pytest.mark.{_NEEDS_RESOURCE} takes exactly one non-empty "
            "string naming the resource; malformed on: " + ", ".join(malformed)
        )


def pytest_deselected(items):
    for item in items:
        mark = item.get_closest_marker(_NEEDS_RESOURCE)
        if mark is not None:
            counts = item.config.stash.setdefault(_DESELECTED_RESOURCES, {})
            counts[mark.args[0]] = counts.get(mark.args[0], 0) + 1


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    counts = config.stash.get(_DESELECTED_RESOURCES, {})
    if _fail_on_skip() and counts:
        terminalreporter.section(f"deselected tests that declare {_NEEDS_RESOURCE}")
        for resource, count in sorted(counts.items()):
            terminalreporter.line(f"{count:5d}  {resource}")
