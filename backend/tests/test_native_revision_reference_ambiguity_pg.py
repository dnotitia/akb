"""PostgreSQL contract tests for fail-closed native Resource references."""

from __future__ import annotations

import importlib.util
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import pytest

from app.exceptions import ConflictError
from app.repositories.native_revision_repo import (
    NativeResourceReferenceAmbiguousError,
    NativeRevisionRepository,
)
from app.services.native_revision_service import NativeRevisionService


pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[1]
_INIT_SQL = (_BACKEND / "app" / "db" / "init.sql").read_text(encoding="utf-8")
_MIGRATION = _BACKEND / "app" / "db" / "migrations" / "048_native_revision_core.py"
_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:5433/akb",  # pragma: allowlist secret
)


async def _reachable() -> bool:
    try:
        conn = await asyncpg.connect(_DSN, timeout=2)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


def _database_dsn(name: str) -> str:
    return f"{_DSN.rsplit('/', 1)[0]}/{name}"


def _load_migration():
    spec = importlib.util.spec_from_file_location("native_reference_ambiguity_048", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@asynccontextmanager
async def _fresh_database():
    if not await _reachable():
        pytest.skip(f"Postgres not reachable at {_DSN}")

    name = f"akb_native_reference_ambiguity_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(_DSN)
    conn = None
    pool = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        conn = await asyncpg.connect(_database_dsn(name))
        await conn.execute(_INIT_SQL)
        await _load_migration().migrate(conn=conn)
        await conn.close()
        conn = None
        pool = await asyncpg.create_pool(_database_dsn(name), min_size=1, max_size=4)
        async with pool.acquire() as seeded:
            namespace_id = await seeded.fetchval(
                "INSERT INTO vaults (name, git_path) VALUES ($1, $2) RETURNING id",
                f"native-ambiguity-{uuid.uuid4().hex}",
                "/tmp/native-ambiguity-unused.git",
            )
        yield pool, namespace_id
    finally:
        if pool is not None:
            await pool.close()
        if conn is not None and not conn.is_closed():
            await conn.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


def _create_args(namespace_id: uuid.UUID, *, path: str, resource_id: uuid.UUID) -> dict:
    return {
        "namespace_id": namespace_id,
        "surface": "document",
        "path": path,
        "payload": f"body for {path}\n",
        "actor": "native-ambiguity-test",
        "mutation_id": uuid.uuid4(),
        "resource_id": resource_id,
    }


async def test_uuid_identity_and_live_path_fail_closed_for_reads_and_mutations():
    async with _fresh_database() as (pool, namespace_id):
        service = NativeRevisionService(pool)
        repository = NativeRevisionRepository(pool)
        resource_id = uuid.uuid4()
        await service.create_text(
            **_create_args(namespace_id, path="identity-owner.md", resource_id=resource_id)
        )
        path_owner = await service.create_text(
            **_create_args(namespace_id, path=str(resource_id), resource_id=uuid.uuid4())
        )

        with pytest.raises(NativeResourceReferenceAmbiguousError) as caught:
            await repository.resolve_live_reference(
                namespace_id=namespace_id,
                surface="document",
                reference=str(resource_id),
            )
        assert isinstance(caught.value, ConflictError)
        assert caught.value.status_code == 409
        assert caught.value.code == "native_resource_reference_ambiguous"

        with pytest.raises(NativeResourceReferenceAmbiguousError):
            await service.get_current_reference(
                namespace_id=namespace_id,
                surface="document",
                reference=str(resource_id),
            )

        before = await service.get_current_resource(
            namespace_id=namespace_id,
            surface="document",
            resource_id=resource_id,
        )
        with pytest.raises(NativeResourceReferenceAmbiguousError):
            await service.replace_text(
                namespace_id=namespace_id,
                surface="document",
                path=str(resource_id),
                payload="must not mutate\n",
                actor="native-ambiguity-test",
                mutation_id=uuid.uuid4(),
            )
        after = await service.get_current_resource(
            namespace_id=namespace_id,
            surface="document",
            resource_id=resource_id,
        )
        assert after.revision_id == before.revision_id
        assert path_owner.resource_id != resource_id


async def test_uuid_identity_and_live_alias_fail_closed_and_do_not_delete():
    async with _fresh_database() as (pool, namespace_id):
        service = NativeRevisionService(pool)
        repository = NativeRevisionRepository(pool)
        resource_id = uuid.uuid4()
        await service.create_text(
            **_create_args(namespace_id, path="identity-owner.md", resource_id=resource_id)
        )
        alias_owner = await service.create_text(
            **_create_args(namespace_id, path="alias-owner.md", resource_id=uuid.uuid4())
        )
        async with pool.acquire() as conn, conn.transaction():
            await repository.insert_path_alias(
                conn,
                namespace_id=namespace_id,
                surface="document",
                old_path=str(resource_id),
                resource_id=alias_owner.resource_id,
                created_revision_id=alias_owner.revision_id,
                occurred_at=alias_owner.occurred_at,
            )

        with pytest.raises(NativeResourceReferenceAmbiguousError):
            await repository.resolve_live_reference(
                namespace_id=namespace_id,
                surface="document",
                reference=str(resource_id),
            )

        with pytest.raises(NativeResourceReferenceAmbiguousError):
            await service.delete_resource(
                namespace_id=namespace_id,
                surface="document",
                path=str(resource_id),
                actor="native-ambiguity-test",
                mutation_id=uuid.uuid4(),
            )

        async with pool.acquire() as conn:
            lifecycle = await conn.fetchval(
                "SELECT lifecycle FROM native_resources WHERE resource_id = $1",
                resource_id,
            )
        assert lifecycle == "live"


async def test_uuid_shaped_path_falls_through_when_no_identity_matches():
    async with _fresh_database() as (pool, namespace_id):
        service = NativeRevisionService(pool)
        repository = NativeRevisionRepository(pool)
        uuid_path = str(uuid.uuid4())
        path_owner = await service.create_text(
            **_create_args(namespace_id, path=uuid_path, resource_id=uuid.uuid4())
        )

        resolved = await repository.resolve_live_reference(
            namespace_id=namespace_id,
            surface="document",
            reference=uuid_path,
        )
        assert resolved is not None
        assert resolved["resource_id"] == path_owner.resource_id


async def test_current_path_precedes_another_resources_old_alias():
    async with _fresh_database() as (pool, namespace_id):
        service = NativeRevisionService(pool)
        repository = NativeRevisionRepository(pool)
        current_owner = await service.create_text(
            **_create_args(namespace_id, path="shared.md", resource_id=uuid.uuid4())
        )
        alias_owner = await service.create_text(
            **_create_args(namespace_id, path="former-shared.md", resource_id=uuid.uuid4())
        )
        async with pool.acquire() as conn, conn.transaction():
            await repository.insert_path_alias(
                conn,
                namespace_id=namespace_id,
                surface="document",
                old_path="shared.md",
                resource_id=alias_owner.resource_id,
                created_revision_id=alias_owner.revision_id,
                occurred_at=alias_owner.occurred_at,
            )

        resolved = await repository.resolve_live_reference(
            namespace_id=namespace_id,
            surface="document",
            reference="shared.md",
        )
        assert resolved is not None
        assert resolved["resource_id"] == current_owner.resource_id
