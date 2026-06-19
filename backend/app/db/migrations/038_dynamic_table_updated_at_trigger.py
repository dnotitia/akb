"""Migration 038: auto-bump `updated_at` on dynamic vault tables.

Vault data tables (`vt_<vault>__<table>`, created by
`table_data_repo.create_dynamic_table`) carry a bookkeeping
`updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()` column. `DEFAULT NOW()`
only fires on INSERT — PostgreSQL has no MySQL-style
`ON UPDATE CURRENT_TIMESTAMP` — so without a trigger every UPDATE left
`updated_at` frozen at insert time, making it an unreliable duplicate of
`created_at`. User SQL flows through `execute_sql` verbatim (table-name
rewrite only), so the app layer can't inject `SET updated_at = NOW()`
either; the only robust place to enforce the invariant is the database.

This migration:
  1. Creates one shared trigger function `akb_set_updated_at()` that sets
     `NEW.updated_at = NOW()`. It is SECURITY INVOKER (the default) — the
     assignment needs no privilege beyond `NOW()`, so it runs unchanged
     under the per-user `akb_user_<uid>` role that `akb_sql` switches into.
  2. Backfills a `BEFORE UPDATE` trigger onto every existing `vt_*` table
     that has an `updated_at` column. New tables get the trigger at create
     time — see `table_data_repo.create_dynamic_table`.

Idempotent: `CREATE OR REPLACE FUNCTION` + per-table
`DROP TRIGGER IF EXISTS` → `CREATE TRIGGER`, so re-running is a no-op and
self-heals a partially-applied state (e.g. some tables triggered, others
not).
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db

logger = logging.getLogger("akb.migration.038")

# Defense-in-depth: only interpolate identifiers matching the `vt_*` shape
# produced by `pg_table_name` (`_sanitize_pg_part` maps every
# non-alphanumeric to `_`, so a conforming name is pure `[a-z0-9_]`). The
# information_schema query already filters to the prefix, but the name is
# formatted into DDL, so we re-validate before trusting it.
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
            CREATE OR REPLACE FUNCTION akb_set_updated_at()
            RETURNS TRIGGER AS $$
            BEGIN
                NEW.updated_at = NOW();
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )

        # `~ '^vt_'` (regex) not `LIKE 'vt\_%'`: `_` is a literal in a
        # regex but a wildcard in LIKE, so the regex form sidesteps the
        # backslash-escaping footgun entirely.
        rows = await conn.fetch(
            """
            SELECT t.table_name
              FROM information_schema.tables t
             WHERE t.table_schema = 'public'
               AND t.table_name ~ '^vt_'
               AND EXISTS (
                   SELECT 1 FROM information_schema.columns c
                    WHERE c.table_schema = 'public'
                      AND c.table_name = t.table_name
                      AND c.column_name = 'updated_at'
               )
             ORDER BY t.table_name
            """
        )

        applied = 0
        skipped: list[str] = []
        for r in rows:
            tbl = r["table_name"]
            if not _VT_NAME_RE.fullmatch(tbl):
                skipped.append(tbl)
                continue
            await conn.execute(
                f"DROP TRIGGER IF EXISTS akb_set_updated_at_trigger ON {tbl}"
            )
            await conn.execute(
                f"CREATE TRIGGER akb_set_updated_at_trigger "
                f"BEFORE UPDATE ON {tbl} "
                f"FOR EACH ROW EXECUTE FUNCTION akb_set_updated_at()"
            )
            applied += 1

    logger.info(
        "Migration 038 applied: akb_set_updated_at() + BEFORE UPDATE trigger "
        "backfilled onto %d vt_* table(s)%s",
        applied,
        f" (skipped {len(skipped)} non-conforming: {skipped})" if skipped else "",
    )


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
