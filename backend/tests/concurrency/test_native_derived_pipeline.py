from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import pytest

from app.config import settings
from app.services import embed_worker, sparse_encoder
from app.services.m1_pg_body_store import M1PgBodyStore
from app.services.m1_native_grep_service import M1NativeGrepService
from app.services._backfill import MAX_RETRIES
from app.services.native_derived_worker import (
    NATIVE_FILE_SOURCE,
    SELECTED_DELIVERY,
    NativeDerivedWorker,
)
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
    except (OSError, asyncpg.PostgresError):
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
    # Measurement-prefixed on purpose: migration 057 is name-gated, and only a
    # database named for the measurement arm can ever receive a native write
    # (see `app/config.py`). Under any other name 057 self-disables and the
    # fixture silently keeps migration 053's one-placement-per-namespace
    # trigger — a schema no real native write ever meets, and one P1's mixed
    # placements would trip the moment this pipeline covers a facade write.
    name = f"akb_revision_m1_measurement_derived_{uuid.uuid4().hex[:10]}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    dsn = f"{_DSN.rsplit('/', 1)[0]}/{name}"
    conn = await asyncpg.connect(dsn)
    pool = None
    try:
        await conn.execute((_BACKEND / "app" / "db" / "init.sql").read_text())
        for number in (5, 6, 48, 53, 54, 57, 59):
            path = next((_BACKEND / "app" / "db" / "migrations").glob(f"{number:03d}_*.py"))
            spec = importlib.util.spec_from_file_location(f"native_derived_{number}", path)
            assert spec is not None and spec.loader is not None
            migration = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(migration)
            await migration.migrate(conn=conn)
            if number in (54, 59):
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


async def _owned_vault(pool, prefix: str) -> tuple[uuid.UUID, uuid.UUID]:
    async with pool.acquire() as conn:
        owner = await conn.fetchval(
            """
            INSERT INTO users (username, email, password_hash)
            VALUES ($1, $2, 'disabled') RETURNING id
            """,
            f"{prefix}-{uuid.uuid4().hex}",
            f"{prefix}-{uuid.uuid4().hex}@invalid.example",
        )
        vault_id = await conn.fetchval(
            "INSERT INTO vaults (name, git_path, owner_id) VALUES ($1, '/tmp/unused.git', $2) RETURNING id",
            f"{prefix}-{uuid.uuid4().hex}",
            owner,
        )
    return owner, vault_id


