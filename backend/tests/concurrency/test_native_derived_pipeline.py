from __future__ import annotations

import importlib.util
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import pytest

from app.services.m1_pg_body_store import M1PgBodyStore
from app.services.native_derived_worker import NativeDerivedWorker
from app.services.native_revision_service import NativeRevisionService


pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[2]
_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:5433/akb",  # pragma: allowlist secret
)


async def _can_connect() -> bool:
    try:
        conn = await asyncpg.connect(_DSN, timeout=2)
    except OSError, asyncpg.PostgresError:
        return False
    await conn.close()
    return True


@asynccontextmanager
async def _fresh_database():
    if not await _can_connect():
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"REQUIRE_REAL_PG=1 but Postgres is not reachable at {_DSN}")
        pytest.skip(f"Postgres not reachable at {_DSN}")
    admin = await asyncpg.connect(_DSN)
    name = f"akb_native_derived_{uuid.uuid4().hex[:10]}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    dsn = f"{_DSN.rsplit('/', 1)[0]}/{name}"
    conn = await asyncpg.connect(dsn)
    pool = None
    try:
        await conn.execute((_BACKEND / "app" / "db" / "init.sql").read_text())
        for number in (48, 49, 50):
            path = next((_BACKEND / "app" / "db" / "migrations").glob(f"{number:03d}_*.py"))
            spec = importlib.util.spec_from_file_location(f"native_derived_{number}", path)
            assert spec is not None and spec.loader is not None
            migration = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(migration)
            await migration.migrate(conn=conn)
        await conn.close()
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=8)
        yield pool
    finally:
        if pool is not None:
            await pool.close()
        elif not conn.is_closed():
            await conn.close()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


async def test_worker_coalesces_to_current_head_and_closes_chunk_mapping_residue():
    async with _fresh_database() as pool:
        async with pool.acquire() as conn:
            owner = await conn.fetchval(
                """
                INSERT INTO users (username, email, password_hash)
                VALUES ($1, $2, 'disabled') RETURNING id
                """,
                f"derived-{uuid.uuid4().hex}",
                f"derived-{uuid.uuid4().hex}@invalid.example",
            )
            vault_id = await conn.fetchval(
                "INSERT INTO vaults (name, git_path, owner_id) VALUES ($1, '/tmp/unused.git', $2) RETURNING id",
                f"derived-{uuid.uuid4().hex}",
                owner,
            )
        service = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool))
        worker = NativeDerivedWorker(pool)
        created = await service.create_text(
            namespace_id=vault_id,
            surface="document",
            path="guide.md",
            payload="---\ntitle: Guide\n---\n# Now\nprior-token\n",
            actor="derived-test",
            mutation_id=uuid.uuid4(),
        )
        replaced = await service.replace_text(
            namespace_id=vault_id,
            surface="document",
            path="guide.md",
            payload="---\ntitle: Guide\n---\n# Now\ncurrent-token\n",
            actor="derived-test",
            mutation_id=uuid.uuid4(),
            expected_revision_id=created.revision_id,
            expected_resource_id=created.resource_id,
        )

        assert await worker.process_once() == 1
        async with pool.acquire() as conn:
            states = await conn.fetch(
                "SELECT revision_id, delivery_outcome FROM native_invalidation_intents ORDER BY occurred_at"
            )
            assert [row["delivery_outcome"] for row in states] == ["superseded", "applied"]
            mapped = await conn.fetch(
                """
                SELECT c.id, c.content, dc.revision_id
                  FROM chunks c JOIN native_derived_chunks dc ON dc.chunk_id = c.id
                 WHERE c.source_type = 'native_document' AND c.source_id = $1
                """,
                created.resource_id,
            )
            assert mapped
            assert {row["revision_id"] for row in mapped} == {replaced.revision_id}
            assert all("current-token" in row["content"] for row in mapped)
            assert all("prior-token" not in row["content"] for row in mapped)
            prior_chunk_ids = [row["id"] for row in mapped]

        deleted = await service.delete_resource(
            namespace_id=vault_id,
            surface="document",
            path="guide.md",
            actor="derived-test",
            mutation_id=uuid.uuid4(),
            expected_revision_id=replaced.revision_id,
            expected_resource_id=replaced.resource_id,
        )
        assert deleted.revision_id != replaced.revision_id
        assert await worker.process_once() == 1
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM chunks WHERE source_type = 'native_document' AND source_id = $1",
                created.resource_id,
            ) == 0
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM native_derived_chunks WHERE resource_id = $1",
                created.resource_id,
            ) == 0
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM native_derived_heads WHERE resource_id = $1",
                created.resource_id,
            ) == 0
            queued = await conn.fetch(
                "SELECT chunk_id FROM vector_delete_outbox WHERE source_type = 'native_document'"
            )
            assert {row["chunk_id"] for row in queued} == set(prior_chunk_ids)


async def test_text_file_intent_closes_on_explicit_direct_grep_delivery_without_chunks():
    async with _fresh_database() as pool:
        async with pool.acquire() as conn:
            vault_id = await conn.fetchval(
                "INSERT INTO vaults (name, git_path) VALUES ($1, '/tmp/unused.git') RETURNING id",
                f"file-{uuid.uuid4().hex}",
            )
        service = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool))
        created = await service.create_text(
            namespace_id=vault_id,
            surface="file",
            path="src/main.py",
            payload="direct_grep_token\n",
            actor="derived-test",
            mutation_id=uuid.uuid4(),
        )

        assert await NativeDerivedWorker(pool).process_once() == 1
        async with pool.acquire() as conn:
            intent = await conn.fetchrow(
                """
                SELECT completed_at, delivery_outcome, selected_delivery
                  FROM native_invalidation_intents WHERE revision_id = $1
                """,
                created.revision_id,
            )
            assert intent["completed_at"] is not None
            assert intent["delivery_outcome"] == "direct_grep"
            assert intent["selected_delivery"] == "native-direct-pg-grep-v1"
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM chunks WHERE source_id = $1", created.resource_id
            ) == 0
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM native_invalidation_intents WHERE completed_at IS NULL"
            ) == 0
