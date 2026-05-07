"""Migration 013: NFC-normalize every user-controlled text column.

macOS HFS+/APFS reports Hangul filenames in NFD (decomposed jamo). When
a legacy uploader read filenames and document text from a Mac and
forwarded them to the backend, that NFD leaked into PG. BM25 tokenizers
and the embedding model treat NFD and NFC as different tokens, so
queries typed in normal NFC never match the indexed NFD content and
documents become invisible to search.

This migration walks every table whose text columns can carry
user-controlled strings and rewrites them to NFC in place. Rows where
the column is already NFC are untouched (the WHERE filter keeps the
update set small and avoids churning every row's xmin).

Chunk embeddings must be recomputed when content changes — NFC-flipping
a chunk's content changes the tokens the embedding was trained on, so
the old vector is stale. We null the embedding + vector_indexed_at on
any chunk whose content was rewritten; `embed_worker` and
`vector_indexer` pick them up on their next sweep.

Idempotent — running twice on fully-NFC data is a no-op.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import get_pool, init_db, close_pool

logger = logging.getLogger("akb.migration.013")


# Tables / columns we know can carry user-supplied Korean. Order matters
# only for readability — each UPDATE is independent.
_TEXT_UPDATES: list[tuple[str, str]] = [
    ("vaults",       "name"),
    ("vaults",       "description"),
    ("collections",  "path"),
    ("collections",  "name"),
    ("collections",  "summary"),
    ("documents",    "path"),
    ("documents",    "title"),
    ("documents",    "summary"),
    ("documents",    "domain"),
    ("documents",    "external_path"),
    ("chunks",       "section_path"),
]


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    total_changed = 0

    # 1. Plain text columns ------------------------------------
    for table, col in _TEXT_UPDATES:
        result = await conn.execute(
            f"""
            UPDATE {table}
               SET {col} = normalize({col}, NFC)
             WHERE {col} IS NOT NULL
               AND {col} <> normalize({col}, NFC)
            """
        )
        # asyncpg returns e.g. "UPDATE 123"
        n = int(result.split()[-1]) if result else 0
        total_changed += n
        if n:
            logger.info("NFC %s.%s: %d rows normalized", table, col, n)

    # 2. Text arrays (tags) ------------------------------------
    # Normalize each element, drop rows that were already NFC.
    result = await conn.execute(
        """
        UPDATE documents
           SET tags = (
                 SELECT array_agg(normalize(t, NFC))
                   FROM unnest(tags) AS t
               )
         WHERE tags IS NOT NULL
           AND EXISTS (
                 SELECT 1 FROM unnest(tags) AS t
                  WHERE t <> normalize(t, NFC)
           )
        """
    )
    n = int(result.split()[-1]) if result else 0
    total_changed += n
    if n:
        logger.info("NFC documents.tags: %d rows normalized", n)

    # 3. JSONB metadata ---------------------------------------
    # Cast → text → normalize → jsonb. Works because jsonb's text form
    # is canonical (no NFD surprise in key ordering); normalize only
    # touches character data, not structure.
    for table, col in [("documents", "metadata")]:
        result = await conn.execute(
            f"""
            UPDATE {table}
               SET {col} = normalize({col}::text, NFC)::jsonb
             WHERE {col} IS NOT NULL
               AND {col}::text <> normalize({col}::text, NFC)
            """
        )
        n = int(result.split()[-1]) if result else 0
        total_changed += n
        if n:
            logger.info("NFC %s.%s: %d rows normalized", table, col, n)

    # 4. Chunks: content is the big one. Any rewrite invalidates the
    #    embedding, so we clear vector_indexed_at to force re-index.
    #    Pre-Phase-4 schemas also had a chunks.embedding column to null;
    #    post-016 it's gone, so we branch on column existence.
    has_embedding = await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_schema = 'public'
               AND table_name = 'chunks'
               AND column_name = 'embedding'
        )
        """
    )
    if has_embedding:
        update_sql = """
            UPDATE chunks
               SET content           = normalize(content, NFC),
                   embedding         = NULL,
                   vector_indexed_at = NULL
             WHERE content IS NOT NULL
               AND content <> normalize(content, NFC)
        """
    else:
        update_sql = """
            UPDATE chunks
               SET content           = normalize(content, NFC),
                   vector_indexed_at = NULL
             WHERE content IS NOT NULL
               AND content <> normalize(content, NFC)
        """
    result = await conn.execute(update_sql)
    n = int(result.split()[-1]) if result else 0
    total_changed += n
    if n:
        logger.info(
            "NFC chunks.content: %d rows normalized + indexing flags cleared",
            n,
        )

    logger.info("Migration 013 applied: %d total rows normalized", total_changed)


if __name__ == "__main__":
    async def main():
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
        await init_db()
        try:
            await migrate()
        finally:
            await close_pool()

    asyncio.run(main())