async def test_text_file_intent_applies_the_document_parity_derived_path():
    """Successor to the pre-parity `direct_grep` closure test.

    A text File intent used to close on an explicit no-op delivery, which left
    text Files greppable but permanently unembeddable. It must now materialize
    the same derived state a Document does — chunks, a derived Head, and
    Revision/intent provenance — on the same Resource/Revision basis.
    """
    async with _fresh_database() as pool:
        _owner, vault_id = await _owned_vault(pool, "file-parity")
        service = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool))
        worker = NativeDerivedWorker(pool)
        created = await service.create_text(
            namespace_id=vault_id,
            surface="file",
            path="src/main.py",
            payload="embeddable_file_token\n",
            actor="derived-test",
            mutation_id=uuid.uuid4(),
        )

        assert await worker.process_once() == 1
        async with pool.acquire() as conn:
            intent = await conn.fetchrow(
                """
                SELECT intent_id, completed_at, delivery_outcome, selected_delivery,
                       last_error, next_attempt_at
                  FROM native_invalidation_intents WHERE revision_id = $1
                """,
                created.revision_id,
            )
            assert intent["completed_at"] is not None
            assert intent["delivery_outcome"] == "applied"
            # One delivery mechanism after parity: Files no longer carry a
            # separate `native-direct-pg-grep-v1` selection.
            assert intent["selected_delivery"] == SELECTED_DELIVERY
            assert intent["last_error"] is None
            assert intent["next_attempt_at"] is None

            chunk_rows = await conn.fetch(
                """
                SELECT c.id, c.content, c.vault_id, c.vector_indexed_at,
                       dc.resource_id, dc.revision_id, dc.intent_id, dc.namespace_id
                  FROM chunks c JOIN native_derived_chunks dc ON dc.chunk_id = c.id
                 WHERE c.source_type = $1 AND c.source_id = $2
                 ORDER BY c.chunk_index
                """,
                NATIVE_FILE_SOURCE,
                created.resource_id,
            )
            assert chunk_rows
            assert all("embeddable_file_token" in row["content"] for row in chunk_rows)
            # File addressing, not Document addressing.
            vault_name = await conn.fetchval("SELECT name FROM vaults WHERE id = $1", vault_id)
            assert all(
                f"URI: akb://{vault_name}/coll/src/file/{created.resource_id}" in row["content"]
                for row in chunk_rows
            )
            assert all("PATH: src/main.py" in row["content"] for row in chunk_rows)
            # C3 identification chain: every derived row names the exact
            # Resource, Revision, and intent it was produced from.
            assert {row["revision_id"] for row in chunk_rows} == {created.revision_id}
            assert {row["intent_id"] for row in chunk_rows} == {intent["intent_id"]}
            assert {row["namespace_id"] for row in chunk_rows} == {vault_id}
            assert {row["vault_id"] for row in chunk_rows} == {vault_id}
            # Crash-safe ordering: chunks land unindexed for the embed worker.
            assert all(row["vector_indexed_at"] is None for row in chunk_rows)

            head = await conn.fetchrow(
                """
                SELECT namespace_id, revision_id, intent_id, path, chunk_count, content_digest
                  FROM native_derived_heads WHERE resource_id = $1
                """,
                created.resource_id,
            )
            assert head["namespace_id"] == vault_id
            assert head["revision_id"] == created.revision_id
            assert head["intent_id"] == intent["intent_id"]
            assert head["path"] == "src/main.py"
            assert head["chunk_count"] == len(chunk_rows)
            assert head["content_digest"] == hashlib.sha256(b"embeddable_file_token\n").hexdigest()

            assert await conn.fetchval(
                "SELECT COUNT(*) FROM native_invalidation_intents WHERE completed_at IS NULL"
            ) == 0

        stats = await worker.pending_stats(vault_id)
        assert stats["pending"] == 0
        assert stats["applied"] == 1
        assert stats["direct_grep"] == 0

        prior_chunk_ids = {row["id"] for row in chunk_rows}
        deleted = await service.delete_resource(
            namespace_id=vault_id,
            surface="file",
            path="src/main.py",
            actor="derived-test",
            mutation_id=uuid.uuid4(),
            expected_revision_id=created.revision_id,
            expected_resource_id=created.resource_id,
        )
        assert deleted.revision_id != created.revision_id
        assert await worker.process_once() == 1
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT delivery_outcome FROM native_invalidation_intents WHERE revision_id = $1",
                deleted.revision_id,
            ) == "deleted"
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM chunks WHERE source_type = $1 AND source_id = $2",
                NATIVE_FILE_SOURCE,
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
            # Vector tombstones ride the same outbox the Document path uses.
            queued = await conn.fetch(
                "SELECT chunk_id FROM vector_delete_outbox WHERE source_type = $1",
                NATIVE_FILE_SOURCE,
            )
            assert {row["chunk_id"] for row in queued} == prior_chunk_ids


