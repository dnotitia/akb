"""Migration 107: row CAS token (`row_commit`) on dynamic vault tables.

T2-1 of the Table row-CAS wave (feeds ADR-0019 trigger (a)).

Vault data tables (`vt_<vault>__<table>`) get a bookkeeping
`row_commit TEXT NOT NULL DEFAULT <uuid>` column — the row-level
equivalent of a Document's `current_commit`. A BEFORE UPDATE trigger
(`akb_bump_row_commit()`) mints a fresh token on every UPDATE, so any
concurrent writer holding a stale token fails its
`expected_row_commit` match (409 at the API layer) instead of
silently winning a lost update.

Design notes (T0: option 1, owner-confirmed):

- Equality-only compare (never ordering): tokens are uuids, not
  versions. "Newer" has no meaning; only match/mismatch.
- `gen_random_uuid()` (pgcrypto, already installed — see init.sql:9),
  not `uuid_generate_v4()`, for the per-row default AND the trigger
  bump: one function, no new extension.
- New tables get the column + trigger at create time — see
  `table_data_repo.create_dynamic_table`. This migration backfills
  existing `vt_*` tables (ADD COLUMN with DEFAULT backfills all rows
  in one pass; trigger installed per table).
- Message Events / append-only paths untouched (no CAS needed).
- Idempotent: ADD COLUMN IF NOT EXISTS + CREATE OR REPLACE FUNCTION
  + per-table DROP TRIGGER IF EXISTS → CREATE TRIGGER.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db

logger = logging.getLogger("akb.migration.107")

_VT_NAME_RE = re.compile(r"^vt_[a-z0-9_]+$")


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    async with conn.transaction():
        await conn.execute(
            """
            CREATE OR REPLACE FUNCTION akb_bump_row_commit()
            RETURNS TRIGGER AS $$
            BEGIN
                NEW.row_commit = gen_random_uuid()::TEXT;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )

        rows = await conn.fetch(
            """
            SELECT t.table_name
              FROM information_schema.tables t
             WHERE t.table_schema = 'public'
               AND t.table_name ~ '^vt_'
               AND NOT EXISTS (
                   SELECT 1 FROM information_schema.columns c
                    WHERE c.table_schema = 'public'
                      AND c.table_name = t.table_name
                      AND c.column_name = 'row_commit'
               )
             ORDER BY t.table_name
            """
        )

        added = 0
        skipped: list[str] = []
        for r in rows:
            tbl = r["table_name"]
            if not _VT_NAME_RE.fullmatch(tbl):
                skipped.append(tbl)
                continue
            await conn.execute(
                f"ALTER TABLE {tbl} ADD COLUMN row_commit TEXT NOT NULL "
                f"DEFAULT gen_random_uuid()::TEXT"
            )
            await conn.execute(
                f"DROP TRIGGER IF EXISTS akb_bump_row_commit_trigger ON {tbl}"
            )
            await conn.execute(
                f"CREATE TRIGGER akb_bump_row_commit_trigger "
                f"BEFORE UPDATE ON {tbl} "
                f"FOR EACH ROW EXECUTE FUNCTION akb_bump_row_commit()"
            )
            added += 1

        # Tables that already have row_commit (re-run / partial state):
        # ensure the trigger exists.
        rows2 = await conn.fetch(
            """
            SELECT t.table_name
              FROM information_schema.tables t
             WHERE t.table_schema = 'public'
               AND t.table_name ~ '^vt_'
               AND EXISTS (
                   SELECT 1 FROM information_schema.columns c
                    WHERE c.table_schema = 'public'
                      AND c.table_name = t.table_name
                      AND c.column_name = 'row_commit'
               )
             ORDER BY t.table_name
            """
        )
        retriggered = 0
        for r in rows2:
            tbl = r["table_name"]
            if not _VT_NAME_RE.fullmatch(tbl):
                continue
            await conn.execute(
                f"DROP TRIGGER IF EXISTS akb_bump_row_commit_trigger ON {tbl}"
            )
            await conn.execute(
                f"CREATE TRIGGER akb_bump_row_commit_trigger "
                f"BEFORE UPDATE ON {tbl} "
                f"FOR EACH ROW EXECUTE FUNCTION akb_bump_row_commit()"
            )
            retriggered += 1

    logger.info(
        "Migration 107 applied: row_commit column + bump trigger on "
        "%d new vt_* table(s), trigger ensured on %d existing%s",
        added,
        retriggered,
        f" (skipped {len(skipped)} non-conforming: {skipped})" if skipped else "",
    )


async def main() -> None:
    await init_db()
    try:
        await migrate()
    finally:
        await close_pool()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
