"""Real lifecycle.init_storage coverage for revision-backend Git composition."""

from __future__ import annotations

import uuid

import pytest

from app.config import Settings


_DATABASE_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
_IMAGE_DIGEST = "sha256:" + "a" * 64


def _settings(tmp_path, backend: str | None = None) -> Settings:
    values = {"git_storage_path": str(tmp_path / "vaults")}
    if backend is not None:
        values["document_revision_backend"] = backend
    if backend == "postgres_native":
        values.update(
            {
                "document_revision_tenant_id": "tenant-001",
                "document_revision_namespace": "tenant-001",
                "document_revision_database_id": _DATABASE_ID,
                "document_revision_runtime_image_digest": _IMAGE_DIGEST,
            }
        )
    return Settings(**values)


def _stub_init_storage_dependencies(monkeypatch, lifecycle, settings, events: list[str]) -> None:
    monkeypatch.setattr(lifecycle, "settings", settings)
    monkeypatch.setattr(lifecycle, "_validate_required_settings", lambda: None)

    async def no_op() -> None:
        pass

    async def authority_preflight() -> str:
        events.append("authority_preflight")
        return "ready"

    monkeypatch.setattr(lifecycle, "pre_migration_revision_authority_guard", no_op)
    monkeypatch.setattr(lifecycle, "init_db", no_op)
    monkeypatch.setattr(lifecycle, "startup_revision_authority_preflight", authority_preflight)

    class _VectorStore:
        async def ensure_collection(self) -> None:
            events.append("vector_store.ensure_collection")

    monkeypatch.setattr(lifecycle, "get_vector_store", lambda: _VectorStore())

    class _Pool:
        pass

    pool = _Pool()

    async def get_pool():
        events.append("get_pool")
        return pool

    monkeypatch.setattr(lifecycle, "get_pool", get_pool)

    class _RoleSync:
        def __init__(self, received_pool) -> None:
            assert received_pool is pool
            events.append("RoleSync")

        async def reconcile_from_catalog(self) -> dict[str, int]:
            events.append("RoleSync.reconcile_from_catalog")
            return {"errors": 0}

    monkeypatch.setattr(lifecycle, "RoleSync", _RoleSync)
    monkeypatch.setattr(lifecycle, "UserSqlExecutor", lambda received_pool: received_pool)
    monkeypatch.setattr(lifecycle, "set_role_sync", lambda role_sync: None)
    monkeypatch.setattr(lifecycle, "set_user_sql_executor", lambda executor: None)


@pytest.mark.parametrize("configured_backend", [None, "bare_git"])
async def test_init_storage_default_and_explicit_bare_git_clean_stale_locks(
    monkeypatch, tmp_path, configured_backend
):
    from app.services import lifecycle
    from app.services.revision_backend import canonical_document_revision_backend

    configured = _settings(tmp_path, configured_backend)
    selected_backend = canonical_document_revision_backend(configured.document_revision_backend)
    assert selected_backend == "bare_git"
    events: list[str] = []
    _stub_init_storage_dependencies(monkeypatch, lifecycle, configured, events)
    monkeypatch.setattr(lifecycle, "selected_document_revision_backend", lambda: selected_backend)

    class _TrackingGit:
        constructed = 0
        cleanups = 0

        def __init__(self) -> None:
            type(self).constructed += 1

        def cleanup_stale_locks(self) -> int:
            type(self).cleanups += 1
            return 0

    monkeypatch.setattr(lifecycle, "GitService", _TrackingGit)

    await lifecycle.init_storage()

    assert _TrackingGit.constructed == 1
    assert _TrackingGit.cleanups == 1
    assert "RoleSync.reconcile_from_catalog" in events


async def test_init_storage_postgres_native_never_constructs_git(monkeypatch, tmp_path):
    from app.services import lifecycle
    from app.services.revision_backend import canonical_document_revision_backend

    configured = _settings(tmp_path, "postgres_native")
    selected_backend = canonical_document_revision_backend(configured.document_revision_backend)
    assert selected_backend == "postgres_native"
    events: list[str] = []
    _stub_init_storage_dependencies(monkeypatch, lifecycle, configured, events)
    monkeypatch.setattr(lifecycle, "selected_document_revision_backend", lambda: selected_backend)

    constructed: list[int] = []

    class _FailingGit:
        def __init__(self) -> None:
            constructed.append(1)
            raise AssertionError("postgres_native init_storage must not construct GitService")

    monkeypatch.setattr(lifecycle, "GitService", _FailingGit)

    await lifecycle.init_storage()

    assert constructed == []
    assert "RoleSync.reconcile_from_catalog" in events
