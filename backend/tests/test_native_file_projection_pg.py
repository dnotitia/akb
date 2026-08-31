"""Fixture checks for post-cutover ordinary File mutation synchronization."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import pytest

from app.config import settings
from app.services import native_file_projection as projection


pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[1]
_INIT_SQL = (_BACKEND / "app" / "db" / "init.sql").read_text(encoding="utf-8")
_MIGRATIONS = _BACKEND / "app" / "db" / "migrations"
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


def _load(filename: str):
    path = _MIGRATIONS / filename
    spec = importlib.util.spec_from_file_location(f"migration_file_projection_{filename}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@asynccontextmanager
async def _fresh_schema():
    if not await _reachable():
        pytest.skip(f"Postgres not reachable at {_DSN}")
    name = f"akb_native_file_projection_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(_DSN)
    conn = None
    pool = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        dsn = f"{_DSN.rsplit('/', 1)[0]}/{name}"
        conn = await asyncpg.connect(dsn)
        await conn.execute(_INIT_SQL)
        for filename in (
            "048_native_revision_core.py",
            "053_native_revision_m1_pg_body.py",
            "089_native_file_projection_outbox.py",
        ):
            await _load(filename).migrate(conn=conn)
        await conn.close()
        conn = None
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
        yield pool
    finally:
        if pool is not None:
            await pool.close()
        if conn is not None and not conn.is_closed():
            await conn.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


async def _publish_source(
    pool: asyncpg.Pool,
    *,
    file_id: uuid.UUID,
    vault_id: uuid.UUID,
    payload: bytes,
    mime_type: str,
    s3_key: str,
    actor: str = "fixture-owner",
) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO vault_files (
                    id, vault_id, kind, upload_state, name, s3_key, mime_type,
                    size_bytes, content_hash, hash_algorithm, hash_verified_at, created_by
                )
                VALUES ($1, $2, 'file', 'confirmed', 'fixture.txt', $3, $4,
                        $5, $6, 'sha256', NOW(), $7)
                ON CONFLICT (id) DO UPDATE
                    SET upload_state = 'confirmed', s3_key = EXCLUDED.s3_key,
                        mime_type = EXCLUDED.mime_type,
                        size_bytes = EXCLUDED.size_bytes,
                        content_hash = EXCLUDED.content_hash,
                        hash_verified_at = NOW(), updated_at = NOW()
                """,
                file_id, vault_id, s3_key, mime_type, len(payload), _sha(payload), actor,
            )
            await projection.enqueue_native_file_projection(
                conn,
                file_id=file_id,
                namespace_id=vault_id,
                collection=None,
                name="fixture.txt",
                mime_type=mime_type,
                content_hash=_sha(payload),
                byte_size=len(payload),
                s3_key=s3_key,
                actor=actor,
            )


