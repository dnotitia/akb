from __future__ import annotations

import asyncio
import importlib.util
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import pytest

from app.services.m1_pg_body_store import M1PgBodyStore
from app.services.m1_native_grep_service import M1NativeGrepService
from app.services._backfill import MAX_RETRIES
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
        for number in (5, 6, 48, 49, 50):
            path = next((_BACKEND / "app" / "db" / "migrations").glob(f"{number:03d}_*.py"))
            spec = importlib.util.spec_from_file_location(f"native_derived_{number}", path)
            assert spec is not None and spec.loader is not None
            migration = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(migration)
            await migration.migrate(conn=conn)
            if number == 50:
                await migration.migrate(conn=conn)  # startup retry is idempotent
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


async def _create_document(pool, vault_id, *, path: str = "retry.md"):
    return await NativeRevisionService(pool, payload_store=M1PgBodyStore(pool)).create_text(
        namespace_id=vault_id,
        surface="document",
        path=path,
        payload="---\ntitle: Retry\n---\n# Retry\nsearchable\n",
        actor="derived-test",
        mutation_id=uuid.uuid4(),
    )


async def test_failed_intent_retries_then_reclaims_and_applies(monkeypatch):
    async with _fresh_database() as pool:
        async with pool.acquire() as conn:
            vault_id = await conn.fetchval(
                "INSERT INTO vaults (name, git_path) VALUES ($1, '/tmp/unused.git') RETURNING id",
                f"retry-{uuid.uuid4().hex}",
            )
        created = await _create_document(pool, vault_id)
        worker = NativeDerivedWorker(pool)
        original_head = worker._head

        async def fail_head(_resource_id):
            raise RuntimeError("sensitive body must not enter durable error")

        monkeypatch.setattr(worker, "_head", fail_head)
        assert await worker.process_once() == 0
        async with pool.acquire() as conn:
            failed = await conn.fetchrow(
                "SELECT retry_count, completed_at, last_error FROM native_invalidation_intents WHERE revision_id = $1",
                created.revision_id,
            )
            assert failed["retry_count"] == 1
            assert failed["completed_at"] is None
            assert failed["last_error"] == "RuntimeError"
            await conn.execute(
                "UPDATE native_invalidation_intents SET next_attempt_at = NOW() WHERE revision_id = $1",
                created.revision_id,
            )

        monkeypatch.setattr(worker, "_head", original_head)
        assert await worker.process_once() == 1
        async with pool.acquire() as conn:
            recovered = await conn.fetchrow(
                "SELECT retry_count, delivery_outcome, completed_at, last_error FROM native_invalidation_intents WHERE revision_id = $1",
                created.revision_id,
            )
            assert recovered["retry_count"] == 1
            assert recovered["delivery_outcome"] == "applied"
            assert recovered["completed_at"] is not None
            assert recovered["last_error"] is None


async def test_retry_exhaustion_is_terminal_abandoned_and_settlement_reports_it(monkeypatch):
    async with _fresh_database() as pool:
        async with pool.acquire() as conn:
            vault_id = await conn.fetchval(
                "INSERT INTO vaults (name, git_path) VALUES ($1, '/tmp/unused.git') RETURNING id",
                f"abandoned-{uuid.uuid4().hex}",
            )
        created = await _create_document(pool, vault_id)
        worker = NativeDerivedWorker(pool)

        async def fail_head(_resource_id):
            raise RuntimeError("never persist this sensitive message")

        monkeypatch.setattr(worker, "_head", fail_head)
        for attempt in range(MAX_RETRIES):
            assert await worker.process_once() == 0
            if attempt + 1 < MAX_RETRIES:
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE native_invalidation_intents SET next_attempt_at = NOW() WHERE revision_id = $1",
                        created.revision_id,
                    )

        stats = await worker.pending_stats(vault_id)
        assert stats["pending"] == 0
        assert stats["abandoned"] == 1
        settled = await worker.settle(
            namespace_id=vault_id,
            timeout_seconds=0.2,
            poll_interval_seconds=0.001,
        )
        assert settled["abandoned"] == 1
        async with pool.acquire() as conn:
            terminal = await conn.fetchrow(
                """
                SELECT retry_count, completed_at, delivery_outcome, next_attempt_at, last_error
                  FROM native_invalidation_intents WHERE revision_id = $1
                """,
                created.revision_id,
            )
            assert terminal["retry_count"] == MAX_RETRIES
            assert terminal["completed_at"] is not None
            assert terminal["delivery_outcome"] == "abandoned"
            assert terminal["next_attempt_at"] is None
            assert terminal["last_error"] == "RuntimeError"


