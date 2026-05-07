"""One-shot reindex of every document / table / file into the generalized
chunks pipeline.

Preserves existing data: PG canonical rows are never touched. Per source
we drop its current chunks (PG + outbox) and rebuild from canonical
content + metadata header. Qdrant points get cleaned up async by
vector_indexer.

Batches embedding calls: documents embed their own chunks per-call
already; tables and files are batched `BATCH_SIZE` at a time to cut
round-trip overhead on the small-metadata-chunk path.

Resumable: --since-ts limits documents to rows updated after a given
timestamp so a partial run can continue.

Usage:
    python scripts/reindex_all.py [--docs] [--tables] [--files] [--all]
                                  [--since YYYY-MM-DDTHH:MM:SS]
                                  [--limit N]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db.postgres import get_pool, init_db, close_pool
from app.services.git_service import GitService
from app.services.index_service import (
    build_doc_metadata_header,
    build_file_chunk,
    build_table_chunk,
    chunk_markdown,
    generate_embeddings,
    write_source_chunks,
)

logger = logging.getLogger("akb.reindex")

BATCH_SIZE = 32   # embedding API batch size for tables/files


def _strip_frontmatter(raw: str) -> str:
    """Strip YAML frontmatter if present — mirror document_service.put."""
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end > 0:
            return raw[end + 4:].lstrip()
    return raw


async def reindex_document(conn, git: GitService, row) -> int:
    raw = await asyncio.to_thread(git.read_file, row["vault_name"], row["path"])
    if not raw:
        logger.warning("skip %s/%s — git file missing", row["vault_name"], row["path"])
        return 0
    body = _strip_frontmatter(raw)

    meta_header = build_doc_metadata_header(
        vault_name=row["vault_name"], path=row["path"], title=row["title"],
        summary=row["summary"],
        tags=list(row["tags"]) if row["tags"] else [],
        doc_type=row["doc_type"],
    )
    chunks = chunk_markdown(body, metadata_header=meta_header)
    if not chunks:
        return 0
    embeddings = await generate_embeddings([c.content for c in chunks])
    return await write_source_chunks(
        conn, "document", str(row["id"]),
        vault_id=row["vault_id"],
        chunks=chunks, embeddings=embeddings,
    )


def _columns_of(row) -> list[dict]:
    raw = row["columns"]
    if isinstance(raw, list):
        return raw
    return json.loads(raw or "[]")


async def reindex_tables_batched(conn, rows) -> int:
    """Embed + write tables in batches to amortize HTTP overhead."""
    done = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        chunks = [
            build_table_chunk(
                vault_name=r["vault_name"], name=r["name"],
                description=r["description"], columns=_columns_of(r),
            )
            for r in batch
        ]
        embs = await generate_embeddings([c.content for c in chunks])
        for r, chunk, emb in zip(batch, chunks, embs):
            await write_source_chunks(
                conn, "table", str(r["id"]),
                vault_id=r["vault_id"],
                chunks=[chunk], embeddings=[emb],
            )
            done += 1
    return done


async def reindex_files_batched(conn, rows) -> int:
    done = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        chunks = [
            build_file_chunk(
                vault_name=r["vault_name"], collection=r["collection"] or "",
                name=r["name"], mime_type=r["mime_type"],
                size_bytes=r["size_bytes"], description=r["description"],
            )
            for r in batch
        ]
        embs = await generate_embeddings([c.content for c in chunks])
        for r, chunk, emb in zip(batch, chunks, embs):
            await write_source_chunks(
                conn, "file", str(r["id"]),
                vault_id=r["vault_id"],
                chunks=[chunk], embeddings=[emb],
            )
            done += 1
    return done


async def _iter_docs(conn, since: str | None, limit: int | None):
    q = """
        SELECT d.id, d.path, d.title, d.summary, d.tags, d.doc_type,
               v.name AS vault_name, v.id AS vault_id
          FROM documents d
          JOIN vaults v ON d.vault_id = v.id
    """
    params: list = []
    if since:
        q += " WHERE d.updated_at >= $1"
        params.append(since)
    q += " ORDER BY d.updated_at DESC"
    if limit:
        q += f" LIMIT {int(limit)}"
    return await conn.fetch(q, *params)


async def _iter_tables(conn, limit: int | None):
    q = """
        SELECT t.id, t.name, t.description, t.columns,
               v.name AS vault_name, v.id AS vault_id
          FROM vault_tables t
          JOIN vaults v ON t.vault_id = v.id
    """
    if limit:
        q += f" LIMIT {int(limit)}"
    return await conn.fetch(q)


async def _iter_files(conn, limit: int | None):
    q = """
        SELECT f.id, f.collection, f.name, f.mime_type, f.size_bytes,
               f.description, v.name AS vault_name, v.id AS vault_id
          FROM vault_files f
          JOIN vaults v ON f.vault_id = v.id
    """
    if limit:
        q += f" LIMIT {int(limit)}"
    return await conn.fetch(q)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", action="store_true")
    ap.add_argument("--tables", action="store_true")
    ap.add_argument("--files", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--since", default=None,
                    help="ISO timestamp; only docs updated after this will be reindexed")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    do_docs = args.all or args.docs
    do_tables = args.all or args.tables
    do_files = args.all or args.files
    if not any([do_docs, do_tables, do_files]):
        print("Select at least one of --docs / --tables / --files, or --all")
        return

    await init_db()
    git = GitService()
    pool = await get_pool()

    try:
        if do_docs:
            async with pool.acquire() as conn:
                rows = await _iter_docs(conn, args.since, args.limit)
            print(f"Reindexing {len(rows)} documents…")
            t0 = time.time()
            ok = err = total_chunks = 0
            # One long-lived connection for the loop — per-row acquire
            # added ~8000 acquire/release cycles otherwise. Failures on a
            # single row don't poison the connection (each write is its
            # own implicit tx in asyncpg).
            async with pool.acquire() as conn:
                for i, r in enumerate(rows, start=1):
                    try:
                        n = await reindex_document(conn, git, r)
                        total_chunks += n
                        ok += 1
                    except Exception as e:
                        err += 1
                        logger.warning("doc %s/%s failed: %s",
                                       r["vault_name"], r["path"], e)
                    if i % 50 == 0 or i == len(rows):
                        dt = time.time() - t0
                        rate = i / max(dt, 0.01)
                        print(f"  [{i}/{len(rows)}] ok={ok} err={err} "
                              f"chunks={total_chunks} rate={rate:.1f}/s")

        if do_tables:
            async with pool.acquire() as conn:
                rows = await _iter_tables(conn, args.limit)
                print(f"Reindexing {len(rows)} tables…")
                done = await reindex_tables_batched(conn, rows)
                print(f"  tables done: {done}")

        if do_files:
            async with pool.acquire() as conn:
                rows = await _iter_files(conn, args.limit)
                print(f"Reindexing {len(rows)} files…")
                done = await reindex_files_batched(conn, rows)
                print(f"  files done: {done}")
    finally:
        await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main())
