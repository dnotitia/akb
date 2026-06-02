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


# ── INV-7b: delete_vault file-chunk outbox with S3 CONFIGURED (P1-1) ─


@pytest.mark.asyncio
async def test_inv7b_delete_vault_file_outbox_with_s3(pool, tmp_path, monkeypatch):
    """When S3 is configured, delete_vault deletes vault_files early (to
    issue S3 object deletes), so the file-chunk outbox enqueue must read
    the file ids BEFORE that delete — otherwise file chunks CASCADE out
    of PG with no vector_delete_outbox row and orphan in the vector store.

    The default-env inv7 test does not catch this because the audit stack
    has no S3 (the early `DELETE FROM vault_files` branch is skipped). Here
    we force S3 on and stub the adapter so the early delete runs.
    """
    from app.config import settings
    from app.services.role_sync import RoleSync, set_role_sync, get_role_sync
    from app.services import access_service

    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    monkeypatch.setattr(settings, "s3_endpoint_url", "http://stub-s3:9000")
    # Stub the S3 adapter so the early per-file delete loop is a no-op.
    from app.services.adapters import s3_adapter
    monkeypatch.setattr(s3_adapter, "delete", lambda *_a, **_k: None)

    try:
        get_role_sync()
    except RuntimeError:
        set_role_sync(RoleSync(pool))

    async with pool.acquire() as conn:
        admin_id = await conn.fetchval(
            "INSERT INTO users (id, username, email, password_hash, is_admin) "
            "VALUES (gen_random_uuid(), $1, $2, 'x', true) RETURNING id",
            f"inv7badm-{uuid.uuid4().hex[:6]}",
            f"inv7badm-{uuid.uuid4().hex[:6]}@test.local",
        )
    vault_repo = VaultRepository(pool)
    name = f"_inv7b_{uuid.uuid4().hex[:8]}"
    vid = await vault_repo.create(
        name=name, description="inv7b", git_path=f"/tmp/{name}.git", owner_id=admin_id,
    )

    file_id = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO vault_files (id, vault_id, name, s3_key, mime_type, size_bytes, created_at) VALUES "
            "($1, $2, 'f.bin', $3, 'application/octet-stream', 0, NOW())",
            file_id, vid, f"_inv7b_{file_id}",
        )
        await conn.execute(
            "INSERT INTO chunks (id, source_type, source_id, vault_id, chunk_index, content) "
            "VALUES (gen_random_uuid(), 'file', $1, $2, 0, 'f')",
            file_id, vid,
        )

    await access_service.delete_vault(user_id=str(admin_id), vault_name=name)

    async with pool.acquire() as conn:
        post = await conn.fetchval(
            "SELECT COUNT(*) FROM chunks WHERE source_id = $1", file_id,
        )
        outbox = await conn.fetchval(
            "SELECT COUNT(*) FROM vector_delete_outbox WHERE source_id = $1", file_id,
        )

    assert post == 0, f"file chunk should be gone from PG, got {post}"
    assert outbox == 1, (
        "file chunk must be enqueued in vector_delete_outbox even when S3 is "
        f"configured (vault_files deleted early); got {outbox}"
    )


# ── P1-2: FileService.delete must roll back on chunk-delete failure ──


@pytest.mark.asyncio
async def test_p1_2_file_delete_rolls_back_on_chunk_failure(pool, monkeypatch):
    """FileService.delete wraps the chunk/outbox cleanup in the file-delete
    transaction. If delete_file_chunks raises (e.g. a failed outbox
    enqueue), the WHOLE delete must roll back — otherwise the vault_files
    row + s3-delete enqueue commit while the chunk's vector point orphans.
    Pre-fix the exception was swallowed and the delete committed anyway.
    """
    from app.services import file_service as fs_mod
    from app.services.file_service import FileService

    vault_repo = VaultRepository(pool)
    name = f"_p12_{uuid.uuid4().hex[:8]}"
    vid = await vault_repo.create(
        name=name, description="p12", git_path=f"/tmp/{name}.git", owner_id=None,
    )
    file_id = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO vault_files (id, vault_id, name, s3_key, mime_type, size_bytes, created_at) VALUES "
            "($1, $2, 'f.bin', $3, 'application/octet-stream', 0, NOW())",
            file_id, vid, f"_p12_{file_id}",
        )

    # Force the chunk delete to blow up the way a failed outbox enqueue would.
    async def _boom(*_a, **_k):
        raise RuntimeError("simulated outbox enqueue failure")

    monkeypatch.setattr(fs_mod, "delete_file_chunks", _boom)

    with pytest.raises(Exception):
        await FileService().delete(vid, str(file_id), actor_id="p12")

    # The file row must survive — the transaction rolled back.
    async with pool.acquire() as conn:
        still = await conn.fetchval(
            "SELECT COUNT(*) FROM vault_files WHERE id = $1", file_id,
        )
    assert still == 1, "file delete must roll back when chunk delete fails (was swallowed pre-fix)"


# ── delete_publications_for_document UUID branch must match canonical URI ──


