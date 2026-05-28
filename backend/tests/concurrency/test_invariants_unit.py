"""Unit-level invariant tests for audit-v2 fixes that don't fit a
shell-bombardment shape.

Covers:
- INV-3 SessionService.end_session FOR UPDATE — N concurrent calls
  must produce exactly one "ended" result and exactly one
  auto_summarize_session (i.e. one row in `memories`).
- INV-5 sparse_encoder.recompute_stats pg_try_advisory_lock — N
  concurrent recompute calls; only the lock holder writes, the rest
  return ``{"skipped": true}``.
- INV-6 metadata_worker stale guard — DocumentRepository.mark_llm_metadata_filled
  honours ``expected_blob``; a stale write after the reconciler updated
  the row returns False and leaves llm_metadata_at unchanged.
- INV-7 delete_vault orphan chunks — after delete_vault, no chunks rows
  remain for any source under that vault (documents OR tables OR files).

Talks to a real Postgres via `AKB_TEST_DSN`; skips when unreachable so
the suite runs unattended on machines without a dev DB. The audit
Docker stack default is `postgresql://akb:akb@localhost:5433/akb`.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from app.repositories.document_repo import DocumentRepository
from app.repositories.vault_repo import VaultRepository
from app.services.session_service import SessionService


_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:5433/akb",
)


async def _can_connect(dsn: str) -> bool:
    try:
        conn = await asyncpg.connect(dsn, timeout=2.0)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


@pytest_asyncio.fixture
async def pool():
    if not await _can_connect(_DSN):
        pytest.skip(f"Postgres not reachable at {_DSN}")
    pool = await asyncpg.create_pool(dsn=_DSN, min_size=2, max_size=10)
    init_sql = (
        Path(__file__).resolve().parents[2] / "app" / "db" / "init.sql"
    ).read_text()
    async with pool.acquire() as conn:
        await conn.execute(init_sql)
    # The session_service uses get_pool() (module-global). Wire it to
    # this test pool for the duration of the test.
    from app.db import postgres as pg_mod
    prev = pg_mod._pool
    pg_mod._pool = pool
    try:
        yield pool
    finally:
        pg_mod._pool = prev
        await pool.close()


@pytest_asyncio.fixture
async def vault(pool):
    vault_repo = VaultRepository(pool)
    name = f"_inv_unit_{uuid.uuid4().hex[:8]}"
    vid = await vault_repo.create(
        name=name,
        description="ephemeral unit-test vault",
        git_path=f"/tmp/{name}.git",
        owner_id=None,
    )
    try:
        yield {"id": vid, "name": name}
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM vaults WHERE id = $1", vid)


# ── INV-3: end_session FOR UPDATE dedup ────────────────────────────


@pytest.mark.asyncio
async def test_inv3_end_session_dedup(pool, vault):
    """Concurrent end_session() must collapse to one ended state and
    fire auto_summarize_session exactly once.
    """
    svc = SessionService()
    started = await svc.start_session(vault["name"], agent_id="inv3", context=None)
    sid = started["session_id"]

    # Spawn a user so auto_summarize_session can attribute a memory.
    async with pool.acquire() as conn:
        uid = await conn.fetchval(
            "INSERT INTO users (id, username, email, password_hash, is_admin) "
            "VALUES (gen_random_uuid(), $1, $2, 'x', false) RETURNING id",
            f"inv3-{uuid.uuid4().hex[:6]}",
            f"inv3-{uuid.uuid4().hex[:6]}@test.local",
        )

    N = 10
    results = await asyncio.gather(
        *[svc.end_session(sid, summary=f"summary v{i}", user_id=str(uid)) for i in range(N)]
    )

    ended_ok = [r for r in results if "ended_at" in r and "error" not in r]
    already = [r for r in results if r.get("error") == "Session already ended"]
    other_err = [r for r in results if "error" in r and r["error"] != "Session already ended"]

    assert len(ended_ok) == 1, f"expected 1 successful end, got {len(ended_ok)}: {ended_ok}"
    assert len(already) == N - 1, f"expected {N-1} 'already ended', got {len(already)}: {already}"
    assert not other_err, f"unexpected errors: {other_err}"

    # auto_summarize_session must have fired exactly once → one memory row.
    async with pool.acquire() as conn:
        mem_count = await conn.fetchval(
            "SELECT COUNT(*) FROM memories WHERE session_id = $1",
            uuid.UUID(sid),
        )
    assert mem_count == 1, f"expected 1 auto-memory, got {mem_count}"


# ── INV-5: BM25 recompute_stats try_advisory_lock ──────────────────


@pytest.mark.asyncio
async def test_inv5_bm25_recompute_lock(pool, vault):
    """Concurrent recompute_stats: exactly one runs, the rest skip
    via pg_try_advisory_lock returning false."""
    from app.services import sparse_encoder

    # Need at least one chunk so the recompute has work to do.
    async with pool.acquire() as conn:
        doc_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO documents (id, vault_id, path, title, doc_type, status, "
            "created_at, updated_at, current_commit, tags, metadata) VALUES "
            "($1, $2, 'x.md', 'x', 'note', 'draft', NOW(), NOW(), 'cafef00d', "
            "'{}'::text[], '{}'::jsonb)",
            doc_id, vault["id"],
        )
        await conn.execute(
            "INSERT INTO chunks (id, source_type, source_id, vault_id, chunk_index, content) "
            "VALUES (gen_random_uuid(), 'document', $1, $2, 0, 'hello world')",
            doc_id, vault["id"],
        )

    N = 5
    results = await asyncio.gather(
        *[sparse_encoder.recompute_stats(batch_size=100) for _ in range(N)]
    )

    skipped = [r for r in results if r.get("skipped")]
    ran = [r for r in results if not r.get("skipped")]
    assert len(ran) == 1, f"expected 1 winner, got {len(ran)}: {ran}"
    assert len(skipped) == N - 1, f"expected {N-1} skipped, got {len(skipped)}"


# ── INV-6: metadata_worker stale guard via expected_blob ───────────


@pytest.mark.asyncio
async def test_inv6_mark_llm_metadata_stale_guard(pool, vault):
    """mark_llm_metadata_filled honours expected_blob: a worker that
    claimed at blob 'OLD' must NOT overwrite a row whose external_blob
    is now 'NEW'.
    """
    doc_repo = DocumentRepository(pool)
    doc_id = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO documents (id, vault_id, path, title, doc_type, status, "
            "source, external_blob, created_at, updated_at, current_commit, tags, metadata) VALUES "
            "($1, $2, 'ext.md', 't', 'note', 'draft', 'external_git', 'OLD', "
            "NOW(), NOW(), 'cafef00d', '{}'::text[], '{}'::jsonb)",
            doc_id, vault["id"],
        )

    now = datetime.now(timezone.utc)

    # 1) Happy path: expected_blob='OLD' matches → returns True, stamps llm_metadata_at.
    applied = await doc_repo.mark_llm_metadata_filled(
        doc_id=doc_id,
        summary="s1", tags=["a"], doc_type="note", domain="x", now=now,
        expected_blob="OLD",
    )
    assert applied is True
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT summary, llm_metadata_at FROM documents WHERE id = $1", doc_id)
    assert row["summary"] == "s1"
    assert row["llm_metadata_at"] is not None

    # 2) Stale path: reconciler swaps blob to 'NEW'; an in-flight worker
    #    that still has expected_blob='OLD' must be rejected.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE documents SET external_blob = 'NEW', summary = NULL, llm_metadata_at = NULL WHERE id = $1",
            doc_id,
        )
    applied2 = await doc_repo.mark_llm_metadata_filled(
        doc_id=doc_id,
        summary="STALE", tags=["x"], doc_type="note", domain="y", now=now,
        expected_blob="OLD",
    )
    assert applied2 is False
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT summary, llm_metadata_at FROM documents WHERE id = $1", doc_id)
    # Stale write must NOT have stamped anything.
    assert row["summary"] is None, f"stale write leaked: summary={row['summary']!r}"
    assert row["llm_metadata_at"] is None


# ── INV-7: delete_vault orphan chunks ──────────────────────────────


@pytest.mark.asyncio
async def test_inv7_delete_vault_no_orphan_chunks(pool, tmp_path, monkeypatch):
    """After delete_vault, no chunks remain that point at
    document/table/file ids that lived in the deleted vault — i.e. the
    per-source cleanup loop in access_service.delete_vault did its
    job (B-F8). We seed one of each source type, then verify zero
    orphans after the destructive call.
    """
    from app.config import settings
    from app.services.role_sync import RoleSync, set_role_sync
    from app.services import access_service

    # delete_vault calls GitService().cleanup_vault_dirs which insists on a
    # writeable storage_path. Default is /data/vaults — point it at a tmp dir.
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))

    # Lifecycle wiring expected by delete_vault → RoleSync grant_table_in_conn / on_table_drop
    try:
        from app.services.role_sync import get_role_sync
        get_role_sync()
    except RuntimeError:
        set_role_sync(RoleSync(pool))

    # check_vault_access requires admin (is_admin=true bypasses ownership).
    async with pool.acquire() as conn:
        admin_id = await conn.fetchval(
            "INSERT INTO users (id, username, email, password_hash, is_admin) "
            "VALUES (gen_random_uuid(), $1, $2, 'x', true) RETURNING id",
            f"inv7admin-{uuid.uuid4().hex[:6]}",
            f"inv7admin-{uuid.uuid4().hex[:6]}@test.local",
        )

    vault_repo = VaultRepository(pool)
    name = f"_inv7_{uuid.uuid4().hex[:8]}"
    vid = await vault_repo.create(
        name=name, description="inv7", git_path=f"/tmp/{name}.git", owner_id=admin_id,
    )

    doc_id = uuid.uuid4()
    tbl_id = uuid.uuid4()
    file_id = uuid.uuid4()
    async with pool.acquire() as conn:
        # documents row + its chunk
        await conn.execute(
            "INSERT INTO documents (id, vault_id, path, title, doc_type, status, "
            "created_at, updated_at, current_commit, tags, metadata) VALUES "
            "($1, $2, 'd.md', 'd', 'note', 'draft', NOW(), NOW(), 'cafef00d', "
            "'{}'::text[], '{}'::jsonb)",
            doc_id, vid,
        )
        await conn.execute(
            "INSERT INTO chunks (id, source_type, source_id, vault_id, chunk_index, content) "
            "VALUES (gen_random_uuid(), 'document', $1, $2, 0, 'd')",
            doc_id, vid,
        )
        # vault_tables row + its chunk
        await conn.execute(
            "INSERT INTO vault_tables (id, vault_id, name, description, columns, created_at) VALUES "
            "($1, $2, 'tbl_a', 't', '[]'::jsonb, NOW())",
            tbl_id, vid,
        )
        await conn.execute(
            "INSERT INTO chunks (id, source_type, source_id, vault_id, chunk_index, content) "
            "VALUES (gen_random_uuid(), 'table', $1, $2, 0, 't')",
            tbl_id, vid,
        )
        # Dynamic PG table the registry points at; delete_vault drops it.
        await conn.execute(
            f'CREATE TABLE IF NOT EXISTS "vt_{name}__tbl_a" (id UUID PRIMARY KEY)'
        )
        # vault_files row + its chunk
        await conn.execute(
            "INSERT INTO vault_files (id, vault_id, name, s3_key, mime_type, size_bytes, created_at) VALUES "
            "($1, $2, 'f.bin', $3, 'application/octet-stream', 0, NOW())",
            file_id, vid, f"_inv7_{file_id}",
        )
        await conn.execute(
            "INSERT INTO chunks (id, source_type, source_id, vault_id, chunk_index, content) "
            "VALUES (gen_random_uuid(), 'file', $1, $2, 0, 'f')",
            file_id, vid,
        )

    # Pre-condition: 3 chunks present.
    async with pool.acquire() as conn:
        pre = await conn.fetchval(
            "SELECT COUNT(*) FROM chunks WHERE source_id IN ($1, $2, $3)",
            doc_id, tbl_id, file_id,
        )
    assert pre == 3

    await access_service.delete_vault(user_id=str(admin_id), vault_name=name)

    async with pool.acquire() as conn:
        post = await conn.fetchval(
            "SELECT COUNT(*) FROM chunks WHERE source_id IN ($1, $2, $3)",
            doc_id, tbl_id, file_id,
        )
        outbox = await conn.fetchval(
            "SELECT COUNT(*) FROM vector_delete_outbox WHERE source_id IN ($1, $2, $3)",
            doc_id, tbl_id, file_id,
        )
        vault_still = await conn.fetchval("SELECT COUNT(*) FROM vaults WHERE id = $1", vid)

    assert vault_still == 0, "vault row was not deleted"
    assert post == 0, f"orphan chunks remain after delete_vault: {post}"
    assert outbox == 3, (
        "vector_delete_outbox should carry the three chunk ids forward "
        f"for the delete worker; got {outbox}"
    )
