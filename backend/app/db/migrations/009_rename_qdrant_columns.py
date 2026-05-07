"""Migration 009: rename `chunks.qdrant_*` columns and `qdrant_delete_outbox`
table to `vector_*`.

Purely cosmetic — completes the service-layer decoupling started in the
code refactor that renamed `qdrant_store.py` → `vector_store.py`. The
backing engine is still Qdrant; only the names stop leaking the driver
identity into the schema.

Idempotent: guarded by information_schema existence checks so running
the migration twice (or on a fresh DB where the target names already
exist) is a no-op.

Intended runbook: pause the backend (`kubectl scale … --replicas=0`)
before applying, because concurrent queries from the live worker would
fail mid-rename.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import get_pool, init_db, close_pool

logger = logging.getLogger("akb.migration.009")


_COLUMN_RENAMES = (
    ("qdrant_indexed_at",      "vector_indexed_at"),
    ("qdrant_next_attempt_at", "vector_next_attempt_at"),
    ("qdrant_retry_count",     "vector_retry_count"),
    ("qdrant_last_error",      "vector_last_error"),
)


async def _column_exists(conn, table: str, col: str) -> bool:
    return bool(await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_name=$1 AND column_name=$2
        )
        """,
        table, col,
    ))


async def _rename_column_if_old_exists(conn, table: str, old: str, new: str) -> None:
    old_exists = await _column_exists(conn, table, old)
    new_exists = await _column_exists(conn, table, new)
    if old_exists and not new_exists:
        await conn.execute(f'ALTER TABLE {table} RENAME COLUMN {old} TO {new}')
    elif old_exists and new_exists:
        # Earlier migration ran (005 pre-rename) and re-added the old column
        # while this migration had already renamed it. Drop the duplicate —
        # data is on the new column (renamed atomically). Happens at most
        # once per deploy while historical migrations catch up.
        await conn.execute(f'ALTER TABLE {table} DROP COLUMN {old}')


async def _rename_table_if_old_exists(conn, old: str, new: str) -> None:
    old_exists = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name=$1)",
        old,
    )
    new_exists = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name=$1)",
        new,
    )
    if old_exists and not new_exists:
        await conn.execute(f'ALTER TABLE {old} RENAME TO {new}')
    elif old_exists and new_exists:
        await conn.execute(f'DROP TABLE {old}')


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    async with conn.transaction():
        for old, new in _COLUMN_RENAMES:
            await _rename_column_if_old_exists(conn, "chunks", old, new)
        await _rename_table_if_old_exists(
            conn, "qdrant_delete_outbox", "vector_delete_outbox",
        )

    # Indexes — PG supports `ALTER INDEX IF EXISTS RENAME TO`, which is
    # naturally idempotent on both the old and new name.
    await conn.execute(
        "ALTER INDEX IF EXISTS idx_chunks_qdrant_pending "
        "RENAME TO idx_chunks_vector_pending"
    )
    await conn.execute(
        "ALTER INDEX IF EXISTS idx_qdrant_delete_pending "
        "RENAME TO idx_vector_delete_pending"
    )
    await conn.execute(
        "ALTER INDEX IF EXISTS idx_qdrant_delete_processed "
        "RENAME TO idx_vector_delete_processed"
    )

    logger.info("Migration 009 applied: qdrant_* columns/table/indexes renamed to vector_*")


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
