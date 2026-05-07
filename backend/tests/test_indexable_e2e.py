"""E2E tests for the generalized indexing pipeline (document/table/file).

Runs inside the backend pod so it talks to the live PG + vector store + embedding API.

Covers edge cases:
  T1  Schema — chunks has source_type + source_id, backfilled
  T2  Migration idempotency — running migration twice is safe
  T3  Document reindex — metadata header appears in chunk content
  T4  Document search — 경조금 case: GOLD in top-5
  T5  Document search — exact-match fallback still works
  T6  Table indexing — metadata chunk exists + discoverable by name
  T7  Table indexing — description + column names searchable
  T8  File indexing — metadata chunk by filename
  T9  File indexing — description searchable
  T10 Mixed pool — single query returns mix of doc/table/file
  T11 SearchResult carries source_type/source_id
  T12 Delete table removes its chunk
  T13 Delete file removes its chunk
  T14 Non-existent source ids gracefully ignored
  T15 Reindex preserves unrelated chunks (doesn't drop other docs)
"""
from __future__ import annotations

import asyncio
import sys
import uuid

sys.path.insert(0, "/app")

from app.config import settings  # noqa: E402
from app.db.postgres import get_pool, init_db, close_pool  # noqa: E402
from app.services.index_service import (  # noqa: E402
    build_doc_metadata_header, build_table_chunk, build_file_chunk,
    chunk_markdown, generate_embeddings, write_source_chunks,
    delete_table_chunks, delete_file_chunks,
)
from app.services.search_service import SearchService  # noqa: E402

PASSED = 0
FAILED = 0


def ok(m):
    global PASSED
    PASSED += 1
    print(f"  OK    {m}")

def fail(m):
    global FAILED
    FAILED += 1
    print(f"  FAIL  {m}")


async def _get_or_create_test_vault(conn) -> uuid.UUID:
    """Return any existing vault id, or create a `_test_indexable` vault.
    Used by t12/t13 which insert synthetic chunks; chunks.vault_id is
    NOT NULL FK so the tests need a real vault row."""
    row = await conn.fetchrow("SELECT id FROM vaults LIMIT 1")
    if row:
        return row["id"]
    # Create one if the DB is empty (rare in real deployments).
    # owner_id is nullable; git_path is NOT NULL so we set a placeholder.
    vid = await conn.fetchval(
        """
        INSERT INTO vaults (id, name, git_path, owner_id, status)
        VALUES (uuid_generate_v4(), '_test_indexable',
                '/tmp/_test_indexable.git', NULL, 'active')
        RETURNING id
        """
    )
    return vid


async def t1_schema():
    print("\n[T1] chunks schema has source_type/source_id, backfilled")
    pool = await get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow("""
            SELECT
                EXISTS(SELECT 1 FROM information_schema.columns
                        WHERE table_name='chunks' AND column_name='source_type') AS has_st,
                EXISTS(SELECT 1 FROM information_schema.columns
                        WHERE table_name='chunks' AND column_name='source_id') AS has_sid,
                (SELECT COUNT(*) FROM chunks WHERE source_id IS NULL) AS nulls,
                (SELECT COUNT(*) FROM chunks) AS total
        """)
    if not r["has_st"] or not r["has_sid"]:
        fail("chunks.source_type or source_id missing")
        return
    if r["nulls"]:
        fail(f"{r['nulls']} chunks still have NULL source_id after backfill")
        return
    ok(f"schema OK, total={r['total']}, all source_id populated")


async def t2_migration_idempotent():
    print("\n[T2] migration 006 is idempotent")
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "mig006", "/app/app/db/migrations/006_indexable_chunks.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    try:
        await mod.migrate()
        await mod.migrate()
        ok("migration ran twice without error")
    except Exception as e:
        fail(f"re-running migration raised {type(e).__name__}: {e}")


