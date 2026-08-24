"""Track BM25 source-corpus mutations without rescanning ``chunks``.

The old refresher compared ``COUNT(chunks)`` with ``bm25_stats.total_docs``.
Those values describe different populations: the former includes every source
chunk, while the latter includes only chunks that retain at least one token
after Kiwi/tag/stop-word filtering.  A stable corpus can therefore look dirty
forever and trigger a full retokenization on every cadence.

``bm25_corpus_revision_seq`` is a cheap, monotonic invalidation clock.  Row
triggers advance it only for changes that can affect BM25 (insert, delete, or
content update); indexing-state updates do not touch it.  Sequences are used
instead of a singleton counter row so concurrent chunk writers do not serialize
on one hot tuple.  Gaps after rolled-back writes are harmless: at worst they
cause a conservative extra refresh.
"""

from __future__ import annotations

import logging


logger = logging.getLogger("akb.migration.084")


async def migrate(conn) -> None:
    sequence_existed = bool(
        await conn.fetchval(
            "SELECT to_regclass('public.bm25_corpus_revision_seq') IS NOT NULL"
        )
    )

    async with conn.transaction():
        await conn.execute(
            """
            CREATE SEQUENCE IF NOT EXISTS bm25_corpus_revision_seq AS BIGINT;

            ALTER TABLE bm25_stats
                ADD COLUMN IF NOT EXISTS source_revision BIGINT NOT NULL DEFAULT 0,
                ADD COLUMN IF NOT EXISTS source_chunk_count BIGINT NOT NULL DEFAULT 0;

            CREATE OR REPLACE FUNCTION akb_bump_bm25_corpus_revision()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            SET search_path = pg_catalog, public
            AS $$
            BEGIN
                PERFORM pg_catalog.nextval(
                    'public.bm25_corpus_revision_seq'::regclass
                );
                -- AFTER-trigger return values are ignored.  Returning NULL
                -- also works for TRUNCATE, where neither row record exists.
                RETURN NULL;
            END;
            $$;

            DROP TRIGGER IF EXISTS trg_chunks_bm25_revision_insert_delete ON chunks;
            CREATE TRIGGER trg_chunks_bm25_revision_insert_delete
            AFTER INSERT OR DELETE ON chunks
            FOR EACH ROW
            EXECUTE FUNCTION akb_bump_bm25_corpus_revision();

            DROP TRIGGER IF EXISTS trg_chunks_bm25_revision_content_update ON chunks;
            CREATE TRIGGER trg_chunks_bm25_revision_content_update
            AFTER UPDATE OF content ON chunks
            FOR EACH ROW
            WHEN (OLD.content IS DISTINCT FROM NEW.content)
            EXECUTE FUNCTION akb_bump_bm25_corpus_revision();

            DROP TRIGGER IF EXISTS trg_chunks_bm25_revision_truncate ON chunks;
            CREATE TRIGGER trg_chunks_bm25_revision_truncate
            AFTER TRUNCATE ON chunks
            FOR EACH STATEMENT
            EXECUTE FUNCTION akb_bump_bm25_corpus_revision();
            """
        )

        # Establish a no-scan baseline only when introducing the sequence.
        # Existing non-zero tokenizer metadata proves that this database has
        # completed at least one recompute already; trust that snapshot instead
        # of forcing a production-sized corpus through Kiwi during rollout.
        if not sequence_existed:
            chunk_count = int(
                await conn.fetchval("SELECT COUNT(*) FROM chunks") or 0
            )
            if chunk_count:
                await conn.fetchval(
                    "SELECT setval('bm25_corpus_revision_seq', $1, true)",
                    chunk_count,
                )
                revision = chunk_count
            else:
                await conn.fetchval(
                    "SELECT setval('bm25_corpus_revision_seq', 1, false)"
                )
                revision = 0

            await conn.execute(
                """
                UPDATE bm25_stats
                   SET source_revision = $1,
                       source_chunk_count = $2
                 WHERE tokenizer_version <> '0'
                """,
                revision,
                chunk_count,
            )

    logger.info("Migration 084 added BM25 corpus revision tracking")
