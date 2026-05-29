from __future__ import annotations

from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]


def test_init_schema_declares_document_and_file_hash_projection() -> None:
    init_sql = (BACKEND / "app/db/init.sql").read_text()

    assert "content_hash TEXT" in init_sql
    assert "hash_algorithm TEXT" in init_sql
    assert "content_hash_commit TEXT" in init_sql
    assert "hash_verified_at TIMESTAMPTZ" in init_sql
    assert "storage_version TEXT" in init_sql
    assert "etag TEXT" in init_sql


def test_migration_runner_includes_resource_hash_projection_migration() -> None:
    postgres_py = (BACKEND / "app/db/postgres.py").read_text()

    assert "030_resource_content_hash.py" in postgres_py
    assert (BACKEND / "app/db/migrations/030_resource_content_hash.py").exists()
