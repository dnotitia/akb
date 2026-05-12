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

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _BACKEND_DIR.parent

_BACKEND_CFG_DIR = _BACKEND_DIR / "config"
_BACKEND_APP_YAML = _BACKEND_CFG_DIR / "app.yaml"

_EXAMPLE_APP_YAML = _REPO_ROOT / "config" / "app.yaml.example"

if not _BACKEND_APP_YAML.exists() and _EXAMPLE_APP_YAML.exists():
    _BACKEND_CFG_DIR.mkdir(parents=True, exist_ok=True)
    _BACKEND_APP_YAML.write_text(_EXAMPLE_APP_YAML.read_text())
