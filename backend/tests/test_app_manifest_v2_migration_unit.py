"""Static guards for the v2-only app release schema migration."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
MIGRATION = ROOT / "app" / "db" / "migrations" / "095_app_release_manifest_v2.py"
POSTGRES = ROOT / "app" / "db" / "postgres.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("manifest_v2_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_v2_migration_is_registered_and_replaces_v1_checks() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    registry = POSTGRES.read_text(encoding="utf-8")

    assert '"095_app_release_manifest_v2.py"' in registry
    assert "DROP CONSTRAINT IF EXISTS app_releases_manifest_shape" in source
    assert "manifest->>'manifest_version' = '2'" in source
    assert "manifest->>'image_digest' ~ '^sha256:[0-9a-f]{64}$'" in source
    assert "manifest->>'source_revision' ~ '^[0-9A-Fa-f]{40,64}$'" in source
    assert "akb_check_release_manifest_app_key" in source


@pytest.mark.asyncio
async def test_manifest_v2_migration_is_noop_when_release_table_is_absent() -> None:
    class _Transaction:
        async def __aenter__(self):
            raise AssertionError("absent-table migration must not enter a transaction")

        async def __aexit__(self, *_args):
            return False

    class _Connection:
        def transaction(self):
            return _Transaction()

        async def fetchval(self, query: str):
            assert query == "SELECT to_regclass('public.app_releases')"
            return None

    await _load_migration()._run(_Connection())
