"""Real PostgreSQL document Head parity, aliases, and read-only boundary."""

from __future__ import annotations

from contextlib import asynccontextmanager
import os
from pathlib import Path
import re
import uuid

import asyncpg
import pytest

from app.db import postgres
from app.exceptions import NotFoundError
from app.models.document import DocumentPutRequest, DocumentUpdateRequest
from app.repositories.document_repo import CollectionRepository, DocumentRepository
from app.repositories.native_revision_repo import NativeResourceReferenceAmbiguousError
from app.repositories.vault_repo import VaultRepository
from app.services import document_service
from app.services.document_service import DocumentService
from app.services.git_service import GitService
from app.services.native_document_service import NativeDocumentService


pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[1]
_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",  # pragma: allowlist secret
)


@asynccontextmanager
async def _fresh_database(monkeypatch):
    try:
        admin = await asyncpg.connect(_DSN, timeout=2)
    except (OSError, asyncpg.PostgresError):
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail("Required PostgreSQL is not reachable for document Head tests")
        pytest.skip("PostgreSQL is not reachable for document Head tests")
    database_name = f"akb_document_head_{uuid.uuid4().hex[:12]}"
    test_dsn = f"{_DSN.rsplit('/', 1)[0]}/{database_name}"
    pool = None
    read_pool = None
    try:
        await admin.execute(f'CREATE DATABASE "{database_name}"')
        conn = await asyncpg.connect(test_dsn)
        try:
            await postgres._run_boot_schema(
                conn, init_sql=(_BACKEND / "app" / "db" / "init.sql").read_text(),
            )
        finally:
            await conn.close()
        pool = await asyncpg.create_pool(test_dsn, min_size=1, max_size=4)
        queries: list[str] = []

        async def capture_queries(conn):
            conn.add_query_logger(lambda entry: queries.append(entry.query))

        read_pool = await asyncpg.create_pool(
            test_dsn, min_size=1, max_size=1, init=capture_queries,
            server_settings={"default_transaction_read_only": "on"},
        )
        monkeypatch.setattr(document_service, "get_pool", lambda: _pool_result(pool))
        yield pool, read_pool, queries
    finally:
        if read_pool is not None:
            await read_pool.close()
        if pool is not None:
            await pool.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
        await admin.close()


async def _pool_result(pool):
    return pool


def _assert_metadata_queries_only(queries):
    assert queries
    forbidden_tables = (
        "native_revisions", "native_payload_manifests", "m1_reference_payloads",
        "m1_document_bodies", "native_revision_migration", "chunks", "events",
    )
    assert not any(table in query.lower() for table in forbidden_tables for query in queries)
    assert not any(re.search(r"\b(?:INSERT|UPDATE|DELETE)\b", query, re.IGNORECASE) for query in queries)


