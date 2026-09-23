"""Migration 113: record each registry column's physical name (`pg_name`).

A vault table column now carries two names (#433): `name`, the logical name
callers and every read surface use, and `pg_name`, the physical identifier
the server derived and the only one that reaches SQL. Tables created before
the split have only `name`.

This writes, for every registry column that lacks one, the identifier that
column already has in PostgreSQL: `safe_ident(name)` folded to lowercase —
what the old DDL produced by interpolating it unquoted. Every name created
under the column grammar maps to itself, so for those the registry now simply
says `pg_name == name`.

Registry only. No DDL is issued and no `vt_*` table is touched: the
identifiers already exist and are being written down, not chosen.
`updated_at` is left alone too — the table's schema did not change.

Idempotent: a column that already has `pg_name` is never rewritten, and a
row with nothing to add is not updated at all.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db
from app.repositories.table_data_repo import column_pg_name
from app.repositories.table_registry_repo import parse_columns

logger = logging.getLogger("akb.migration.113")


def backfilled_columns(columns: list) -> list | None:
    """`columns` with `pg_name` recorded on every column that lacks one, or
    None when there is nothing to record."""
    out = []
    changed = False
    for col in columns:
        if isinstance(col, dict) and isinstance(col.get("name"), str) and not col.get("pg_name"):
            col = {**col, "pg_name": column_pg_name(col)}
            changed = True
        out.append(col)
    return out if changed else None


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    updated = 0
    async with conn.transaction():
        rows = await conn.fetch("SELECT id, columns FROM vault_tables FOR UPDATE")
        for row in rows:
            columns = backfilled_columns(parse_columns(row["columns"]))
            if columns is None:
                continue
            await conn.execute(
                "UPDATE vault_tables SET columns = $1::jsonb WHERE id = $2",
                json.dumps(columns), row["id"],
            )
            updated += 1

    logger.info(
        "Migration 113 recorded pg_name on %d of %d vault table registry row(s)",
        updated, len(rows),
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
