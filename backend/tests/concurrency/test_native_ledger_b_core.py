"""Focused real-PostgreSQL contract for the M1 B-core native ledger.

The reference payload table exists only to exercise ledger publication in M1;
these tests deliberately do not approve it as the final searchable-body store.
"""

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

from app.exceptions import ConflictError, ValidationError
from app.services.m1_reference_payload_store import M1ReferencePayloadStore
from app.services.native_revision_service import NativeRevisionService


pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[2]
_INIT_SQL = (_BACKEND / "app" / "db" / "init.sql").read_text()
_MIGRATION = _BACKEND / "app" / "db" / "migrations" / "048_native_revision_core.py"
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


def _database_dsn(name: str) -> str:
    base, _ = _DSN.rsplit("/", 1)
    return f"{base}/{name}"


def _load_migration():
    spec = importlib.util.spec_from_file_location("native_revision_migration", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@asynccontextmanager
async def _fresh_database():
    if not await _can_connect():
        pytest.skip(f"Postgres not reachable at {_DSN}")
    admin = await asyncpg.connect(_DSN)
    name = f"akb_native_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    dsn = _database_dsn(name)
    conn = await asyncpg.connect(dsn)
    pool = None
    try:
        await conn.execute(_INIT_SQL)
        migration = _load_migration()
        await migration.migrate(conn=conn)
        await migration.migrate(conn=conn)  # idempotent startup/retry
        await conn.close()
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=8)
        async with pool.acquire() as seeded:
            vault_id = await seeded.fetchval(
                "INSERT INTO vaults (name, git_path) VALUES ($1, $2) RETURNING id",
                f"native-{uuid.uuid4().hex}",
                "/tmp/legacy-unused.git",
            )
        yield pool, vault_id
    finally:
        if pool is not None:
            await pool.close()
        elif not conn.is_closed():
            await conn.close()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


def _create_args(vault_id: uuid.UUID, *, mutation_id: uuid.UUID | None = None) -> dict:
    return {
        "namespace_id": vault_id,
        "surface": "document",
        "path": "notes/native.md",
        "payload": "alpha\n",
        "actor": "m1-test",
        "mutation_id": mutation_id or uuid.uuid4(),
        "message": "create native note",
        "subject": "native note",
        "summary": "first revision",
    }


async def _authority_counts(pool: asyncpg.Pool) -> dict[str, int]:
    tables = (
        "native_resources",
        "native_revisions",
        "native_payload_manifests",
        "native_revision_activity",
        "native_invalidation_intents",
    )
    async with pool.acquire() as conn:
        return {table: await conn.fetchval(f"SELECT count(*) FROM {table}") for table in tables}


async def test_create_get_replace_are_native_atomic_and_leave_legacy_projection_untouched():
    async with _fresh_database() as (pool, vault_id):
        async with pool.acquire() as conn:
            legacy_id = await conn.fetchval(
                """
                INSERT INTO documents
                    (vault_id, path, title, current_commit, created_by)
                VALUES ($1, $2, 'legacy', $3, 'legacy-owner')
                RETURNING id
                """,
                vault_id,
                "notes/native.md",
                "0123456789abcdef0123456789abcdef01234567",
            )

        service = NativeRevisionService(pool)
        created = await service.create_text(**_create_args(vault_id))

        assert len(created.revision_id) == 40
        assert created.revision_id == created.revision_id.lower()
        assert all(ch in "0123456789abcdef" for ch in created.revision_id)
        assert created.parent_revision_id is None
        assert created.action == "create"

        current = await service.get_current(
            namespace_id=vault_id,
            surface="document",
            path="notes/native.md",
        )
        assert current.resource_id == created.resource_id
        assert current.revision_id == created.revision_id
        assert current.payload_bytes == b"alpha\n"
        assert current.text == "alpha\n"
        assert current.digest == hashlib.sha256(b"alpha\n").hexdigest()

        replaced = await service.replace_text(
            namespace_id=vault_id,
            surface="document",
            path="notes/native.md",
            payload="beta\n",
            actor="m1-test",
            mutation_id=uuid.uuid4(),
            expected_revision_id=created.revision_id,
            message="replace native note",
        )
        assert replaced.resource_id == created.resource_id
        assert replaced.parent_revision_id == created.revision_id
        assert replaced.revision_id != created.revision_id

        current = await service.get_current(
            namespace_id=vault_id,
            surface="document",
            path="notes/native.md",
        )
        assert current.revision_id == replaced.revision_id
        assert current.text == "beta\n"

        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT current_commit FROM documents WHERE id = $1", legacy_id
            ) == "0123456789abcdef0123456789abcdef01234567"
            rows = await conn.fetch(
                """
                SELECT r.revision_id, r.parent_revision_id, r.action,
                       m.digest, m.byte_size, m.selected_placement,
                       a.revision_id AS activity_revision,
                       i.revision_id AS intent_revision
                  FROM native_revisions r
                  JOIN native_payload_manifests m ON m.payload_manifest_id = r.payload_manifest_id
                  JOIN native_revision_activity a ON a.activity_event_id = r.activity_event_id
                  JOIN native_invalidation_intents i ON i.intent_id = r.invalidation_intent_id
                 WHERE r.resource_id = $1
                 ORDER BY r.occurred_at, r.revision_id
                """,
                created.resource_id,
            )
        assert len(rows) == 2
        assert {row["selected_placement"] for row in rows} == {"m1-reference-payload-v1"}
        assert all(row["revision_id"] == row["activity_revision"] == row["intent_revision"] for row in rows)


