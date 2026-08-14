"""Static migration guards for the immutable adoption ledger."""

from __future__ import annotations

from pathlib import Path

from app.db import postgres


MIGRATION = Path(__file__).parents[1] / "app" / "db" / "migrations" / "077_legacy_adoptions.py"


def test_adoption_migration_is_registered_and_additive() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    registry = Path(postgres.__file__).read_text(encoding="utf-8")
    assert '"077_legacy_adoptions.py"' in registry
    assert "CREATE TABLE IF NOT EXISTS app_legacy_adoption_plans" in source
    assert "CREATE TABLE IF NOT EXISTS app_legacy_adoption_targets" in source
    assert "CREATE TABLE IF NOT EXISTS app_legacy_adoption_audit" in source
    assert "ON DELETE RESTRICT" in source
    assert "legacy adoption plan identity is immutable" in source
    assert "legacy adoption target identity is immutable" in source
    assert "legacy adoption ledger rows are retained" in source
    assert "app_legacy_adoption_audit_no_mutation" in source
    assert "DROP TABLE" not in source.upper()
    assert "ALTER TABLE vault_tables" not in source


def test_adoption_audit_actions_and_bounded_shapes_are_closed() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    for action in (
        "plan_created",
        "plan_replayed",
        "target_applied",
        "target_replayed",
        "target_blocked",
        "resource_adopted",
        "ownership_denied",
    ):
        assert f"'{action}'" in source
    assert "jsonb_typeof(checkpoint) = 'object'" in source
    assert "jsonb_typeof(planned_metadata) = 'object'" in source