@pytest.mark.parametrize("backend", ["bare_git", "postgres_native"])
async def test_head_tracks_get_update_move_path_reuse_and_delete(monkeypatch, tmp_path, backend):
    async with _fresh_database(monkeypatch) as (pool, read_pool, queries):
        vault = f"head-{uuid.uuid4().hex[:12]}"
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO vaults (name, git_path) VALUES ($1, $2)",
                vault, str(tmp_path / f"{vault}.git"),
            )
            await conn.execute(
                "INSERT INTO vaults (name, git_path) VALUES ('other-vault', 'unused.git')",
            )
        if backend == "postgres_native":
            writer = NativeDocumentService(pool=pool)
            reader = NativeDocumentService(pool=read_pool)
        else:
            git = GitService(storage_path=str(tmp_path))
            git.init_vault(vault)
            writer = DocumentService(git=git)
            reader = DocumentService(git=git)

            async def read_repos():
                return VaultRepository(read_pool), DocumentRepository(read_pool), CollectionRepository(read_pool)

            monkeypatch.setattr(reader, "_repos", read_repos)

        async def head(reference):
            queries.clear()
            result = await reader.get_head(vault, reference)
            _assert_metadata_queries_only(queries)
            assert read_pool.get_idle_size() == read_pool.get_size() == 1
            return result

        created = await writer.put(DocumentPutRequest(
            vault=vault, collection="notes", title="Source", slug="source", content="first body",
        ))
        original = await writer.get(vault, created.path)
        original_head = await head(created.path)
        assert original_head.current_commit == original.current_commit == created.current_commit
        assert original_head.updated_at == original.updated_at

        updated = await writer.update(vault, created.path, DocumentUpdateRequest(
            content="second body", expected_commit=original_head.current_commit,
        ))
        current = await writer.get(vault, created.path)
        updated_head = await head(created.path)
        assert updated_head.current_commit == current.current_commit == updated.current_commit
        assert updated_head.current_commit != original_head.current_commit
        assert updated_head.updated_at == current.updated_at

        moved = await writer.move(vault, created.path, collection="archive", slug="moved")
        moved_head = await head(created.path)
        moved_get = await writer.get(vault, created.path)
        assert moved_head.path == moved.path == "archive/moved.md"
        assert moved_head.uri == moved_get.uri == moved.uri
        assert moved_head.current_commit == moved_get.current_commit == moved.current_commit

        replacement = await writer.put(DocumentPutRequest(
            vault=vault, collection="notes", title="Replacement", slug="source", content="replacement body",
        ))
        assert (await head(created.path)).current_commit == replacement.current_commit
        assert (await head(moved.path)).current_commit == moved.current_commit

        async with pool.acquire() as conn:
            resource_id = await conn.fetchval(
                "SELECT resource_id FROM native_resources WHERE namespace_id = "
                "(SELECT id FROM vaults WHERE name = $1) AND current_path = $2 AND lifecycle = 'live'"
                if backend == "postgres_native" else
                "SELECT id FROM documents WHERE vault_id = (SELECT id FROM vaults WHERE name = $1) AND path = $2",
                vault, moved.path,
            )
        with pytest.raises(NotFoundError):
            await reader.get_head("other-vault", str(resource_id))
        with pytest.raises(NotFoundError):
            await reader.get_head("missing-vault", moved.path)

        assert (await head(str(resource_id))).path == moved.path
        if backend == "postgres_native":
            await (await writer._native()).create_text(
                namespace_id=await writer._vault_id(vault),
                surface="document", path=str(resource_id), payload="UUID path owner",
                actor="head-test", mutation_id=uuid.uuid4(), resource_id=uuid.uuid4(),
            )
            with pytest.raises(NativeResourceReferenceAmbiguousError):
                await reader.get_head(vault, str(resource_id))

        await writer.delete(vault, moved.path)
        with pytest.raises(NotFoundError):
            await reader.get_head(vault, moved.path)
        assert (await head(created.path)).current_commit == replacement.current_commit
        await writer.delete(vault, created.path)
        with pytest.raises(NotFoundError):
            await reader.get_head(vault, created.path)


async def test_legacy_null_head_token_remains_null(monkeypatch, tmp_path):
    async with _fresh_database(monkeypatch) as (pool, _read_pool, _queries):
        vault = f"null-head-{uuid.uuid4().hex[:12]}"
        git = GitService(storage_path=str(tmp_path))
        git.init_vault(vault)
        service = DocumentService(git=git)
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO vaults (name, git_path) VALUES ($1, $2)", vault, "unused.git")
        created = await service.put(DocumentPutRequest(
            vault=vault, collection="", title="Legacy", slug="legacy", content="legacy body",
        ))
        async with pool.acquire() as conn:
            await conn.execute("UPDATE documents SET current_commit = NULL WHERE path = $1", created.path)
        assert (await service.get_head(vault, created.path)).current_commit is None
        assert (await service.get(vault, created.path)).current_commit is None