async def test_multiworker_skip_locked_claims_intent_once():
    async with _fresh_database() as pool:
        async with pool.acquire() as conn:
            vault_id = await conn.fetchval(
                "INSERT INTO vaults (name, git_path) VALUES ($1, '/tmp/unused.git') RETURNING id",
                f"workers-{uuid.uuid4().hex}",
            )
        created = await _create_document(pool, vault_id)
        workers = (NativeDerivedWorker(pool), NativeDerivedWorker(pool))

        claims = await asyncio.gather(*(worker._claim_one() for worker in workers))

        claimed = [row for row in claims if row is not None]
        assert len(claimed) == 1
        assert claimed[0]["revision_id"] == created.revision_id


async def test_expired_claim_lease_is_reclaimed_by_another_worker():
    async with _fresh_database() as pool:
        async with pool.acquire() as conn:
            vault_id = await conn.fetchval(
                "INSERT INTO vaults (name, git_path) VALUES ($1, '/tmp/unused.git') RETURNING id",
                f"reclaim-{uuid.uuid4().hex}",
            )
        created = await _create_document(pool, vault_id, path="reclaim.md")
        first = await NativeDerivedWorker(pool)._claim_one()
        assert first is not None
        assert first["revision_id"] == created.revision_id
        assert await NativeDerivedWorker(pool)._claim_one() is None

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE native_invalidation_intents SET next_attempt_at = NOW() "
                "WHERE intent_id = $1",
                first["intent_id"],
            )

        reclaimed = await NativeDerivedWorker(pool)._claim_one()
        assert reclaimed is not None
        assert reclaimed["intent_id"] == first["intent_id"]
        assert reclaimed["retry_count"] == 0


async def test_direct_pg_grep_default_and_additive_modes_preserve_acl_and_collection_boundaries():
    async with _fresh_database() as pool:
        async with pool.acquire() as conn:
            owner = await conn.fetchval(
                """
                INSERT INTO users (username, email, password_hash)
                VALUES ($1, $2, 'disabled') RETURNING id
                """,
                f"grep-owner-{uuid.uuid4().hex}",
                f"grep-owner-{uuid.uuid4().hex}@invalid.example",
            )
            denied_owner = await conn.fetchval(
                """
                INSERT INTO users (username, email, password_hash)
                VALUES ($1, $2, 'disabled') RETURNING id
                """,
                f"grep-denied-{uuid.uuid4().hex}",
                f"grep-denied-{uuid.uuid4().hex}@invalid.example",
            )
            allowed_vault = await conn.fetchval(
                "INSERT INTO vaults (name, git_path, owner_id) VALUES ($1, '/tmp/unused.git', $2) RETURNING id",
                f"grep-allowed-{uuid.uuid4().hex}",
                owner,
            )
            denied_vault = await conn.fetchval(
                "INSERT INTO vaults (name, git_path, owner_id) VALUES ($1, '/tmp/unused.git', $2) RETURNING id",
                f"grep-denied-{uuid.uuid4().hex}",
                denied_owner,
            )
        service = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool))

        async def create(namespace_id, surface, path):
            return await service.create_text(
                namespace_id=namespace_id,
                surface=surface,
                path=path,
                payload="needle-boundary\n",
                actor="grep-test",
                mutation_id=uuid.uuid4(),
            )

        await create(allowed_vault, "document", "src/a.md")
        await create(allowed_vault, "document", "src2/b.md")
        await create(allowed_vault, "file", "src/main.py")
        await create(allowed_vault, "document", "team_%/literal.md")
        await create(allowed_vault, "document", "team_ax/escape.md")
        await create(denied_vault, "document", "src/secret.md")
        grep = M1NativeGrepService(pool)

        default = await grep.grep("needle-boundary", user_id=owner)
        assert default["total_resources"] == 4
        assert {row["resource_type"] for row in default["results"]} == {"document"}
        assert all("secret.md" not in row["path"] for row in default["results"])

        additive = await grep.grep(
            "needle-boundary", user_id=owner, include_text_files=True
        )
        assert additive["total_resources"] == 5
        assert {row["resource_type"] for row in additive["results"]} == {
            "document",
            "file",
        }

        src = await grep.grep(
            "needle-boundary",
            user_id=owner,
            collection="src",
            include_text_files=True,
        )
        assert {row["path"] for row in src["results"]} == {"src/a.md", "src/main.py"}

        wildcard = await grep.grep(
            "needle-boundary", user_id=owner, collection="team_%"
        )
        assert [row["path"] for row in wildcard["results"]] == ["team_%/literal.md"]
