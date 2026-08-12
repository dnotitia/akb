"""PostgreSQL contract tests for public Native Revision selectors."""

from __future__ import annotations

import importlib.util
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import pytest

from app.exceptions import ConflictError, NotFoundError, ValidationError
from app.services.native_revision_backend import NativeRevisionBackend
from app.services.native_document_service import NativeDocumentService
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
    spec = importlib.util.spec_from_file_location("native_public_selector_048", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@asynccontextmanager
async def _fresh_database():
    if not await _reachable():
        pytest.skip(f"Postgres not reachable at {_DSN}")

    name = f"akb_native_public_selector_{uuid.uuid4().hex[:12]}"
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
        vault_name = f"native-selector-{uuid.uuid4().hex}"
        async with pool.acquire() as seeded:
            namespace_id = await seeded.fetchval(
                "INSERT INTO vaults (name, git_path) VALUES ($1, $2) RETURNING id",
                vault_name,
                "/tmp/native-selector-unused.git",
            )
        yield pool, namespace_id, vault_name
    finally:
        if pool is not None:
            await pool.close()
        if conn is not None and not conn.is_closed():
            await conn.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def test_public_historical_selector_is_exact_or_resource_scoped_prefix(monkeypatch):
    revision_ids = iter(
        (
            "abcdef0" + "1" * 33,
            "abcdef0" + "2" * 33,
            "1234567" + "3" * 33,
        )
    )
    monkeypatch.setattr(
        NativeRevisionService,
        "_opaque_revision_id",
        staticmethod(lambda: next(revision_ids)),
    )

    async with _fresh_database() as (pool, namespace_id, vault_name):
        service = NativeRevisionService(pool)
        first = await service.create_text(
            namespace_id=namespace_id,
            surface="document",
            path="one.md",
            payload="first\n",
            actor="selector-test",
            mutation_id=uuid.uuid4(),
            resource_id=uuid.uuid4(),
        )
        await service.replace_text(
            namespace_id=namespace_id,
            surface="document",
            path="one.md",
            payload="second\n",
            actor="selector-test",
            mutation_id=uuid.uuid4(),
            expected_revision_id=first.revision_id,
        )
        await service.create_text(
            namespace_id=namespace_id,
            surface="document",
            path="two.md",
            payload="other\n",
            actor="selector-test",
            mutation_id=uuid.uuid4(),
            resource_id=uuid.uuid4(),
        )

        backend = NativeRevisionBackend(pool=pool)
        exact = await backend.document_version(vault_name, "one.md", first.revision_id)
        assert exact is not None
        assert exact[1] == "first\n"

        prefix = first.revision_id[:8]
        selected = await backend.document_version(vault_name, "one.md", prefix)
        assert selected is not None
        assert selected[1] == "first\n"

        document_service = NativeDocumentService(pool=pool)
        rest_exact = await document_service.get_at_commit(vault_name, "one.md", first.revision_id)
        assert rest_exact.content == "first"
        assert rest_exact.current_commit == first.revision_id
        rest_selected = await document_service.get_at_commit(vault_name, "one.md", first.revision_id[:8])
        assert rest_selected.content == "first"
        assert rest_selected.current_commit == prefix

        NativeRevisionService._validate_expected_revision(first.revision_id)
        with pytest.raises(ValidationError):
            NativeRevisionService._validate_expected_revision(first.revision_id[:8])

        assert await backend.document_version(vault_name, "one.md", "deadbee") is None
        assert await backend.document_version(vault_name, "two.md", prefix) is None

        with pytest.raises(NotFoundError):
            await document_service.get_at_commit(vault_name, "one.md", "deadbee")
        with pytest.raises(NotFoundError):
            await document_service.get_at_commit(vault_name, "two.md", prefix)

        diff = await backend.document_diff(vault_name, "one.md", prefix)
        assert diff is not None
        assert diff["type"] == "added"
        assert "+first" in diff["diff"]

        with pytest.raises(ConflictError) as caught:
            await backend.document_version(vault_name, "one.md", first.revision_id[:7])
        assert caught.value.code == "native_revision_selector_ambiguous"

        with pytest.raises(ConflictError) as caught:
            await backend.document_diff(vault_name, "one.md", first.revision_id[:7])
        assert caught.value.code == "native_revision_selector_ambiguous"

        with pytest.raises(ConflictError) as caught:
            await document_service.get_at_commit(vault_name, "one.md", first.revision_id[:7])
        assert caught.value.code == "native_revision_selector_ambiguous"
