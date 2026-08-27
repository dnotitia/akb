"""Unit-level invariant tests for audit-v2 fixes that don't fit a
shell-bombardment shape.

Covers:
- INV-5 sparse_encoder.recompute_stats pg_try_advisory_lock — N
  concurrent recompute calls; only the lock holder writes, the rest
  return ``{"skipped": true}``.
- INV-6 metadata_worker stale guard — DocumentRepository.mark_llm_metadata_filled
  honours ``expected_blob``; a stale write after the reconciler updated
  the row returns False and leaves llm_metadata_at unchanged.
- INV-7 delete_vault orphan chunks — after delete_vault, no chunks rows
  remain for any source under that vault (documents OR tables OR files).

(INV-3 SessionService.end_session FOR UPDATE was retired in v0.5.0
alongside the memory-feature removal — the underlying service no
longer exists.)

- The publication cleanup helpers' `conn=` contract — the DELETE must
  run inside the caller's transaction, so a rollback resurrects the
  publication row.

Talks to a real Postgres via `AKB_TEST_DSN`; skips when unreachable so
the suite runs unattended on machines without a dev DB. The audit
Docker stack default is `postgresql://akb:akb@localhost:5433/akb`.

This file is listed in the DB-backed job of `backend-pytest.yml`, which
sets `AKB_TEST_DSN` and `REQUIRE_REAL_PG=1` — under that flag an
unreachable DB fails instead of skipping, so the gate cannot go quietly
green. The `pool` fixture applies init.sql *and* the migrations, because
CI hands it an empty database.
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
        # This file is in the DB-backed CI job, where an unreachable
        # Postgres means the gate silently stopped firing — fail loudly
        # rather than skip into a false green. Locally it still skips.
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"REQUIRE_REAL_PG=1 but Postgres is not reachable at {_DSN}")
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
        # init.sql is only half the schema: `events` (migration 015),
        # `s3_delete_outbox` (019), `chunks.vault_id` (014) and friends are
        # created by migrations, and tests below need them. Against the
        # empty database CI creates, init.sql alone leaves this file red.
        # Drive the app's own runner rather than a hand-copied DDL list
        # that would rot the first time someone adds a migration. This has
        # to run AFTER the _pool wiring above — _apply_migrations resolves
        # its connection through get_pool(). The schema_migrations ledger
        # makes every run after the first a single SELECT.
        await pg_mod._apply_migrations()
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


@pytest.mark.asyncio
async def test_document_image_write_and_vault_lock_share_one_order(pool, vault):
    """A manifest write finishes while a concurrent vault lifecycle lock waits.

    The writer takes vault -> document -> asset, matching deletion. The retained
    manifest has no second direct FK to vaults; its composite asset FK already
    owns that lifecycle and avoids reacquiring the parent after child locks.
    """
    document_id = uuid.uuid4()
    asset_id = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO documents (id, vault_id, path, title, doc_type, status, "
            "created_at, updated_at, current_commit, tags, metadata) VALUES "
            "($1, $2, 'image-lock.md', 'image lock', 'note', 'draft', NOW(), NOW(), "
            "'abcdef1', '{}'::text[], '{}'::jsonb)",
            document_id, vault["id"],
        )
        await conn.execute(
            "INSERT INTO vault_files (id, vault_id, kind, upload_state, name, s3_key, "
            "mime_type, size_bytes, content_hash, hash_algorithm, hash_verified_at, "
            "created_by, attachment_claimed_at) VALUES "
            "($1, $2, 'attachment', 'confirmed', 'image.png', $3, 'image/png', 1, "
            "$4, 'sha256', NOW(), 'tester', NOW())",
            asset_id, vault["id"], f"{vault['name']}/.akb-assets/{asset_id}/image.png",
            "0" * 64,
        )

        direct_parent_fks = await conn.fetchval(
            """
            SELECT COUNT(*)
              FROM pg_constraint
             WHERE conrelid = 'document_asset_revision_refs'::regclass
               AND confrelid = 'vaults'::regclass
               AND contype = 'f'
            """
        )
        assert direct_parent_fks == 0

    async with pool.acquire() as writer, pool.acquire() as lifecycle:
        writer_tx = writer.transaction()
        await writer_tx.start()
        await writer.fetchval(
            "SELECT id FROM vaults WHERE id = $1 FOR KEY SHARE", vault["id"],
        )
        await writer.fetchval(
            "SELECT id FROM documents WHERE id = $1 FOR UPDATE", document_id,
        )
        await writer.fetchval(
            "SELECT id FROM vault_files WHERE id = $1 FOR UPDATE", asset_id,
        )

        async def lock_vault_then_children() -> None:
            async with lifecycle.transaction():
                await lifecycle.fetchval(
                    "SELECT id FROM vaults WHERE id = $1 FOR UPDATE", vault["id"],
                )
                await lifecycle.fetchval(
                    "SELECT id FROM vault_files WHERE id = $1 FOR UPDATE", asset_id,
                )

        lifecycle_task = asyncio.create_task(lock_vault_then_children())
        await asyncio.sleep(0.05)
        assert not lifecycle_task.done()

        await writer.execute(
            "INSERT INTO document_asset_revision_refs "
            "(vault_id, document_path, commit_hash, asset_id, retain_until) "
            "VALUES ($1, 'image-lock.md', 'abcdef1', $2, NOW() + INTERVAL '1 day')",
            vault["id"], asset_id,
        )
        await writer_tx.commit()
        await asyncio.wait_for(lifecycle_task, timeout=2)


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
    """When S3 is configured, delete_vault records both durable outboxes.

    The file ids must reach the vector outbox before the metadata cascade, and
    each immutable object key must reach the S3 outbox in the same transaction
    so a transient store failure cannot orphan bytes after the vault row is
    gone.

    The default-env inv7 test does not catch this because the audit stack
    has no S3 (the early `DELETE FROM vault_files` branch is skipped). Here
    We force S3 on so the explicit metadata delete and object outbox path run.
    """
    from app.config import settings
    from app.services.role_sync import RoleSync, set_role_sync, get_role_sync
    from app.services import access_service

    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    monkeypatch.setattr(settings, "s3_endpoint_url", "http://stub-s3:9000")
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

    pending_file_id = uuid.uuid4()
    confirmed_file_id = uuid.uuid4()
    pending_key = f"_inv7b_{pending_file_id}"
    confirmed_key = f"_inv7b_{confirmed_file_id}"
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO vault_files "
            "(id, vault_id, name, s3_key, mime_type, size_bytes, upload_state, created_at) VALUES "
            "($1, $2, 'f.bin', $3, 'application/octet-stream', 0, 'pending', NOW())",
            pending_file_id, vid, pending_key,
        )
        await conn.execute(
            "INSERT INTO vault_files "
            "(id, vault_id, name, s3_key, mime_type, size_bytes, upload_state, "
            "hash_verified_at, created_at) VALUES "
            "($1, $2, 'confirmed.bin', $3, 'application/octet-stream', 0, "
            "'confirmed', NOW(), NOW())",
            confirmed_file_id, vid, confirmed_key,
        )
        await conn.execute(
            "INSERT INTO chunks (id, source_type, source_id, vault_id, chunk_index, content) "
            "VALUES (gen_random_uuid(), 'file', $1, $3, 0, 'pending'), "
            "       (gen_random_uuid(), 'file', $2, $3, 0, 'confirmed')",
            pending_file_id, confirmed_file_id, vid,
        )

    await access_service.delete_vault(user_id=str(admin_id), vault_name=name)

    async with pool.acquire() as conn:
        post = await conn.fetchval(
            "SELECT COUNT(*) FROM chunks WHERE source_id = ANY($1::uuid[])",
            [pending_file_id, confirmed_file_id],
        )
        outbox = await conn.fetchval(
            "SELECT COUNT(*) FROM vector_delete_outbox WHERE source_id = ANY($1::uuid[])",
            [pending_file_id, confirmed_file_id],
        )
        pending_s3_outbox = await conn.fetchrow(
            """
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE next_attempt_at <= NOW()) AS immediate,
                   COUNT(*) FILTER (
                       WHERE next_attempt_at > NOW() + INTERVAL '23 hours'
                   ) AS reconciliation
              FROM s3_delete_outbox
             WHERE s3_key = $1
            """,
            pending_key,
        )
        confirmed_s3_outbox = await conn.fetchrow(
            """
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE next_attempt_at <= NOW()) AS immediate,
                   COUNT(*) FILTER (WHERE next_attempt_at > NOW()) AS delayed
              FROM s3_delete_outbox
             WHERE s3_key = $1
            """,
            confirmed_key,
        )

    assert post == 0, f"file chunk should be gone from PG, got {post}"
    assert outbox == 2, (
        "both file chunks must be enqueued in vector_delete_outbox when S3 is "
        f"configured (vault_files deleted early); got {outbox}"
    )
    assert dict(pending_s3_outbox) == {
        "total": 2, "immediate": 1, "reconciliation": 1,
    }, (
        "a pending upload must enqueue one immediate delete and one delayed "
        f"reconciliation after vault deletion; got {dict(pending_s3_outbox)}"
    )
    assert dict(confirmed_s3_outbox) == {
        "total": 1, "immediate": 1, "delayed": 0,
    }, (
        "a confirmed file must enqueue exactly one immediate object delete; "
        f"got {dict(confirmed_s3_outbox)}"
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


# ── The cascade must delete under the CANONICAL URI ──


@pytest.mark.asyncio
async def test_chokepoint_materializes_the_canonical_uri(pool):
    """A caller holding only a document id has to resolve it to the
    CANONICAL URI — `akb://V/coll/{coll}/doc/{name}` — before the cascade can
    match. An earlier incident built the pre-0.3.0 legacy shape
    (`akb://V/doc/{coll}/{name}`), which never equals what
    `publications.resource_uri` stores, so the cascade silently deleted
    nothing and left orphans.

    That resolution now lives in `DocumentRepository.delete_with_publications`,
    which reads the path back from the row it locked and builds the URI with
    `doc_uri`. (The helper itself no longer accepts ids at all — see
    `test_delete_publications_for_document_rejects_a_bare_id`.) Driving the
    chokepoint here tests the path that actually runs in production.
    """
    vault_repo = VaultRepository(pool)
    doc_repo = DocumentRepository(pool)
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

    async with pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        deleted = await doc_repo.delete_with_publications(conn, doc_id=did, vault_id=vid)
        await tx.commit()

    async with pool.acquire() as conn:
        remaining = await conn.fetchval(
            "SELECT COUNT(*) FROM publications WHERE vault_id = $1", vid,
        )
        docs_left = await conn.fetchval(
            "SELECT COUNT(*) FROM documents WHERE id = $1", did,
        )
        await conn.execute("DELETE FROM vaults WHERE id = $1", vid)

    assert deleted is True
    assert docs_left == 0
    assert remaining == 0, (
        "publication should be cascade-deleted, not orphaned — a legacy-shape "
        "URI would have matched nothing and left it behind"
    )


@pytest.mark.asyncio
async def test_delete_publications_for_document_rejects_a_bare_id(pool, vault):
    """The helper takes a canonical URI and nothing else.

    It used to accept a PG UUID too, resolving it through a join on
    `documents`. The two input forms had different ordering requirements and
    the signature said so nowhere: called after the row delete on the same
    connection, the id form found no row and returned 0 — no error, nothing
    deleted, publication orphaned. A delete path was correct only by accident
    of which argument it happened to pass, and "tidying" a call from a URI to
    `row["id"]` would have silently restored it. Rejecting ids outright
    is what makes that unrepresentable.
    """
    from app.services.publication_service import delete_publications_for_document

    vid, vname = vault["id"], vault["name"]
    did = uuid.uuid4()
    uri = f"akb://{vname}/coll/incidents/doc/report.md"
    slug = await _seed_publication(pool, vid, uri)

    for bad in (did, str(did), f"akb://{vname}/file/{did}", "not-a-uri", ""):
        with pytest.raises(ValueError, match="canonical document"):
            await delete_publications_for_document(bad, expected_vault_id=vid)

    # A rejected call must be a no-op, not a partial delete.
    assert await _publication_exists(pool, slug)
    assert await delete_publications_for_document(uri, expected_vault_id=vid) == 1


@pytest.mark.asyncio
async def test_deleting_a_brace_path_document_is_not_blocked_by_uri_validation(
    pool, vault,
):
    """A path `parse_uri` cannot parse must still be deletable.

    `parse_uri` rejects URIs containing braces — they are template
    placeholders — but external-git accepts upstream paths like
    `templates/{{cookiecutter.project}}/README.md`, so `doc_uri` can
    legitimately produce a URI nothing can parse. The delete path must match
    on equality and never parse: raising instead aborts the tombstone, rolls
    the transaction back, and holds the external-git sync cursor forever,
    because a per-file error there is retried and never skipped. One
    templated file upstream would wedge the entire mirror, and the document
    it failed to delete stays live and searchable.

    This is why the chokepoint calls `delete_publications_by_doc_uri` rather
    than the validating entry point.
    """
    from app.services.publication_service import delete_publications_by_doc_uri
    from app.services.uri_service import doc_uri, parse_uri

    vid, vname = vault["id"], vault["name"]
    path = "templates/{{cookiecutter.project_slug}}/README.md"
    uri = doc_uri(vname, path)

    # The premise: this URI is genuinely unparseable. If parse_uri ever starts
    # accepting braces this test still passes, but it stops testing anything —
    # so assert the premise rather than assuming it.
    assert parse_uri(uri) is None, "premise broken: parse_uri now accepts braces"

    slug = await _seed_publication(pool, vid, uri)
    assert await delete_publications_by_doc_uri(uri, expected_vault_id=vid) == 1
    assert not await _publication_exists(pool, slug)


# ── The publication cleanup helpers must join the caller's transaction ──
#
# Callers that delete a document/file inside their own explicit TX have to
# be able to run the publication cleanup on that same connection. If it
# runs on a separate pool connection the two commits are independent, and
# a crash between them leaves a publication row pointing at a deleted
# document — the exact stale row that lets a public slug reach a
# document at the reused path.


async def _seed_publication(
    pool, vault_id: uuid.UUID, uri: str, resource_type: str = "document",
) -> str:
    slug = f"slug{uuid.uuid4().hex[:6]}"
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO publications (id, slug, vault_id, resource_type, resource_uri, created_at) "
            "VALUES (gen_random_uuid(), $1, $2, $3, $4, NOW())",
            slug, vault_id, resource_type, uri,
        )
    return slug


async def _publication_exists(pool, slug: str) -> bool:
    async with pool.acquire() as conn:
        return bool(await conn.fetchval(
            "SELECT COUNT(*) FROM publications WHERE slug = $1", slug,
        ))


@pytest.mark.asyncio
async def test_delete_publications_for_document_joins_caller_tx(pool, vault):
    """`conn=` makes the DELETE part of the caller's transaction: rolling
    the caller back must resurrect the publication row. Without it the
    helper commits on its own connection and the rollback is a no-op.
    """
    from app.services.publication_service import delete_publications_for_document

    vid, vname = vault["id"], vault["name"]
    did = uuid.uuid4()
    uri = f"akb://{vname}/coll/incidents/doc/report.md"
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO documents (id, vault_id, path, title, doc_type, status, "
            "created_at, updated_at, current_commit, tags, metadata) VALUES "
            "($1, $2, 'incidents/report.md', 'r', 'report', 'draft', NOW(), NOW(), "
            "'cafef00d', '{}'::text[], '{}'::jsonb)",
            did, vid,
        )

    # Rolled back → the row must come back.
    slug = await _seed_publication(pool, vid, uri)
    async with pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        deleted = await delete_publications_for_document(uri, conn=conn)
        assert deleted == 1, "helper must still report the row it deleted"
        await tx.rollback()
    assert await _publication_exists(pool, slug), (
        "publication survived only if the DELETE ran inside the caller's TX; "
        "a separate connection would have committed it independently"
    )

    # Committed → the row is really gone (guards against a no-op 'fix').
    async with pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        deleted = await delete_publications_for_document(uri, conn=conn)
        await tx.commit()
    assert deleted == 1
    assert not await _publication_exists(pool, slug)

    # expected_vault_id still binds the DELETE on the caller's connection.
    slug = await _seed_publication(pool, vid, uri)
    async with pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        deleted = await delete_publications_for_document(
            uri, expected_vault_id=uuid.uuid4(), conn=conn,
        )
        await tx.commit()
    assert deleted == 0, "wrong expected_vault_id must not delete"
    assert await _publication_exists(pool, slug)


@pytest.mark.asyncio
async def test_delete_publications_for_file_joins_caller_tx(pool, vault):
    """Same contract for the file helper. File publications carry a UUID
    so they cannot be reoccupied, but a stale row is still wrong — and the
    inline collection delete needs to clean them up inside its own TX.
    """
    from app.services.publication_service import delete_publications_for_file

    vid, vname = vault["id"], vault["name"]
    file_id = uuid.uuid4()
    uri = f"akb://{vname}/file/{file_id}"
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO vault_files (id, vault_id, name, s3_key, created_at, updated_at) "
            "VALUES ($1, $2, 'diagram.png', $3, NOW(), NOW())",
            file_id, vid, f"files/{file_id}",
        )

    slug = await _seed_publication(pool, vid, uri, resource_type="file")
    async with pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        deleted = await delete_publications_for_file(file_id, vname, conn=conn)
        assert deleted == 1
        await tx.rollback()
    assert await _publication_exists(pool, slug), (
        "file publication must be deleted inside the caller's TX"
    )

    async with pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        deleted = await delete_publications_for_file(file_id, vname, conn=conn)
        await tx.commit()
    assert deleted == 1
    assert not await _publication_exists(pool, slug)


@pytest.mark.asyncio
async def test_delete_publications_for_file_without_conn(pool, vault):
    """Omitting `conn` keeps the pre-existing self-acquiring behaviour —
    the documents helper already has coverage above, this pins the file
    one so the refactor can't regress it.
    """
    from app.services.publication_service import delete_publications_for_file

    vid, vname = vault["id"], vault["name"]
    file_id = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO vault_files (id, vault_id, name, s3_key, created_at, updated_at) "
            "VALUES ($1, $2, 'diagram.png', $3, NOW(), NOW())",
            file_id, vid, f"files/{file_id}",
        )
    slug = await _seed_publication(
        pool, vid, f"akb://{vname}/file/{file_id}", resource_type="file",
    )

    deleted = await delete_publications_for_file(file_id, vname, expected_vault_id=vid)

    assert deleted == 1
    assert not await _publication_exists(pool, slug)


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


# ── P2: create_table reserved/duplicate/malformed column → 422 ────


@pytest.mark.asyncio
async def test_p2_create_table_bad_columns_are_validation_errors(pool):
    """Every reserved / duplicate / malformed / missing-name column on create
    must surface as a clean ValidationError (HTTP 422 / MCP invalid_argument),
    never a bare ValueError-as-500 or an uncaught asyncpg DuplicateColumnError.
    Regression: reef's POST /tables with an `id` column returned a 500."""
    from app.services import table_service
    from app.services.role_sync import RoleSync, set_role_sync, get_role_sync
    from app.exceptions import ValidationError

    set_role_sync(RoleSync(pool))
    vault_repo = VaultRepository(pool)
    name = f"_p2create_{uuid.uuid4().hex[:8]}"
    vid = await vault_repo.create(name=name, description="x", git_path=f"/tmp/{name}.git", owner_id=None)
    await get_role_sync().on_vault_create(vid, None)

    bad_payloads = [
        [{"name": "id", "type": "text"}],                      # reserved (reef's case)
        [{"name": "created_at", "type": "text"}],              # reserved
        [{"name": "title"}, {"name": "title"}],                # duplicate → would be 42701
        [{"name": "Title", "type": "text"}],                   # malformed (uppercase)
        [{"type": "text"}],                                    # missing "name"
    ]
    for i, cols in enumerate(bad_payloads):
        with pytest.raises(ValidationError):
            await table_service.create_table(vid, f"t_{i}", cols, actor_id="t")
        # IS-A ValueError too, so MCP `except ValueError` handlers still classify it.
        try:
            await table_service.create_table(vid, f"t_{i}b", cols, actor_id="t")
        except ValueError:
            pass
        else:
            raise AssertionError(f"payload {cols!r} should have raised")

    # A clean payload still succeeds and is queryable by its bare name.
    await table_service.create_table(
        vid, "reef_issues",
        [{"name": "reef_id", "type": "text"}, {"name": "title", "type": "text"}],
        actor_id="t",
    )
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM information_schema.tables WHERE table_name = $1",
            table_service.table_data_repo.pg_table_name(name, "reef_issues"),
        )
        await conn.execute("DELETE FROM vaults WHERE id = $1", vid)
    assert exists == 1, "the corrected (no reserved id) create must succeed"


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
    from app.exceptions import ForbiddenError
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

    # A writer-equivalent caller must not bypass the table endpoint's admin
    # boundary by deleting its parent collection. The denial happens before
    # either the registry or physical table is mutated.
    with pytest.raises(ForbiddenError, match="contains tables requires 'admin'"):
        await svc.delete(
            vault=name,
            path="specs",
            recursive=True,
            agent_id="t",
            allow_table_delete=False,
        )
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT COUNT(*) FROM vault_tables WHERE vault_id = $1", vid,
        ) == 1
        assert await conn.fetchval(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = $1",
            table_service.table_data_repo.pg_table_name(name, "items"),
        ) == 1

    # recursive delete actually drops the table (registry + dynamic table)
    out = await svc.delete(
        vault=name,
        path="specs",
        recursive=True,
        agent_id="t",
        allow_table_delete=True,
    )
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
            tags=[], metadata={}, vault_name=vault["name"],
        )


