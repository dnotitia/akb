"""Static guards for the v2-only app release schema migration."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]
MIGRATION = ROOT / "app" / "db" / "migrations" / "095_app_release_manifest_v2.py"
POSTGRES = ROOT / "app" / "db" / "postgres.py"


def test_manifest_v2_migration_is_registered_and_replaces_v1_checks() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    registry = POSTGRES.read_text(encoding="utf-8")

    assert '"095_app_release_manifest_v2.py"' in registry
    assert "DROP CONSTRAINT IF EXISTS app_releases_manifest_shape" in source
    assert "manifest->>'manifest_version' = '2'" in source
    assert "manifest->>'image_digest' ~ '^sha256:[0-9a-f]{64}$'" in source
    assert "manifest->>'source_revision' ~ '^[0-9A-Fa-f]{40,64}$'" in source
    assert "akb_check_release_manifest_app_key" in source