async def test_file_grep_reads_the_head_even_when_derived_chunks_disagree():
    """Derived output is never an exact-grep oracle.

    Text Files have no update path today, so the disagreement is manufactured
    directly in the disposable database: the derived chunk row is rewritten to
    claim a token the Head never contained and to drop the token it does.  Grep
    must still answer from the verified Head bytes.
    """
    async with _fresh_database() as pool:
        owner, vault_id = await _owned_vault(pool, "grep-head")
        service = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool))
        created = await service.create_text(
            namespace_id=vault_id,
            surface="file",
            path="src/main.py",
            payload="head_truth_token\n",
            actor="derived-test",
            mutation_id=uuid.uuid4(),
        )
        assert await NativeDerivedWorker(pool).process_once() == 1

        async with pool.acquire() as conn:
            corrupted = await conn.fetchval(
                """
                UPDATE chunks SET content = 'chunk_lie_token'
                 WHERE source_type = $1 AND source_id = $2
                RETURNING id
                """,
                NATIVE_FILE_SOURCE,
                created.resource_id,
            )
            assert corrupted is not None

        grep = M1NativeGrepService(pool)
        found = await grep.grep("head_truth_token", user_id=owner, include_text_files=True)
        assert found["total_resources"] == 1
        assert found["results"][0]["revision"] == created.revision_id
        assert found["results"][0]["resource_type"] == "file"
        lied = await grep.grep("chunk_lie_token", user_id=owner, include_text_files=True)
        assert lied["total_resources"] == 0
        assert lied["results"] == []


async def test_native_file_chunks_are_claimed_and_upserted_by_the_embed_worker(monkeypatch):
    """The embeddability the frozen spec requires, proven end to end.

    Admitted text File create → intent → derived worker → chunks with the
    File's Revision/intent provenance → the embed worker's own selection query
    claims them and drives them through to a vector-store upsert.
    """
    async with _fresh_database() as pool:
        _owner, vault_id = await _owned_vault(pool, "embed-file")
        service = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool))
        created = await service.create_text(
            namespace_id=vault_id,
            surface="file",
            path="src/service.py",
            payload="def handler():\n    return 'indexable file body'\n",
            actor="derived-test",
            mutation_id=uuid.uuid4(),
        )
        assert await NativeDerivedWorker(pool).process_once() == 1

        async with pool.acquire() as conn:
            derived_chunk_ids = {
                row["id"]
                for row in await conn.fetch(
                    "SELECT id FROM chunks WHERE source_type = $1 AND source_id = $2",
                    NATIVE_FILE_SOURCE,
                    created.resource_id,
                )
            }
        assert derived_chunk_ids

        # 1. The embed worker's real selection query must see them.
        async with pool.acquire() as conn:
            async with conn.transaction():
                claimed = await embed_worker._claim_batch(conn)
        assert {row["id"] for row in claimed} == derived_chunk_ids
        assert {row["source_type"] for row in claimed} == {NATIVE_FILE_SOURCE}
        assert {row["source_id"] for row in claimed} == {created.resource_id}
        async with pool.acquire() as conn:
            await conn.execute("UPDATE chunks SET vector_next_attempt_at = NULL")

        # 2. Drive the whole pass with a faked model + vector store. The
        #    measurement hook stays off (this database is not named exactly
        #    `akb_revision_m1_measurement`), so the pass measures embedding
        #    pickup only.
        upserts: list[dict] = []

        class _FakeStore:
            async def upsert_one(self, **kwargs):
                upserts.append(kwargs)

        async def fake_embeddings(texts):
            return [[0.5] * 4 for _ in texts]

        async def fake_sparse(_content):
            return [1], [1.0]

        async def fake_get_pool():
            return pool

        monkeypatch.setattr(embed_worker, "get_pool", fake_get_pool)
        monkeypatch.setattr(embed_worker, "generate_embeddings", fake_embeddings)
        monkeypatch.setattr(embed_worker, "get_vector_store", lambda: _FakeStore())
        monkeypatch.setattr(sparse_encoder, "encode_document", fake_sparse)
        monkeypatch.setattr(settings, "embed_base_url", "http://embed.invalid/v1")

        assert await embed_worker._process_once() == len(derived_chunk_ids)
        assert {uuid.UUID(call["chunk_id"]) for call in upserts} == derived_chunk_ids
        assert {call["source_type"] for call in upserts} == {NATIVE_FILE_SOURCE}
        assert {call["source_id"] for call in upserts} == {str(created.resource_id)}
        assert {call["vault_id"] for call in upserts} == {str(vault_id)}
        assert all(call["dense"] == [0.5] * 4 for call in upserts)

        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM chunks WHERE id = ANY($1::uuid[]) AND vector_indexed_at IS NULL",
                list(derived_chunk_ids),
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