# ── The publish/delete TOCTOU: FOR UPDATE at the deletion chokepoint ──
#
# `create_publication` takes `FOR SHARE` on the documents row and then
# INSERTs. The deleter's own serialization is `pg_advisory_xact_lock` over
# (vault_id, path) — a namespace the publisher never touches, so the two do
# NOT serialize against each other. With the publication cleanup running
# before any conflicting row lock there is a window: cleanup finds nothing,
# the publisher takes FOR SHARE and commits its INSERT, and the deleter's
# `DELETE FROM documents` — which had been blocked on that share lock —
# then proceeds over a publication that survives. The orphan resolves by
# path, and `documents` is UNIQUE(vault_id, path), so the next document at
# that path is reached through the old slug.
#
# `DocumentRepository.delete_with_publications` takes FOR UPDATE *before*
# the cleanup, which makes both orderings safe.


async def _blocked_queries(watch, *, contains: str | None = None) -> list[str]:
    """Queries of every backend in this database currently waiting on a lock."""
    rows = await watch.fetch(
        "SELECT query FROM pg_stat_activity "
        " WHERE datname = current_database() AND wait_event_type = 'Lock' "
        "   AND pid <> pg_backend_pid()"
    )
    qs = [r["query"] for r in rows]
    if contains is not None:
        qs = [q for q in qs if contains.lower() in (q or "").lower()]
    return qs


