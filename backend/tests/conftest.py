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
"""

from __future__ import annotations

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