async def test_file_text_binary_text_delete_projection_is_durable_and_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _fresh_schema() as pool:
        monkeypatch.setattr(settings, "document_revision_backend", "postgres_native")
        payloads = {
            "fixture/v1": b"native file version one\n",
            "fixture/v2": b"native file version two\n",
            "fixture/bin": b"\x00\x01binary",
            "fixture/v3": b"native file version three\n",
        }
        monkeypatch.setattr(
            projection.s3_adapter,
            "iter_chunks",
            lambda key: iter((payloads[key],)),
        )
        async with pool.acquire() as conn:
            vault_id = await conn.fetchval(
                "INSERT INTO vaults (name, status) VALUES ($1, 'active') RETURNING id",
                f"native-file-projection-{uuid.uuid4().hex}",
            )
        file_id = uuid.uuid4()
        worker = projection.NativeFileProjectionWorker(pool)

        await _publish_source(
            pool, file_id=file_id, vault_id=vault_id,
            payload=payloads["fixture/v1"], mime_type="text/plain", s3_key="fixture/v1",
        )
        assert await worker.process_once() == 1
        async with pool.acquire() as conn:
            created = await conn.fetchrow(
                """
                SELECT r.resource_id, r.lifecycle, r.current_path, r.head_revision_id,
                       pm.digest, o.outcome
                  FROM native_resources r
                  JOIN native_revisions nr ON nr.revision_id = r.head_revision_id
                  JOIN native_payload_manifests pm
                    ON pm.payload_manifest_id = nr.payload_manifest_id
                  JOIN native_file_projection_outbox o ON o.file_id = r.resource_id
                 WHERE r.resource_id = $1
                """,
                file_id,
            )
        assert tuple(created) == (
            file_id, "live", "fixture.txt", created["head_revision_id"],
            _sha(payloads["fixture/v1"]), "created",
        )

        await _publish_source(
            pool, file_id=file_id, vault_id=vault_id,
            payload=payloads["fixture/v2"], mime_type="text/plain", s3_key="fixture/v2",
        )
        assert await worker.process_once() == 1
        async with pool.acquire() as conn:
            replaced = await conn.fetchrow(
                """
                SELECT r.lifecycle, pm.digest, o.outcome,
                       (SELECT count(*) FROM native_revisions WHERE resource_id = $1) AS revisions
                  FROM native_resources r
                  JOIN native_revisions nr ON nr.revision_id = r.head_revision_id
                  JOIN native_payload_manifests pm
                    ON pm.payload_manifest_id = nr.payload_manifest_id
                  JOIN native_file_projection_outbox o ON o.file_id = r.resource_id
                 WHERE r.resource_id = $1
                """,
                file_id,
            )
        assert tuple(replaced) == ("live", _sha(payloads["fixture/v2"]), "replaced", 2)

        await _publish_source(
            pool, file_id=file_id, vault_id=vault_id,
            payload=payloads["fixture/bin"],
            mime_type="application/octet-stream", s3_key="fixture/bin",
        )
        assert await worker.process_once() == 1
        async with pool.acquire() as conn:
            binary = await conn.fetchrow(
                """
                SELECT r.lifecycle, o.outcome,
                       (SELECT count(*) FROM native_revisions WHERE resource_id = $1) AS revisions
                  FROM native_resources r
                  JOIN native_file_projection_outbox o ON o.file_id = r.resource_id
                 WHERE r.resource_id = $1
                """,
                file_id,
            )
        assert tuple(binary) == ("deleted", "deleted", 3)

        await _publish_source(
            pool, file_id=file_id, vault_id=vault_id,
            payload=payloads["fixture/v3"], mime_type="text/plain", s3_key="fixture/v3",
        )
        assert await worker.process_once() == 1
        async with pool.acquire() as conn:
            restored = await conn.fetchrow(
                """
                SELECT r.resource_id, r.lifecycle, pm.digest, o.outcome,
                       (SELECT count(*) FROM native_revisions WHERE resource_id = $1) AS revisions
                  FROM native_resources r
                  JOIN native_revisions nr ON nr.revision_id = r.head_revision_id
                  JOIN native_payload_manifests pm
                    ON pm.payload_manifest_id = nr.payload_manifest_id
                  JOIN native_file_projection_outbox o ON o.file_id = r.resource_id
                 WHERE r.resource_id = $1
                """,
                file_id,
            )
        assert tuple(restored) == (
            file_id, "live", _sha(payloads["fixture/v3"]), "restored", 4,
        )

        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM vault_files WHERE id = $1", file_id)
                await projection.enqueue_native_file_projection_delete(
                    conn,
                    file_id=file_id,
                    namespace_id=vault_id,
                    collection=None,
                    name="fixture.txt",
                    actor="fixture-owner",
                )
        assert await worker.process_once() == 1
        assert await worker.process_once() == 0
        async with pool.acquire() as conn:
            deleted = await conn.fetchrow(
                """
                SELECT r.resource_id, r.lifecycle, o.outcome,
                       (SELECT count(*) FROM native_revisions WHERE resource_id = $1) AS revisions
                  FROM native_resources r
                  JOIN native_file_projection_outbox o ON o.file_id = r.resource_id
                 WHERE r.resource_id = $1
                """,
                file_id,
            )
        assert tuple(deleted) == (file_id, "deleted", "deleted", 5)