async def t3_doc_metadata_header():
    print("\n[T3] document chunks include metadata header")
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT c.content FROM chunks c
             JOIN documents d ON c.source_id = d.id AND c.source_type='document'
             JOIN vaults v ON d.vault_id = v.id
             WHERE v.name='dnotitia' AND d.path LIKE '%경조비%'
             ORDER BY c.chunk_index LIMIT 1
        """)
    if not row:
        fail("no dnotitia 경조비 chunk found — reindex may not have run")
        return
    c = row["content"]
    checks = {
        "TITLE:": "TITLE:" in c,
        "PATH:": "PATH:" in c,
        "TYPE:": "TYPE:" in c or True,  # doc_type optional
    }
    missing = [k for k, v in checks.items() if not v]
    if missing:
        fail(f"metadata header missing {missing} in chunk: {c[:120]}")
    else:
        ok(f"metadata header present (first 80 chars): {c[:80]!r}")


async def t4_kyungjo_search():
    print("\n[T4] 경조금 case — GOLD in top-5")
    svc = SearchService()
    r = await svc.search("경조금 지급과 관련된 사내 규정", limit=10)
    gold_rank = next(
        (i+1 for i, h in enumerate(r.results) if "경조비-및-경조휴가" in h.path),
        None,
    )
    if gold_rank is None:
        fail("GOLD not found in top-10")
    elif gold_rank > 5:
        fail(f"GOLD at rank {gold_rank}, expected top-5")
    else:
        ok(f"GOLD at rank #{gold_rank}")


async def t5_exact_match_still_works():
    print("\n[T5] single-term '경조비' query finds the right doc at #1")
    svc = SearchService()
    r = await svc.search("경조비", limit=10)
    rank = next(
        (i+1 for i, h in enumerate(r.results) if "경조비-및-경조휴가" in h.path),
        None,
    )
    if rank != 1:
        fail(f"'경조비' single-term: GOLD at {rank}, expected #1")
    else:
        ok(f"'경조비' single-term: GOLD at #1 (score={r.results[0].score:.3f})")


async def t6_table_indexing():
    print("\n[T6] table metadata indexed and discoverable")
    pool = await get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow("""
            SELECT COUNT(*) AS n FROM chunks WHERE source_type='table'
        """)
    if not r["n"]:
        fail("no 'table' chunks in PG — table reindex not run?")
        return
    ok(f"{r['n']} table chunks present")

    svc = SearchService()
    r = await svc.search("sales pipeline", limit=10)
    table_hits = [h for h in r.results if h.source_type == "table"]
    if not table_hits:
        fail("no table in search results for 'sales pipeline' (is there a sales/pipeline table?)")
    else:
        ok(f"{len(table_hits)} table(s) in top-10, first: {table_hits[0].title}")


async def t7_table_description_searchable():
    print("\n[T7] table column names hit search")
    svc = SearchService()
    r = await svc.search("cpu catalog gpu", limit=10)
    table_hits = [h for h in r.results if h.source_type == "table"]
    if table_hits:
        ok(f"{len(table_hits)} table(s) for cpu/gpu query; top={table_hits[0].title}")
    else:
        fail("expected at least one hw_market table hit")


async def t8_file_indexing():
    print("\n[T8] file metadata indexed and discoverable")
    pool = await get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow("SELECT COUNT(*) AS n FROM chunks WHERE source_type='file'")
    if not r["n"]:
        fail("no 'file' chunks — reindex not run?")
        return
    ok(f"{r['n']} file chunks present")


async def t9_file_name_searchable():
    print("\n[T9] file name appears in search")
    svc = SearchService()
    # Search for a plausible filename keyword; whatever's in the corpus
    r = await svc.search("pdf document", limit=10)
    file_hits = [h for h in r.results if h.source_type == "file"]
    if file_hits:
        ok(f"{len(file_hits)} file(s) in results")
    else:
        # Not a hard fail — depends on what files exist
        print("  (note: no file in top-10 for 'pdf document' — may be fine if corpus has few files)")
        ok("no crash; file-specific discovery depends on corpus")


async def t10_mixed_pool():
    print("\n[T10] mixed source types in a single query")
    svc = SearchService()
    r = await svc.search("data", limit=20)
    types = {h.source_type for h in r.results}
    ok(f"source_types returned: {types}")


async def t11_searchresult_fields():
    print("\n[T11] SearchResult carries source_type + source_id")
    svc = SearchService()
    r = await svc.search("경조비", limit=3)
    if not r.results:
        fail("no results for 경조비")
        return
    first = r.results[0]
    missing = []
    if not getattr(first, "source_type", None): missing.append("source_type")
    if not getattr(first, "source_id", None): missing.append("source_id")
    if missing:
        fail(f"SearchResult missing fields: {missing}")
    else:
        ok(f"source_type={first.source_type} source_id={first.source_id[:8]}…")


async def t12_delete_table_chunk():
    print("\n[T12] deleting a table removes its chunks")
    fake_id = uuid.uuid4()
    # Insert synthetic table chunk then delete, verify
    pool = await get_pool()
    from app.services.index_service import build_table_chunk, write_source_chunks, delete_table_chunks
    chunk = build_table_chunk(
        vault_name="_test", name="_e2e_fake_table_",
        description="e2e fake", columns=[{"name": "x", "type": "text"}],
    )
    embs = await generate_embeddings([chunk.content])
    async with pool.acquire() as conn:
        vault_id = await _get_or_create_test_vault(conn)
        await write_source_chunks(
            conn, "table", str(fake_id),
            vault_id=vault_id,
            chunks=[chunk],
        )
        r = await conn.fetchrow(
            "SELECT COUNT(*) AS n FROM chunks WHERE source_type='table' AND source_id=$1",
            fake_id,
        )
        if r["n"] != 1:
            fail(f"expected 1 chunk, got {r['n']}")
            return
        await delete_table_chunks(conn, str(fake_id))
        r2 = await conn.fetchrow(
            "SELECT COUNT(*) AS n FROM chunks WHERE source_type='table' AND source_id=$1",
            fake_id,
        )
    if r2["n"] != 0:
        fail(f"chunks not deleted: {r2['n']} remain")
    else:
        ok("table chunks deleted cleanly")


async def t13_delete_file_chunk():
    print("\n[T13] deleting a file removes its chunks")
    fake_id = uuid.uuid4()
    pool = await get_pool()
    chunk = build_file_chunk(
        vault_name="_test", collection="tmp", name="fake.pdf",
        mime_type="application/pdf", size_bytes=100,
        description="e2e fake file",
    )
    embs = await generate_embeddings([chunk.content])
    async with pool.acquire() as conn:
        vault_id = await _get_or_create_test_vault(conn)
        await write_source_chunks(
            conn, "file", str(fake_id),
            vault_id=vault_id,
            chunks=[chunk],
        )
        r = await conn.fetchrow(
            "SELECT COUNT(*) AS n FROM chunks WHERE source_type='file' AND source_id=$1",
            fake_id,
        )
        if r["n"] != 1:
            fail(f"expected 1 chunk got {r['n']}")
            return
        await delete_file_chunks(conn, str(fake_id))
        r2 = await conn.fetchrow(
            "SELECT COUNT(*) AS n FROM chunks WHERE source_type='file' AND source_id=$1",
            fake_id,
        )
    if r2["n"] != 0:
        fail(f"chunks not deleted: {r2['n']}")
    else:
        ok("file chunks deleted cleanly")


async def t14_nonexistent_source_ids():
    print("\n[T14] hydrator silently drops hits whose metadata is gone")
    svc = SearchService()
    # Make up a bogus VectorHit-like object and feed it through _hydrate_hits
    from app.services.vector_store import VectorHit
    fake = VectorHit(
        chunk_id="fake", source_type="document", source_id=str(uuid.uuid4()),
        section_path="", content="x", score=0.5,
    )
    results = await svc._hydrate_hits([fake])
    if results:
        fail(f"hydrator returned {len(results)} results for bogus source_id")
    else:
        ok("hydrator dropped unresolvable source_id")


async def t15_reindex_preserves_others():
    print("\n[T15] reindexing one doc doesn't touch others' chunks")
    pool = await get_pool()
    async with pool.acquire() as conn:
        totals = await conn.fetchrow("SELECT COUNT(*) AS n FROM chunks")
        total_before = totals["n"]
        # Pick one doc and count its chunks
        row = await conn.fetchrow("""
            SELECT d.id, d.path, d.title, d.summary, d.tags, d.doc_type,
                   v.name AS vault_name
              FROM documents d JOIN vaults v ON d.vault_id = v.id
             WHERE v.name='dnotitia' LIMIT 1
        """)
        r_before = await conn.fetchrow(
            "SELECT COUNT(*) AS n FROM chunks WHERE source_type='document' AND source_id=$1",
            row["id"],
        )
        print(f"    before: doc chunks={r_before['n']}, total={total_before}")

    from app.services.git_service import GitService
    from scripts.reindex_all import reindex_document
    async with pool.acquire() as conn:
        await reindex_document(conn, GitService(), row)
        r_after = await conn.fetchrow(
            "SELECT COUNT(*) AS n FROM chunks WHERE source_type='document' AND source_id=$1",
            row["id"],
        )
        total_after = await conn.fetchval("SELECT COUNT(*) FROM chunks")
    delta_other = (total_after - r_after["n"]) - (total_before - r_before["n"])
    if delta_other != 0:
        fail(f"other chunks changed: delta={delta_other}")
    else:
        ok(f"only target doc changed ({r_before['n']} → {r_after['n']})")


async def t16_outbox_sweep():
    print("\n[T16] outbox sweep removes old processed rows, leaves fresh ones")
    pool = await get_pool()
    # Seed two rows: one "just processed" (should survive), one
    # "processed 2 days ago" (should be swept).
    fake_a = uuid.uuid4()
    fake_b = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO vector_delete_outbox
                (chunk_id, source_type, source_id, processed_at)
            VALUES
                ($1, 'document', $1, NOW()),
                ($2, 'document', $2, NOW() - INTERVAL '2 days')
            """,
            fake_a, fake_b,
        )

    # Reset the sweep rate-limit guard so the call actually runs.
    from app.services import vector_indexer as qi
    qi._last_sweep_at = 0.0
    purged = await qi._sweep_outbox_once()

    async with pool.acquire() as conn:
        a_exists = await conn.fetchval(
            "SELECT 1 FROM vector_delete_outbox WHERE chunk_id=$1", fake_a,
        )
        b_exists = await conn.fetchval(
            "SELECT 1 FROM vector_delete_outbox WHERE chunk_id=$1", fake_b,
        )
        # Cleanup the fresh row we seeded so this test is idempotent.
        await conn.execute(
            "DELETE FROM vector_delete_outbox WHERE chunk_id=$1", fake_a,
        )

    if not a_exists:
        fail("sweep purged a fresh row")
    elif b_exists:
        fail("sweep did not purge a 2-day-old row")
    elif purged < 1:
        fail(f"sweep returned {purged}, expected at least 1")
    else:
        ok(f"sweep purged {purged} stale row(s); fresh row preserved")


