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

It also enforces which test outcomes are required by the two backend CI jobs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.helpers.required_outcomes import (
    _fail_on_skip,
    pytest_make_collect_report as pytest_make_collect_report,
    pytest_runtest_makereport as pytest_runtest_makereport,
)
from tests.helpers.auth_database import (
    admin_auth_template as admin_auth_template,
    auth_schema_template as auth_schema_template,
    browser_auth_template as browser_auth_template,
    identity_operations_template as identity_operations_template,
    operation_requests_template as operation_requests_template,
)
from tests.helpers.file_database import file_capability_schema_template as file_capability_schema_template
from tests.helpers.native_revision_database import (
    derived_pipeline_template as derived_pipeline_template,
    native_template as native_template,
)
from tests.helpers.pg_database import (
    _local_audit_application_schema_template as _local_audit_application_schema_template,
    audit_application_schema_template as audit_application_schema_template,
    application_schema_template as application_schema_template,
    base_schema_template as base_schema_template,
)
from tests.helpers.vector_database import (
    compaction_template as compaction_template,
    corpus_template as corpus_template,
    empty_vchord_template as empty_vchord_template,
)

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


_NEEDS_RESOURCE = "needs_resource"
_DESELECTED_RESOURCES = pytest.StashKey[dict[str, int]]()


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


def pytest_sessionfinish(session):
    config = session.config
    if (workeroutput := getattr(config, "workeroutput", None)) is not None:
        workeroutput["akb_deselected_resources"] = config.stash.get(_DESELECTED_RESOURCES, {})


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error):
    if error is None:
        # Each worker collects the same tests. Use one report so the counts
        # describe the test suite once.
        node.config.stash.setdefault(
            _DESELECTED_RESOURCES, node.workeroutput["akb_deselected_resources"],
        )


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    counts = config.stash.get(_DESELECTED_RESOURCES, {})
    if _fail_on_skip() and counts:
        terminalreporter.section(f"deselected tests that declare {_NEEDS_RESOURCE}")
        for resource, count in sorted(counts.items()):
            terminalreporter.line(f"{count:5d}  {resource}")