@pytest.mark.asyncio
async def test_delete_publications_for_document_uuid_canonical(pool):
    """delete_publications_for_document(UUID) previously built a legacy
    `akb://V/doc/{coll}/{name}` URI that never matched the canonical
    `akb://V/coll/{coll}/doc/{name}` stored in publications.resource_uri,
    so the cascade silently left orphan publications. The UUID branch now
    builds the URI via doc_uri so the DELETE actually matches.
    """
    from app.services.publication_service import delete_publications_for_document

    vault_repo = VaultRepository(pool)
    name = f"_pubdel_{uuid.uuid4().hex[:8]}"
    vid = await vault_repo.create(
        name=name, description="pubdel", git_path=f"/tmp/{name}.git", owner_id=None,
    )
    did = uuid.uuid4()
    canonical = f"akb://{name}/coll/incidents/doc/report.md"
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO documents (id, vault_id, path, title, doc_type, status, "
            "created_at, updated_at, current_commit, tags, metadata) VALUES "
            "($1, $2, 'incidents/report.md', 'r', 'report', 'draft', NOW(), NOW(), "
            "'cafef00d', '{}'::text[], '{}'::jsonb)",
            did, vid,
        )
        await conn.execute(
            "INSERT INTO publications (id, slug, vault_id, resource_type, resource_uri, created_at) "
            "VALUES (gen_random_uuid(), $1, $2, 'document', $3, NOW())",
            f"slug{uuid.uuid4().hex[:6]}", vid, canonical,
        )

    deleted = await delete_publications_for_document(did)  # the UUID branch

    async with pool.acquire() as conn:
        remaining = await conn.fetchval(
            "SELECT COUNT(*) FROM publications WHERE vault_id = $1", vid,
        )
        await conn.execute("DELETE FROM vaults WHERE id = $1", vid)

    assert deleted == 1, "UUID branch must materialize the canonical URI and match"
    assert remaining == 0, "publication should be cascade-deleted, not orphaned"


# ── P2: embedding response reordered by `index`, not array position ──


@pytest.mark.asyncio
async def test_p2_embed_index_reorder():
    """_embed_call must pair each output to its input via the response
    item's `index` field, not array order. A gateway that returns items
    out of order would otherwise attach vectors to the wrong chunks.
    """
    from app.services import index_service

    class _Resp:
        status_code = 200
        def __init__(self, payload):
            self._p = payload
        def json(self):
            return self._p

    class _Client:
        def __init__(self, payload):
            self._p = payload
        async def post(self, *_a, **_k):
            return _Resp(self._p)

    # Response deliberately OUT OF ORDER (index 2, 0, 1).
    payload = {"data": [
        {"index": 2, "embedding": [2.0]},
        {"index": 0, "embedding": [0.0]},
        {"index": 1, "embedding": [1.0]},
    ]}
    status, embs, _ = await index_service._embed_call(_Client(payload), ["a", "b", "c"], 5.0)
    assert status == "ok"
    assert embs == [[0.0], [1.0], [2.0]], "vectors must be positionally aligned by index"

    # A short / gapped index set is a malformed response → transient.
    bad = {"data": [{"index": 0, "embedding": [0.0]}, {"index": 5, "embedding": [9.0]}]}
    status2, embs2, _ = await index_service._embed_call(_Client(bad), ["a", "b"], 5.0)
    assert status2 == "transient" and embs2 is None


# ── P2: alter_table reserved-column guard ─────────────────────────


@pytest.mark.asyncio
async def test_p2_alter_table_reserved_guard(pool):
    from app.services import table_service
    from app.services.role_sync import RoleSync, set_role_sync, get_role_sync

    set_role_sync(RoleSync(pool))

    vault_repo = VaultRepository(pool)
    name = f"_p2alter_{uuid.uuid4().hex[:8]}"
    vid = await vault_repo.create(name=name, description="x", git_path=f"/tmp/{name}.git", owner_id=None)
    await get_role_sync().on_vault_create(vid, None)
    await table_service.create_table(vid, "items", [{"name": "label", "type": "text"}], actor_id="t")

    # dropping the PK must be rejected
    with pytest.raises(ValueError):
        await table_service.alter_table(vid, "items", actor_id="t", drop_columns=["id"])
    # adding a reserved name must be rejected
    with pytest.raises(ValueError):
        await table_service.alter_table(vid, "items", actor_id="t",
                                        add_columns=[{"name": "created_at", "type": "text"}])
    # renaming onto a reserved name must be rejected
    with pytest.raises(ValueError):
        await table_service.alter_table(vid, "items", actor_id="t",
                                        rename_columns={"label": "id"})
    # the PK survives all rejected alters
    async with pool.acquire() as conn:
        has_id = await conn.fetchval(
            "SELECT 1 FROM information_schema.columns WHERE table_name = $1 AND column_name = 'id'",
            table_service.table_data_repo.pg_table_name(name, "items"),
        )
        await conn.execute("DELETE FROM vaults WHERE id = $1", vid)
    assert has_id == 1, "PK column must still exist after rejected drop"


# ── P2: archived vault is read-only via akb_sql ───────────────────


