"""Migration 014: chunks.vault_id

Denormalize vault_id onto chunks so per-vault pending_stats can be a
single indexed COUNT instead of a polymorphic 3-branch JOIN.

The chunks table has source_type ∈ {document, table, file} and a
polymorphic source_id. Each parent table (documents / vault_tables /
vault_files) already carries vault_id, so we backfill from there.

Single transaction: ADD nullable column → backfill 3 parent types →
delete orphans → SET NOT NULL → FK + index. Concurrent INSERT during
the migration is safe because the column is nullable until step 4 and
the writer code (deployed before this migration) supplies vault_id.

Idempotent — running twice on already-migrated data is a no-op.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import get_pool

logger = logging.getLogger("akb.migrations.014")


async def migrate(conn=None):
    """Entry point invoked by app.db.postgres._apply_migrations.

    Mirrors 013_nfc_normalize: if the runner did not pass us a conn,
    acquire our own from the pool. Inside `_run`, all DDL + backfill
    runs in a single transaction.
    """
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    async with conn.transaction():
        await conn.execute(
            "ALTER TABLE chunks ADD COLUMN IF NOT EXISTS vault_id UUID"
        )
        await conn.execute(
            """
            UPDATE chunks c
               SET vault_id = d.vault_id
              FROM documents d
             WHERE c.source_type = 'document'
               AND c.source_id = d.id
               AND c.vault_id IS NULL
            """
        )
        await conn.execute(
            """
            UPDATE chunks c
               SET vault_id = t.vault_id
              FROM vault_tables t
             WHERE c.source_type = 'table'
               AND c.source_id = t.id
               AND c.vault_id IS NULL
            """
        )
        await conn.execute(
            """
            UPDATE chunks c
               SET vault_id = f.vault_id
              FROM vault_files f
             WHERE c.source_type = 'file'
               AND c.source_id = f.id
               AND c.vault_id IS NULL
            """
        )
        orphans = await conn.fetchval(
            "SELECT COUNT(*) FROM chunks WHERE vault_id IS NULL"
        )
        if orphans:
            logger.warning(
                "Deleting %d orphan chunks with no parent row", orphans
            )
            await conn.execute(
                "DELETE FROM chunks WHERE vault_id IS NULL"
            )
        await conn.execute(
            "ALTER TABLE chunks ALTER COLUMN vault_id SET NOT NULL"
        )
        # FK is wrapped in DO block because PG raises if the constraint
        # already exists; idempotent migration must tolerate re-runs.
        await conn.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'chunks_vault_id_fkey'
                ) THEN
                    ALTER TABLE chunks
                      ADD CONSTRAINT chunks_vault_id_fkey
                      FOREIGN KEY (vault_id)
                      REFERENCES vaults(id) ON DELETE CASCADE;
                END IF;
            END $$;
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_chunks_vault_id
                ON chunks (vault_id)
            """
        )
