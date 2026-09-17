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
            "055_native_revision_m1_file_storage.py",
            "057_native_revision_m1_payload_placement.py",
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
            vault_name = f"native-file-projection-{uuid.uuid4().hex}"
            vault_id = await conn.fetchval(
                """
                INSERT INTO vaults (name, git_path, status)
                VALUES ($1, $2, 'active')
                RETURNING id
                """,
                vault_name,
                f"/tmp/{vault_name}.git",
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


async def test_final_projection_claim_is_visible_as_exhausted_until_its_lease_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last claim is diagnostic, but cannot be requeued while it is live."""
    async with _fresh_schema() as pool:
        monkeypatch.setattr(settings, "document_revision_backend", "postgres_native")
        monkeypatch.setattr(projection, "MAX_RETRIES", 1)
        payload = b"final claim lease\n"
        async with pool.acquire() as conn:
            vault_name = f"native-file-projection-final-claim-{uuid.uuid4().hex}"
            vault_id = await conn.fetchval(
                """
                INSERT INTO vaults (name, git_path, status)
                VALUES ($1, $2, 'active')
                RETURNING id
                """,
                vault_name,
                f"/tmp/{vault_name}.git",
            )

        file_id = uuid.uuid4()
        worker = projection.NativeFileProjectionWorker(pool)
        await _publish_source(
            pool,
            file_id=file_id,
            vault_id=vault_id,
            payload=payload,
            mime_type="text/plain",
            s3_key="fixture/final-claim",
        )

        claim = await worker._claim_one()

        assert claim is not None
        assert claim["retry_count"] == 1
        assert claim["claimed_at"] is not None
        assert await worker.pending_stats(namespace_id=vault_id) == {
            "pending": 1,
            "retrying": 0,
            "exhausted": 1,
            "abandoned": 0,
            "status": "degraded",
        }
        # The claim owns its lease until it either completes/fails or the
        # queue rescuer turns an expired lease into terminal abandonment.
        assert await worker.requeue_abandoned(
            namespace_id=vault_id, file_id=file_id,
        ) == 0


async def test_abandoned_projection_is_visible_requeued_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient S3 read failure stays diagnosable until an explicit requeue.

    Requeue is intentionally only a state transition: before the worker runs
    again there is still no Native resource, so it cannot be mistaken for a
    fresh projection.
    """
    async with _fresh_schema() as pool:
        monkeypatch.setattr(settings, "document_revision_backend", "postgres_native")
        monkeypatch.setattr(projection, "MAX_RETRIES", 2)
        monkeypatch.setattr(projection, "next_attempt_delay", lambda _attempt: 0)
        payload = b"recoverable native file text\n"
        failures = {"remaining": 2}

        def flaky_chunks(key: str):
            assert key == "fixture/recovery"
            if failures["remaining"]:
                failures["remaining"] -= 1
                raise OSError("temporary S3 read failure")
            return iter((payload,))

        monkeypatch.setattr(projection.s3_adapter, "iter_chunks", flaky_chunks)
        async with pool.acquire() as conn:
            vault_name = f"native-file-projection-recovery-{uuid.uuid4().hex}"
            vault_id = await conn.fetchval(
                """
                INSERT INTO vaults (name, git_path, status)
                VALUES ($1, $2, 'active')
                RETURNING id
                """,
                vault_name,
                f"/tmp/{vault_name}.git",
            )

        file_id = uuid.uuid4()
        worker = projection.NativeFileProjectionWorker(pool)
        await _publish_source(
            pool,
            file_id=file_id,
            vault_id=vault_id,
            payload=payload,
            mime_type="text/plain",
            s3_key="fixture/recovery",
        )

        assert await worker.process_once() == 0
        assert await worker.process_once() == 0
        async with pool.acquire() as conn:
            terminal = await conn.fetchrow(
                """
                SELECT retry_count, claimed_at, next_attempt_at, completed_at,
                       outcome, last_error
                  FROM native_file_projection_outbox
                 WHERE file_id = $1
                """,
                file_id,
            )
        assert terminal is not None
        assert terminal["retry_count"] == 2
        assert terminal["claimed_at"] is None
        assert terminal["next_attempt_at"] is None
        assert terminal["completed_at"] is not None
        assert terminal["outcome"] == "abandoned"
        assert terminal["last_error"] == "OSError"
        assert await worker.pending_stats(namespace_id=vault_id) == {
            "pending": 0,
            "retrying": 0,
            "exhausted": 0,
            "abandoned": 1,
            "status": "degraded",
        }

        # Scope must be explicit; a mistaken namespace cannot revive an intent.
        assert await worker.requeue_abandoned(
            namespace_id=uuid.uuid4(), file_id=file_id,
        ) == 0
        assert await worker.requeue_abandoned(
            namespace_id=vault_id, file_id=file_id,
        ) == 1
        # A second operator request leaves the already-requeued row untouched.
        assert await worker.requeue_abandoned(
            namespace_id=vault_id, file_id=file_id,
        ) == 0
        assert await worker.pending_stats(namespace_id=vault_id) == {
            "pending": 1,
            "retrying": 0,
            "exhausted": 0,
            "abandoned": 0,
            "status": "reconciling",
        }
        async with pool.acquire() as conn:
            requeued = await conn.fetchrow(
                """
                SELECT retry_count, claimed_at, next_attempt_at, completed_at,
                       outcome, last_error
                  FROM native_file_projection_outbox
                 WHERE file_id = $1
                """,
                file_id,
            )
            resource_count = await conn.fetchval(
                "SELECT COUNT(*) FROM native_resources WHERE resource_id = $1",
                file_id,
            )
        assert requeued is not None
        assert requeued["retry_count"] == 0
        assert requeued["claimed_at"] is None
        assert requeued["next_attempt_at"] is not None
        assert requeued["completed_at"] is None
        assert requeued["outcome"] is None
        assert requeued["last_error"] is None
        assert resource_count == 0

        assert await worker.process_once() == 1
        async with pool.acquire() as conn:
            projected = await conn.fetchrow(
                """
                SELECT pm.digest, o.outcome
                  FROM native_resources r
                  JOIN native_revisions nr ON nr.revision_id = r.head_revision_id
                  JOIN native_payload_manifests pm
                    ON pm.payload_manifest_id = nr.payload_manifest_id
                  JOIN native_file_projection_outbox o ON o.file_id = r.resource_id
                 WHERE r.resource_id = $1
                """,
                file_id,
            )
        assert projected is not None
        assert tuple(projected) == (_sha(payload), "created")
        assert await worker.pending_stats(namespace_id=vault_id) == {
            "pending": 0,
            "retrying": 0,
            "exhausted": 0,
            "abandoned": 0,
            "status": "ok",
        }


async def test_grep_file_coverage_tracks_current_catalogue_and_not_just_live_heads(monkeypatch, tmp_path):
    from app.exceptions import AKBError
    from app.services.m1_native_grep_service import M1NativeGrepService

    async with _fresh_schema() as pool:
        monkeypatch.setattr(settings, "document_revision_backend", "postgres_native")
        payloads = {"v1": b"old needle\n", "v2": b"new needle\n", "invalid": b"\x00bad"}
        monkeypatch.setattr(projection.s3_adapter, "iter_chunks", lambda key: iter([payloads[key]]))
        async with pool.acquire() as conn:
            owner = await conn.fetchval(
                "INSERT INTO users (username, email, password_hash) VALUES ($1,$2,'unused') RETURNING id",
                str(uuid.uuid4()), str(uuid.uuid4()) + "@example.test",
            )
            vault = await conn.fetchval(
                "INSERT INTO vaults (name,git_path,owner_id) VALUES ($1,'/tmp/unused', $2) RETURNING id",
                str(uuid.uuid4()), owner,
            )
        # Exercise the public REST serializer and MCP dispatch against this
        # actual database. Only transport authentication is fixture-injected.
        from types import SimpleNamespace
        import httpx
        from fastapi import FastAPI
        monkeypatch.setattr(settings, "git_storage_path", str(tmp_path))
        from app.api.routes import search as routes
        from app.api.deps import get_current_user
        from app.main import akb_error_handler
        from app.services import search_service as search_module
        import mcp_server.server as mcp

        async def get_pool():
            return pool

        monkeypatch.setattr(search_module, "get_pool", get_pool)
        app = FastAPI()
        app.include_router(routes.router)
        app.add_exception_handler(AKBError, akb_error_handler)
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(user_id=str(owner))

        async def rest():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                return await client.get("/grep", params={"q": "needle", "include_text_files": "true"})

        file_id = uuid.uuid4()
        grep = M1NativeGrepService(pool)
        worker = projection.NativeFileProjectionWorker(pool)

        async def query(pattern="needle", **kwargs):
            return await grep.grep_public(
                pattern, user_id=owner, include_text_files=True, resource_output=True, **kwargs,
            )

        async def not_ready():
            with pytest.raises(AKBError) as error:
                await query(count_only=True)
            assert error.value.status_code == 409
            assert error.value.code == "grep_file_projection_not_ready"

        await _publish_source(pool, file_id=file_id, vault_id=vault,
                              payload=payloads["v1"], mime_type="text/plain", s3_key="v1")
        await not_ready()  # Missing projection must not masquerade as zero matches.
        pending = await rest()
        assert pending.status_code == 409
        assert pending.json()["code"] == "grep_file_projection_not_ready"
        # A caller without access cannot infer that another vault is pending.
        hidden = await grep.grep_public("needle", user_id=uuid.uuid4(), include_text_files=True)
        assert hidden["total_matches"] == 0
        # Document-only metadata filters exclude Files before readiness checks.
        filtered = await query(doc_types=["note"])
        assert filtered["total_resources"] == 0
        assert await worker.process_once() == 1
        hit = await query()
        assert hit["total_resources"] == 1 and hit["total_docs"] == 0
        assert hit["results"][0]["resource_type"] == "file"
        assert hit["results"][0]["matches"][0]["line"] == 1
        http = await rest()
        assert http.status_code == 200
        wire = http.json()
        mcp_result = await mcp._handle_grep(
            {"pattern": "needle", "include_text_files": True}, str(owner),
            mcp._MCPUser(user_id=str(owner), username="fixture-owner"),
        )
        for key in ("total_docs", "total_resources", "returned_resources", "total_matches"):
            assert wire[key] == mcp_result[key] == hit[key]
        assert wire["results"][0]["revision"] == mcp_result["results"][0]["revision"]
        assert wire["results"][0]["matches"] == [{"line": 1, "text": "old needle"}]
        counts = await query(count_only=True)
        assert counts["by_doc"] == {} and len(counts["by_resource"]) == 1
        listing = await query(files_with_matches=True)
        assert listing["files"] == [] and listing["n_resources"] == 1

        # Even an identical-byte new generation must complete its own intent.
        await _publish_source(pool, file_id=file_id, vault_id=vault,
                              payload=payloads["v1"], mime_type="text/plain", s3_key="v1")
        await not_ready()
        assert await worker.process_once() == 1
        assert (await query())["total_resources"] == 1

        await _publish_source(pool, file_id=file_id, vault_id=vault,
                              payload=payloads["v2"], mime_type="text/plain", s3_key="v2")
        await not_ready()
        # Exhausted/abandoned work must not expose stale text either.
        async with pool.acquire() as conn:
            await conn.execute("UPDATE native_file_projection_outbox SET completed_at=NOW(), outcome='abandoned' WHERE file_id=$1", file_id)
        await not_ready()
        assert await projection._requeue_abandoned(pool, namespace_id=vault, file_id=file_id) == 1
        assert await worker.process_once() == 1
        assert (await query("old"))["total_resources"] == 0
        assert (await query("new"))["total_resources"] == 1

        await _publish_source(pool, file_id=file_id, vault_id=vault,
                              payload=payloads["invalid"], mime_type="text/plain", s3_key="invalid")
        await not_ready()
        assert await worker.process_once() == 1
        assert (await query())["total_resources"] == 0  # Verified non-text exclusion.
        await _publish_source(pool, file_id=file_id, vault_id=vault,
                              payload=payloads["v1"], mime_type="text/plain", s3_key="v1")
        assert await worker.process_once() == 1
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM vault_files WHERE id=$1", file_id)
                await projection.enqueue_native_file_projection_delete(
                    conn, file_id=file_id, namespace_id=vault, collection=None,
                    name="fixture.txt", actor="fixture-owner",
                )
        await not_ready()  # The old live Head is not an existing File.
        assert await worker.process_once() == 1
        assert (await query())["total_resources"] == 0