async def test_same_base_replace_has_one_winner_without_vault_write_lane():
    async with _fresh_database() as (pool, vault_id):
        service = NativeRevisionService(pool)
        created = await service.create_text(**_create_args(vault_id))

        async def replace(body: str):
            return await service.replace_text(
                namespace_id=vault_id,
                surface="document",
                path="notes/native.md",
                payload=body,
                actor="racer",
                mutation_id=uuid.uuid4(),
                expected_revision_id=created.revision_id,
            )

        outcomes = await asyncio.gather(replace("winner-a"), replace("winner-b"), return_exceptions=True)
        successes = [item for item in outcomes if not isinstance(item, Exception)]
        conflicts = [item for item in outcomes if isinstance(item, ConflictError)]
        assert len(successes) == 1
        assert len(conflicts) == 1

        current = await service.get_current(
            namespace_id=vault_id,
            surface="document",
            path="notes/native.md",
        )
        assert current.revision_id == successes[0].revision_id
        assert await _authority_counts(pool) == {
            "native_resources": 1,
            "native_revisions": 2,
            "native_payload_manifests": 2,
            "native_revision_activity": 2,
            "native_invalidation_intents": 2,
        }


async def test_same_path_create_has_one_winner_and_distinct_paths_do_not_share_a_vault_lock():
    async with _fresh_database() as (pool, vault_id):
        service = NativeRevisionService(pool)

        same_path = await asyncio.gather(
            service.create_text(**_create_args(vault_id)),
            service.create_text(**_create_args(vault_id)),
            return_exceptions=True,
        )
        assert len([item for item in same_path if not isinstance(item, Exception)]) == 1
        assert len([item for item in same_path if isinstance(item, ConflictError)]) == 1

        left = _create_args(vault_id)
        left["path"] = "notes/left.md"
        right = _create_args(vault_id)
        right["path"] = "notes/right.md"
        extra = await asyncio.gather(service.create_text(**left), service.create_text(**right))
        assert {item.path for item in extra} == {"notes/left.md", "notes/right.md"}


@pytest.mark.parametrize(
    "boundary",
    [
        "authority.after_resource",
        "authority.after_manifest",
        "authority.after_revision",
        "authority.after_head",
        "authority.after_activity",
        "authority.after_invalidation",
    ],
)
async def test_authority_failpoints_rollback_every_published_fact(boundary: str):
    async with _fresh_database() as (pool, vault_id):
        seen: list[str] = []

        async def failpoint(name: str) -> None:
            seen.append(name)
            if name == boundary:
                raise RuntimeError(f"injected:{name}")

        service = NativeRevisionService(pool, failpoint=failpoint)
        mutation_id = uuid.uuid4()
        with pytest.raises(RuntimeError, match=f"injected:{boundary}"):
            await service.create_text(**_create_args(vault_id, mutation_id=mutation_id))

        assert boundary in seen
        assert await _authority_counts(pool) == {
            "native_resources": 0,
            "native_revisions": 0,
            "native_payload_manifests": 0,
            "native_revision_activity": 0,
            "native_invalidation_intents": 0,
        }
        # Preparation is deliberately outside the authority transaction. The
        # unreferenced verified row is not a Revision and may be retried.
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM m1_reference_payloads") == 1