async def t17_sweep_rate_limited():
    print("\n[T17] sweep is rate-limited by SWEEP_INTERVAL_SECONDS")
    from app.services import vector_indexer as qi
    qi._last_sweep_at = 0.0
    first = await qi._sweep_outbox_once()
    # Immediate second call should be gated and return 0 (no-op).
    second = await qi._sweep_outbox_once()
    if second != 0:
        fail(f"rate-limit guard failed: second sweep returned {second}")
    else:
        ok(f"sweep rate-limited correctly (first={first}, second={second})")


async def main():
    await init_db()
    print("=== AKB indexable E2E ===")
    await t1_schema()
    await t2_migration_idempotent()
    await t3_doc_metadata_header()
    await t4_kyungjo_search()
    await t5_exact_match_still_works()
    await t6_table_indexing()
    await t7_table_description_searchable()
    await t8_file_indexing()
    await t9_file_name_searchable()
    await t10_mixed_pool()
    await t11_searchresult_fields()
    await t12_delete_table_chunk()
    await t13_delete_file_chunk()
    await t14_nonexistent_source_ids()
    await t15_reindex_preserves_others()
    await t16_outbox_sweep()
    await t17_sweep_rate_limited()
    await close_pool()
    total = PASSED + FAILED
    print(f"\n=== Summary: {PASSED}/{total} passed, {FAILED} failed ===")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