async def _await_blocked(watch, *, count: int, contains: str | None = None,
                         what: str, timeout: float = 20.0) -> list[str]:
    """Block until `count` backends are waiting on a lock (optionally with
    `contains` in their query text), or fail.

    This is a wait-for-state, not a timing guess: the interleaving itself is
    pinned by transaction control plus PostgreSQL's lock queue, and each side
    is only released once the database itself reports it is parked on the
    lock. A timeout here means the expected contention never happened, which
    is a real failure, not flake — so it asserts rather than proceeding.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        qs = await _blocked_queries(watch, contains=contains)
        if len(qs) >= count:
            return qs
        if loop.time() > deadline:
            all_waiting = await _blocked_queries(watch)
            raise AssertionError(
                f"timed out waiting for {what}: expected >={count} blocked "
                f"backend(s)"
                + (f" matching {contains!r}" if contains else "")
                + f", saw {len(qs)}. All waiters: {all_waiting}"
            )
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_publication_waits_for_vault_before_locking_document(
    pool, vault, monkeypatch,
):
    """Publication creation follows the vault -> document lifecycle order."""
    from app.services import publication_service
    from app.services.publication_service import create_publication

    monkeypatch.setattr(
        publication_service.settings, "public_base_url",
        "https://race.test.local", raising=False,
    )
    vid, vname = vault["id"], vault["name"]
    did = uuid.uuid4()
    path = "reports/parent-lock.md"
    uri = f"akb://{vname}/coll/reports/doc/parent-lock.md"
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO documents (id, vault_id, path, title, doc_type, status, "
            "created_at, updated_at, current_commit, tags, metadata) VALUES "
            "($1, $2, $3, 'Parent lock', 'report', 'draft', NOW(), NOW(), "
            "'cafef00d', '{}'::text[], '{}'::jsonb)",
            did, vid, path,
        )

    doc_repo = DocumentRepository(pool)
    watch = await asyncpg.connect(_DSN)
    try:
        async with pool.acquire() as lifecycle:
            tx = lifecycle.transaction()
            await tx.start()
            await lifecycle.fetchval(
                "SELECT id FROM vaults WHERE id = $1 FOR UPDATE", vid,
            )

            publisher = asyncio.create_task(create_publication(
                vault_id=vid,
                resource_type="document",
                resource_uri=uri,
                document_id=did,
                title="Parent lock",
            ))
            await _await_blocked(
                watch,
                count=1,
                contains="FROM vaults",
                what="publication to wait on the parent vault",
            )

            # If the publisher had locked the document first, this call and
            # the publisher's later vault FK lock would form the inverse-order
            # cycle. It must complete while the publisher remains parked on
            # the parent.
            assert await doc_repo.delete_with_publications(
                lifecycle, doc_id=did, vault_id=vid,
            ) is True
            await tx.commit()

        with pytest.raises(ValueError) as exc:
            await asyncio.wait_for(publisher, timeout=2)
        assert str(exc.value) == (
            "Document not found (resource was deleted concurrently): " + uri
        )
    finally:
        await watch.close()


@pytest.mark.asyncio
async def test_publish_delete_race_leaves_no_orphan_publication(pool, vault, monkeypatch):
    """A publisher that commits *while a delete is in flight* must not leave
    its publication behind.

    The interleaving is forced, not hoped for. A third connection holds
    FOR UPDATE on the document row, so both contenders park in PostgreSQL's
    lock queue in a known order — publisher first, deleter second. Releasing
    that lock hands the row to the publisher, which inserts and commits, and
    only then to the deleter.

    Pre-fix this is RED: the deleter's publication cleanup had already run
    (against an empty table) before it ever queued for the row, so the
    publication it never saw survived the document it pointed at. With
    FOR UPDATE taken first, the deleter's cleanup runs downstream of the
    lock, sees the freshly committed row, and removes it.

    **What this test stopped proving.** The publication it creates is now
    bound (`document_id` is required for a document publication), so
    migration 058's ON DELETE CASCADE removes it too — the final assertion
    below holds even with the explicit cleanup deleted outright. Verified,
    not assumed. So this is now a test of the END STATE, and the FK is what
    guarantees it. The lock ordering and the explicit cleanup are what a
    publication the FK cannot reach still depends on, and that is pinned
    separately by
    ``test_delete_cleanup_removes_a_legacy_publication_the_cascade_cannot_reach``
    below. Do not delete this one in favour of that: between them they say
    the invariant holds for both populations.
    """
    from app.services import publication_service
    from app.services.publication_service import create_publication

    monkeypatch.setattr(
        publication_service.settings, "public_base_url",
        "https://race.test.local", raising=False,
    )

    vid, vname = vault["id"], vault["name"]
    did = uuid.uuid4()
    path = "reports/q3.md"
    uri = f"akb://{vname}/coll/reports/doc/q3.md"
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO documents (id, vault_id, path, title, doc_type, status, "
            "created_at, updated_at, current_commit, tags, metadata) VALUES "
            "($1, $2, $3, 'Q3', 'report', 'draft', NOW(), NOW(), 'cafef00d', "
            "'{}'::text[], '{}'::jsonb)",
            did, vid, path,
        )

    doc_repo = DocumentRepository(pool)
    blocker = await asyncpg.connect(_DSN)   # holds the row so we can order the queue
    watch = await asyncpg.connect(_DSN)     # observes who is parked on it
    pub_result: dict | None = None
    pub_error: Exception | None = None
    try:
        btx = blocker.transaction()
        await btx.start()
        await blocker.fetchval(
            "SELECT id FROM documents WHERE id = $1 FOR UPDATE", did,
        )

        # 1) Publisher queues FIRST. Its FOR SHARE conflicts with the
        #    blocker's FOR UPDATE, so it parks before inserting anything.
        pub_task = asyncio.create_task(create_publication(
            vault_id=vid, resource_type="document", resource_uri=uri,
            document_id=did, title="Q3",
        ))
        await _await_blocked(
            watch, count=1, contains="FOR SHARE",
            what="create_publication to park on the document row",
        )

        # 2) Deleter queues SECOND, behind the publisher.
        async def _delete() -> bool:
            async with pool.acquire() as c:
                tx = c.transaction()
                await tx.start()
                try:
                    out = await doc_repo.delete_with_publications(
                        c, doc_id=did, vault_id=vid,
                    )
                except BaseException:
                    await tx.rollback()
                    raise
                await tx.commit()
                return out

        del_task = asyncio.create_task(_delete())
        # Both contenders now park on the `documents` row: the publisher on
        # its FOR SHARE, the deleter on the chokepoint's FOR UPDATE (or, in
        # the pre-fix shape this test is built to catch, on its bare
        # `DELETE FROM documents`). Matching on the table name covers both
        # without letting an unrelated waiter satisfy the count.
        await _await_blocked(
            watch, count=2, contains="documents",
            what="the delete to park behind the publisher",
        )

        # 3) Release. Publisher commits its INSERT, then the deleter runs.
        await btx.commit()
        try:
            pub_result = await pub_task
        except ValueError as e:          # the publisher may legitimately lose
            pub_error = e
        deleted = await del_task
    finally:
        if not blocker.is_closed():
            await blocker.close()
        await watch.close()

    assert deleted is True, "the delete must have removed the document row"
    # Pin the interleaving this test exists to exercise. The lock queue is
    # FIFO, so the publisher — queued first — takes the row first, inserts,
    # and commits BEFORE the deleter runs. If it ever lost instead, the
    # orphan assertion below would pass for the wrong reason (that direction
    # was already safe before this fix), so a losing publisher is a broken
    # test, not a pass.
    assert pub_result is not None, (
        "the publisher was supposed to win the lock queue and commit its "
        f"INSERT before the delete ran; it failed instead: {pub_error!r}. "
        "The red interleaving was not exercised."
    )

    async with pool.acquire() as conn:
        orphans = await conn.fetch(
            "SELECT slug FROM publications WHERE resource_uri = $1", uri,
        )
        doc_rows = await conn.fetchval(
            "SELECT COUNT(*) FROM documents WHERE id = $1", did,
        )

    assert doc_rows == 0, "the document row must be gone"
    # Whichever side won the row, the invariant is the same: no publication
    # may outlive the document it points at. Here the publisher wins the
    # queue, so this is specifically asserting the deleter cleaned up a row
    # that did not exist when its cleanup would have run pre-fix.
    assert not orphans, (
        "ORPHAN PUBLICATION SURVIVED the delete "
        f"({[r['slug'] for r in orphans]}, publisher "
        f"{'succeeded' if pub_result else f'failed: {pub_error}'}). "
        "The next document created at this path would be reachable "
        "under that slug, reopened by the race."
    )


@pytest.mark.asyncio
async def test_delete_cleanup_removes_a_legacy_publication_the_cascade_cannot_reach(
    pool, vault,
):
    """The same lock-ordering proof, on the one population the FK is blind to.

    Migration 058's ON DELETE CASCADE only reaches publications that carry a
    `document_id`, and the column is nullable precisely because the backfill
    could not bind every pre-existing row. For a row it left NULL, the
    deleter's explicit publication cleanup is the ONLY thing that removes it
    — so this is where the FOR UPDATE-before-cleanup ordering still has to
    be correct, and the only place a test can still tell.

    The publisher here is hand-rolled rather than `create_publication`: that
    function now refuses to create an unbound document publication, which is
    the point of the change, so the legacy shape has to be written directly.
    What it reproduces is exactly the pre-058 publisher — take `FOR SHARE`
    on the document row, then INSERT — so the lock choreography under test is
    the real one.

    RED if the cleanup runs before the row lock, and equally RED if the
    cleanup is removed: nothing else can delete this row.
    """
    vid, vname = vault["id"], vault["name"]
    did = uuid.uuid4()
    path = "reports/legacy.md"
    uri = f"akb://{vname}/coll/reports/doc/legacy.md"
    # Only the publication's NULL `document_id` below is load-bearing here —
    # the document itself is ordinary, so it goes through the shared helper
    # rather than a hand-written INSERT that would read as deliberate.
    await _insert_doc(pool, doc_id=did, vault_id=vid, path=path, title="Legacy")

    doc_repo = DocumentRepository(pool)
    blocker = await asyncpg.connect(_DSN)
    watch = await asyncpg.connect(_DSN)
    try:
        btx = blocker.transaction()
        await btx.start()
        await blocker.fetchval(
            "SELECT id FROM documents WHERE id = $1 FOR UPDATE", did,
        )

        async def _publish_unbound() -> None:
            async with pool.acquire() as c:
                tx = c.transaction()
                await tx.start()
                try:
                    await c.fetchval(
                        "SELECT 1 FROM documents WHERE id = $1 AND vault_id = $2 "
                        "FOR SHARE",
                        did, vid,
                    )
                    await c.execute(
                        "INSERT INTO publications (slug, vault_id, resource_type, "
                        "resource_uri, document_id, mode, allow_embed, query_params) "
                        "VALUES ($1, $2, 'document', $3, NULL, 'live', true, "
                        "'{}'::jsonb)",
                        "legacyslug000001", vid, uri,
                    )
                except BaseException:
                    await tx.rollback()
                    raise
                await tx.commit()

        # Publisher queues FIRST, deleter SECOND — same order as the bound
        # case above, so the deleter's cleanup is the side under test.
        pub_task = asyncio.create_task(_publish_unbound())
        await _await_blocked(
            watch, count=1, contains="FOR SHARE",
            what="the legacy publisher to park on the document row",
        )

        async def _delete() -> bool:
            async with pool.acquire() as c:
                tx = c.transaction()
                await tx.start()
                try:
                    out = await doc_repo.delete_with_publications(
                        c, doc_id=did, vault_id=vid,
                    )
                except BaseException:
                    await tx.rollback()
                    raise
                await tx.commit()
                return out

        del_task = asyncio.create_task(_delete())
        await _await_blocked(
            watch, count=2, contains="documents",
            what="the delete to park behind the legacy publisher",
        )

        await btx.commit()
        await pub_task
        deleted = await del_task
    finally:
        if not blocker.is_closed():
            await blocker.close()
        await watch.close()

    assert deleted is True, "the delete must have removed the document row"

    async with pool.acquire() as conn:
        orphans = await conn.fetch(
            "SELECT slug, document_id FROM publications WHERE resource_uri = $1", uri,
        )
    assert not orphans, (
        "ORPHAN PUBLICATION SURVIVED the delete "
        f"({[dict(r) for r in orphans]}). Its `document_id` is NULL, so the "
        "foreign key could not take it — the deleter's own cleanup, running "
        "downstream of the row lock, is the only thing that removes it."
    )


@pytest.mark.asyncio
async def test_publish_aborts_when_the_delete_holds_the_row(pool, vault, monkeypatch):
    """The other interleaving: the deleter wins the row, so the publisher's
    FOR SHARE re-check finds nothing and `create_publication` fails closed
    instead of inserting a publication for a document that is already gone.

    This direction held before the chokepoint's FOR UPDATE too (the row
    DELETE takes its own exclusive lock), so it is a contract pin rather
    than a regression test — it is here so a future change to the publisher's
    re-check cannot silently turn a hard abort into an orphan.
    """
    from app.services import publication_service
    from app.services.publication_service import create_publication

    monkeypatch.setattr(
        publication_service.settings, "public_base_url",
        "https://race.test.local", raising=False,
    )

    vid, vname = vault["id"], vault["name"]
    did = uuid.uuid4()
    path = "reports/q4.md"
    uri = f"akb://{vname}/coll/reports/doc/q4.md"
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO documents (id, vault_id, path, title, doc_type, status, "
            "created_at, updated_at, current_commit, tags, metadata) VALUES "
            "($1, $2, $3, 'Q4', 'report', 'draft', NOW(), NOW(), 'cafef00d', "
            "'{}'::text[], '{}'::jsonb)",
            did, vid, path,
        )

    doc_repo = DocumentRepository(pool)
    watch = await asyncpg.connect(_DSN)
    try:
        async with pool.acquire() as c:
            tx = c.transaction()
            await tx.start()
            deleted = await doc_repo.delete_with_publications(
                c, doc_id=did, vault_id=vid,
            )
            assert deleted is True

            # Publisher parks on the uncommitted delete.
            pub_task = asyncio.create_task(create_publication(
                vault_id=vid, resource_type="document", resource_uri=uri,
                document_id=did, title="Q4",
            ))
            await _await_blocked(
                watch, count=1, contains="FOR SHARE",
                what="create_publication to park on the in-flight delete",
            )
            await tx.commit()

        with pytest.raises(ValueError, match="deleted concurrently"):
            await pub_task
    finally:
        await watch.close()

    async with pool.acquire() as conn:
        rows = await conn.fetchval(
            "SELECT COUNT(*) FROM publications WHERE resource_uri = $1", uri,
        )
    assert rows == 0, "a publication must not be created for a deleted document"


# ── The publisher's own handoff: resolve → INSERT ─────────────────────
#
# `create_publication_for_vault` resolves the caller's doc_id to a document
# on one pooled connection, and `create_publication` INSERTs on another.
# Nothing spans the two — no transaction, no lock, no snapshot — so the
# document is unheld in between. That gap is milliseconds wide and lives
# inside a single request; the two interleaving tests below force it open on
# purpose rather than waiting for it, because what is being pinned is not the
# odds of hitting it but which value carries identity across it. The first
# test takes no interleaving at all — it is the positive statement that the
# binding gets written on the ordinary path, without which the other two
# could be satisfied by a publisher that never succeeds.
#
# Before this change only the PATH crossed the gap, and the INSERT re-found
# the document by that path. `documents` is UNIQUE(vault_id, path) and paths
# are reusable, so "the document at path P" is not a stable name for a
# document — the publisher could resolve one document and publish another.
# Now the resolved id crosses, the re-check keys on it, and the row stores
# it (`publications.document_id`, migration 058).
#
# The interleaving is injected by wrapping `create_publication` itself: the
# wrapper runs at exactly the moment the resolve has finished and the INSERT
# has not started, which is the window, and then calls the real function
# with the arguments the resolve produced. Those arguments are captured, so
# a test cannot pass by the resolve having quietly seen the *new* document.


async def _insert_doc(pool, *, doc_id, vault_id, path, title) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO documents (id, vault_id, path, title, doc_type, status, "
            "created_at, updated_at, current_commit, tags, metadata) VALUES "
            "($1, $2, $3, $4, 'report', 'draft', NOW(), NOW(), 'cafef00d', "
            "'{}'::text[], '{}'::jsonb)",
            doc_id, vault_id, path, title,
        )


@pytest.mark.asyncio
async def test_publish_binds_the_publication_to_the_document_it_resolved(
    pool, vault, monkeypatch,
):
    """The invariant, stated positively: a publication created from here on
    carries the id of the document its publisher resolved.

    No interleaving — this is the ordinary path. It is the test that says the
    binding is written at all, so the two failure tests below cannot both be
    satisfied by a publisher that simply never succeeds.
    """
    from app.services import publication_service
    from app.services.publication_service import create_publication_for_vault
    from app.services.uri_service import doc_uri

    monkeypatch.setattr(
        publication_service.settings, "public_base_url",
        "https://race.test.local", raising=False,
    )

    vid, vname = vault["id"], vault["name"]
    did = uuid.uuid4()
    path = "reports/q5.md"
    await _insert_doc(pool, doc_id=did, vault_id=vid, path=path, title="Q5")

    pub = await create_publication_for_vault(
        vault_name=vname, resource_type="document", doc_id=path,
        title="Q5 (public)",
    )

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT document_id, vault_id, resource_uri FROM publications "
            " WHERE slug = $1",
            pub["slug"],
        )

    assert row is not None, "the publication row is missing"
    assert row["document_id"] == did, (
        "the publication is not bound to the document that was resolved "
        f"(document_id={row['document_id']}, expected {did}). A NULL here "
        "means the identity was dropped between resolve and INSERT and the "
        "row is held to its document by a reusable path alone."
    )
    # The composite FK already forbids a cross-vault pair; asserting it keeps
    # the expectation visible next to the id.
    assert row["vault_id"] == vid
    # `resource_uri` is retained and still written — derived, for documents,
    # from the same binding.
    assert row["resource_uri"] == doc_uri(vname, path)


@pytest.mark.asyncio
async def test_publish_refuses_when_another_document_takes_the_resolved_path(
    pool, vault, monkeypatch,
):
    """Document A is resolved, then deleted, and B is created at A's path —
    all before the INSERT. The publisher must fail, not publish B.

    The delete is clean (through the chokepoint, publications and all), so
    nothing earlier on this branch catches it: there is no orphan for the
    write-side guard to refuse, the delete had no publication to cascade, and
    B is in the same vault so the resolution binding is satisfied. The only
    thing that can tell A from B here is the id the publisher resolved.

    Pre-change this is RED: `create_publication` re-found the document by
    path, found B, and published it under a link the caller asked for A.
    """
    from app.services import publication_service
    from app.services.publication_service import create_publication_for_vault

    monkeypatch.setattr(
        publication_service.settings, "public_base_url",
        "https://race.test.local", raising=False,
    )

    vid, vname = vault["id"], vault["name"]
    did_a, did_b = uuid.uuid4(), uuid.uuid4()
    path = "reports/q6.md"
    await _insert_doc(pool, doc_id=did_a, vault_id=vid, path=path, title="A")

    doc_repo = DocumentRepository(pool)
    real_create = publication_service.create_publication
    handed_off: dict = {}

    async def _swap_the_document_then_insert(**kwargs):
        handed_off.update(kwargs)
        async with pool.acquire() as c:
            tx = c.transaction()
            await tx.start()
            await doc_repo.delete_with_publications(c, doc_id=did_a, vault_id=vid)
            await tx.commit()
        await _insert_doc(pool, doc_id=did_b, vault_id=vid, path=path, title="B")
        return await real_create(**kwargs)

    monkeypatch.setattr(
        publication_service, "create_publication", _swap_the_document_then_insert,
    )

    with pytest.raises(ValueError, match="deleted concurrently"):
        await create_publication_for_vault(
            vault_name=vname, resource_type="document", doc_id=path,
            title="A (public)",
        )

    # Pin that the resolve really did see A. Without this the test could pass
    # for the wrong reason — a resolve that ran late and never held A at all.
    assert handed_off.get("document_id") == did_a, (
        "the resolve did not hand A's id to the INSERT "
        f"(got {handed_off.get('document_id')!r}); the window this test "
        "exists to force was not exercised."
    )

    async with pool.acquire() as conn:
        pubs = await conn.fetch(
            "SELECT slug, document_id FROM publications WHERE vault_id = $1", vid,
        )
        b_still_there = await conn.fetchval(
            "SELECT COUNT(*) FROM documents WHERE id = $1", did_b,
        )
    assert b_still_there == 1, "B should be untouched — it was never the target"
    assert not pubs, (
        f"a publication was created anyway: {[dict(r) for r in pubs]}. "
        "Whoever wrote B never asked for it to be publicly readable."
    )


@pytest.mark.asyncio
async def test_publish_refuses_when_the_resolved_document_moves_off_its_path(
    pool, vault, monkeypatch,
):
    """The other half of the same gap: A is not deleted, it MOVES, and B
    lands on the path A vacated.

    By path this was indistinguishable from a plain delete — worse, it was
    satisfied by B and published it. By id it is now its own case, and the
    error says so, because "your document moved, publish again" and "your
    document is gone" ask the caller for different things.

    It also pins that a stale URI is never stored: `document_id` and
    `resource_uri` describe the same document or the publication is not
    written.

    Pre-change this is RED for the same reason as the test above.
    """
    from app.services import publication_service
    from app.services.publication_service import create_publication_for_vault
    from app.services.uri_service import doc_uri

    monkeypatch.setattr(
        publication_service.settings, "public_base_url",
        "https://race.test.local", raising=False,
    )

    vid, vname = vault["id"], vault["name"]
    did_a, did_b = uuid.uuid4(), uuid.uuid4()
    path = "reports/q7.md"
    moved_to = "archive/q7.md"
    await _insert_doc(pool, doc_id=did_a, vault_id=vid, path=path, title="A")

    real_create = publication_service.create_publication
    handed_off: dict = {}

    async def _move_a_then_insert(**kwargs):
        handed_off.update(kwargs)
        async with pool.acquire() as c:
            await c.execute(
                "UPDATE documents SET path = $1 WHERE id = $2", moved_to, did_a,
            )
        await _insert_doc(pool, doc_id=did_b, vault_id=vid, path=path, title="B")
        return await real_create(**kwargs)

    monkeypatch.setattr(
        publication_service, "create_publication", _move_a_then_insert,
    )

    with pytest.raises(ValueError, match="moved concurrently"):
        await create_publication_for_vault(
            vault_name=vname, resource_type="document", doc_id=path,
            title="A (public)",
        )

    assert handed_off.get("document_id") == did_a, (
        "the resolve did not hand A's id to the INSERT "
        f"(got {handed_off.get('document_id')!r}); the window this test "
        "exists to force was not exercised."
    )
    # The URI that crossed the gap is A's ORIGINAL path — the stale one. That
    # is what makes this the move case and not the delete case: the id is
    # still live, and it is the id/URI disagreement that has to be caught.
    assert handed_off.get("resource_uri") == doc_uri(vname, path), (
        f"the resolve handed over {handed_off.get('resource_uri')!r}, not the "
        "pre-move URI; this is not the interleaving the test describes."
    )

    async with pool.acquire() as conn:
        a_now, b_now = await conn.fetchval(
            "SELECT path FROM documents WHERE id = $1", did_a,
        ), await conn.fetchval(
            "SELECT path FROM documents WHERE id = $1", did_b,
        )
        pubs = await conn.fetch(
            "SELECT slug, document_id, resource_uri FROM publications "
            " WHERE vault_id = $1",
            vid,
        )
    assert (a_now, b_now) == (moved_to, path), (
        f"setup did not hold: A is at {a_now!r} and B at {b_now!r}; the test "
        "needs A moved off the path and B sitting on it."
    )
    assert not pubs, (
        f"a publication was created anyway: {[dict(r) for r in pubs]}. "
        "Either it points at B — which nobody published — or it points at A "
        "under the path A no longer occupies."
    )


@pytest.mark.asyncio
async def test_publish_refuses_a_uri_that_names_another_vault(
    pool, vault, monkeypatch,
):
    """The same comparison, reached from the caller's side rather than a race.

    `create_publication` is the low-level function; it does no vault-access
    check of its own and no production caller reaches it directly. Verifying
    the stored URI against the one the LOCKED document row renders means it
    can no longer persist a row whose two vault identifiers disagree —
    resolution already declines to serve such a row, so the write is where
    it should have been refused.

    The document is real and in this vault; only the URI's vault component
    is wrong, which is the whole point: the id being valid is not enough.
    """
    from app.services import publication_service
    from app.services.publication_service import create_publication

    monkeypatch.setattr(
        publication_service.settings, "public_base_url",
        "https://race.test.local", raising=False,
    )

    vid = vault["id"]
    did = uuid.uuid4()
    path = "reports/q8.md"
    await _insert_doc(pool, doc_id=did, vault_id=vid, path=path, title="Q8")

    with pytest.raises(ValueError, match="vault does not match"):
        await create_publication(
            vault_id=vid, resource_type="document",
            resource_uri="akb://some-other-vault/coll/reports/doc/q8.md",
            document_id=did, title="Q8",
        )

    async with pool.acquire() as conn:
        pubs = await conn.fetchval(
            "SELECT COUNT(*) FROM publications WHERE vault_id = $1", vid,
        )
    assert pubs == 0, "a vault-mismatched publication must not be stored"