@pytest.mark.asyncio
async def test_p2_archived_vault_blocks_writes(pool):
    from app.services import table_service
    from app.services.role_sync import RoleSync, set_role_sync, get_role_sync
    from app.services.user_sql_executor import UserSqlExecutor, set_user_sql_executor

    set_role_sync(RoleSync(pool))
    set_user_sql_executor(UserSqlExecutor(pool))

    async with pool.acquire() as conn:
        admin = await conn.fetchval(
            "INSERT INTO users (id, username, email, password_hash, is_admin) "
            "VALUES (gen_random_uuid(), $1, $2, 'x', true) RETURNING id",
            f"p2a{uuid.uuid4().hex[:6]}", f"p2a{uuid.uuid4().hex[:6]}@t.local",
        )
    vault_repo = VaultRepository(pool)
    name = f"_p2arch_{uuid.uuid4().hex[:8]}"
    vid = await vault_repo.create(name=name, description="x", git_path=f"/tmp/{name}.git", owner_id=admin)
    await get_role_sync().on_vault_create(vid, admin)
    await table_service.create_table(vid, "items", [{"name": "label", "type": "text"}], actor_id="t")
    await table_service.execute_sql(vault_names=[name], user_id=str(admin),
                                    sql="INSERT INTO items (label) VALUES ('a')", is_admin=True)

    # archive the vault (status flip only, no role DDL — as archive_vault does)
    async with pool.acquire() as conn:
        await conn.execute("UPDATE vaults SET status = 'archived' WHERE id = $1", vid)

    # WRITE must be blocked at the app layer
    w = await table_service.execute_sql(vault_names=[name], user_id=str(admin),
                                        sql="INSERT INTO items (label) VALUES ('b')", is_admin=True)
    assert w.get("code") == "vault_archived", f"archived write should be blocked, got {w}"

    # READ must still work
    r = await table_service.execute_sql(vault_names=[name], user_id=str(admin),
                                        sql="SELECT label FROM items", is_admin=True)
    assert r.get("total") == 1, f"archived read should still work, got {r}"

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM vaults WHERE id = $1", vid)


# ── P2: collection delete handles tables ──────────────────────────


@pytest.mark.asyncio
async def test_p2_collection_delete_handles_tables(pool):
    from app.services import table_service
    from app.services.collection_service import CollectionService, CollectionNotEmptyError
    from app.services.role_sync import RoleSync, set_role_sync, get_role_sync

    set_role_sync(RoleSync(pool))

    vault_repo = VaultRepository(pool)
    name = f"_p2coll_{uuid.uuid4().hex[:8]}"
    vid = await vault_repo.create(name=name, description="x", git_path=f"/tmp/{name}.git", owner_id=None)
    await get_role_sync().on_vault_create(vid, None)
    # a table inside collection 'specs'
    await table_service.create_table(vid, "items", [{"name": "label", "type": "text"}],
                                     actor_id="t", collection="specs")

    svc = CollectionService()
    # non-recursive delete of a table-only collection must NOT silently succeed
    with pytest.raises(CollectionNotEmptyError) as ei:
        await svc.delete(vault=name, path="specs", recursive=False, agent_id="t")
    assert ei.value.table_count == 1

    # recursive delete actually drops the table (registry + dynamic table)
    out = await svc.delete(vault=name, path="specs", recursive=True, agent_id="t")
    assert out["deleted_tables"] == 1

    async with pool.acquire() as conn:
        reg = await conn.fetchval("SELECT COUNT(*) FROM vault_tables WHERE vault_id = $1", vid)
        pg_tbl = await conn.fetchval(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = $1",
            table_service.table_data_repo.pg_table_name(name, "items"),
        )
        await conn.execute("DELETE FROM vaults WHERE id = $1", vid)
    assert reg == 0, "registry row must be gone"
    assert pg_tbl == 0, "dynamic PG table must be dropped"


# ── E06: collection-retirement vs PUT FK race ──────────────────────


@pytest.mark.asyncio
async def test_create_with_deleted_collection_raises_conflict(pool, vault):
    """A document insert whose `collection_id` references a collection that no
    longer exists must surface as ConflictError (409), not an unhandled
    asyncpg.ForeignKeyViolationError (500).

    This is the E06 'collection retirement race': a PUT's get_or_create
    observes a collection, a concurrent recursive DELETE removes it, then the
    PUT's INSERT (still referencing the vanished id) trips
    `documents_collection_id_fkey`. `collection_id` is ON DELETE SET NULL, so
    the delete side re-homes existing docs — but a NEW insert against the gone
    id is an FK violation that must become a clean, retryable 409.
    """
    from datetime import datetime, timezone

    from app.exceptions import ConflictError

    doc_repo = DocumentRepository(pool)
    bogus_collection_id = uuid.uuid4()  # never inserted into `collections`
    now = datetime.now(timezone.utc)

    with pytest.raises(ConflictError):
        await doc_repo.create(
            vault_id=vault["id"], collection_id=bogus_collection_id,
            path="retire/lost-race.md", title="Doc", doc_type="note",
            status="draft", summary=None, domain=None, created_by=None, now=now,
            commit_hash="0" * 40, content_hash="h", hash_algorithm="sha256",
            tags=[], metadata={},
        )