async def test_committed_response_lost_retry_returns_same_revision_once():
    async with _fresh_database() as (pool, vault_id):
        fired = False

        async def failpoint(name: str) -> None:
            nonlocal fired
            if name == "authority.after_commit_before_response" and not fired:
                fired = True
                raise RuntimeError("response lost")

        mutation_id = uuid.uuid4()
        service = NativeRevisionService(pool, failpoint=failpoint)
        with pytest.raises(RuntimeError, match="response lost"):
            await service.create_text(**_create_args(vault_id, mutation_id=mutation_id))

        retried = await service.create_text(**_create_args(vault_id, mutation_id=mutation_id))
        assert retried.idempotent_replay is True
        assert await _authority_counts(pool) == {
            "native_resources": 1,
            "native_revisions": 1,
            "native_payload_manifests": 1,
            "native_revision_activity": 1,
            "native_invalidation_intents": 1,
        }
        with pytest.raises(ConflictError, match="idempotency"):
            changed = _create_args(vault_id, mutation_id=mutation_id)
            changed["payload"] = "different retry"
            await service.create_text(**changed)


async def test_reference_payload_verifies_digest_size_and_utf8_before_authority_tx():
    async with _fresh_database() as (pool, vault_id):
        store = M1ReferencePayloadStore(pool)
        body = "한글 payload\n".encode()
        digest = hashlib.sha256(body).hexdigest()
        prepared = await store.prepare_text(
            namespace_id=vault_id,
            payload=body,
            expected_digest=digest,
            expected_size=len(body),
        )
        assert await store.open_verified(prepared.payload_id) == body

        with pytest.raises(ValidationError, match="UTF-8"):
            await store.prepare_text(namespace_id=vault_id, payload=b"\xff")
        with pytest.raises(ValidationError, match="NUL"):
            await store.prepare_text(namespace_id=vault_id, payload=b"a\x00b")
        with pytest.raises(ValidationError, match="digest"):
            await store.prepare_text(
                namespace_id=vault_id,
                payload=body,
                expected_digest="0" * 64,
            )
        with pytest.raises(ValidationError, match="size"):
            await store.prepare_text(namespace_id=vault_id, payload=body, expected_size=len(body) + 1)


async def test_schema_enforces_revision_identity_head_ownership_and_immutable_facts():
    async with _fresh_database() as (pool, vault_id):
        service = NativeRevisionService(pool)
        one = await service.create_text(**_create_args(vault_id))
        other_args = _create_args(vault_id)
        other_args["path"] = "notes/other.md"
        other = await service.create_text(**other_args)

        async with pool.acquire() as conn:
            definition = await conn.fetchval(
                """
                SELECT pg_get_constraintdef(oid)
                  FROM pg_constraint
                 WHERE conname = 'native_revisions_revision_id_shape'
                """
            )
            assert "{40}" in definition

            with pytest.raises(asyncpg.ForeignKeyViolationError):
                async with conn.transaction():
                    await conn.execute(
                        "UPDATE native_resources SET head_revision_id = $2 WHERE resource_id = $1",
                        one.resource_id,
                        other.revision_id,
                    )
            with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
                await conn.execute(
                    "UPDATE native_revisions SET message = 'mutated' WHERE revision_id = $1",
                    one.revision_id,
                )
            with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
                await conn.execute(
                    "DELETE FROM native_revision_activity WHERE revision_id = $1",
                    one.revision_id,
                )


async def test_revision_id_collision_is_retried_as_identity_not_path_conflict(monkeypatch):
    async with _fresh_database() as (pool, vault_id):
        service = NativeRevisionService(pool)
        first = await service.create_text(**_create_args(vault_id))
        allocated_after_retry = "f" * 40
        candidates = iter((first.revision_id, allocated_after_retry))
        monkeypatch.setattr(service, "_opaque_revision_id", lambda: next(candidates))

        second_args = _create_args(vault_id)
        second_args["path"] = "notes/collision-retried.md"
        second = await service.create_text(**second_args)
        assert second.revision_id == allocated_after_retry
        assert second.resource_id != first.resource_id
        assert (await _authority_counts(pool))["native_revisions"] == 2


async def test_vault_delete_cascades_the_experimental_native_subtree():
    async with _fresh_database() as (pool, vault_id):
        service = NativeRevisionService(pool)
        await service.create_text(**_create_args(vault_id))
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM vaults WHERE id = $1", vault_id)
            for table in (
                "native_resources",
                "native_revisions",
                "native_payload_manifests",
                "native_revision_activity",
                "native_invalidation_intents",
                "m1_reference_payloads",
            ):
                assert await conn.fetchval(f"SELECT count(*) FROM {table}") == 0


async def test_native_substrate_has_no_git_legacy_projection_or_write_lane_dependency():
    paths = (
        _BACKEND / "app" / "repositories" / "native_revision_repo.py",
        _BACKEND / "app" / "services" / "m1_reference_payload_store.py",
        _BACKEND / "app" / "services" / "native_revision_service.py",
    )
    source = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    for forbidden in ("GitService", "git_service", "current_commit", "write_lane", "documents"):
        assert forbidden not in source
