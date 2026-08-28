"""Migration 086: statement-level wake-up events for dynamic table writes.

Dynamic vault tables need to notify downstream readers when their source state
changes. The trigger is statement-level and uses PostgreSQL transition tables,
so one INSERT, UPDATE, or DELETE produces at most one outbox row regardless of
how many rows the statement affects. Empty transition tables produce no event.

The trigger function is SECURITY DEFINER because the per-user SQL roles can
write their granted data tables but must not write the system ``events`` table.
The executor supplies the authenticated actor through a transaction-local GUC;
the event insert remains in the caller's transaction and therefore rolls back
with the data write.

Existing registered dynamic tables are backfilled here. New tables install the
same triggers in ``table_data_repo.create_dynamic_table``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db
from app.repositories.table_data_repo import (
    install_dynamic_table_rows_changed_triggers,
    pg_table_name,
)
from app.services.uri_service import table_uri

logger = logging.getLogger("akb.migration.086")

_DYNAMIC_TABLE_RE = re.compile(r"^vt_[a-z0-9_]+$")


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    await conn.execute(
        """
        CREATE OR REPLACE FUNCTION public.akb_dynamic_table_rows_changed()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
        DECLARE
            changed_rows BIGINT;
            operation TEXT := TG_ARGV[2];
            actor TEXT;
        BEGIN
            IF operation = 'insert' THEN
                SELECT COUNT(*) INTO changed_rows FROM akb_rows_changed_new;
            ELSIF operation = 'update' THEN
                SELECT COUNT(*) INTO changed_rows FROM akb_rows_changed_new;
            ELSIF operation = 'delete' THEN
                SELECT COUNT(*) INTO changed_rows FROM akb_rows_changed_old;
            ELSE
                RAISE EXCEPTION 'unknown dynamic table row-change operation: %', operation;
            END IF;

            IF changed_rows = 0 THEN
                RETURN NULL;
            END IF;

            actor := NULLIF(current_setting('akb.actor_id', true), '');
            INSERT INTO public.events (vault_id, kind, resource_uri, actor_id, payload)
            VALUES (
                TG_ARGV[0]::uuid,
                'table.rows_changed',
                TG_ARGV[1],
                actor,
                jsonb_build_object('operation', operation)
            );
            RETURN NULL;
        END;
        $function$
        """
    )

    rows = await conn.fetch(
        """
        SELECT vt.vault_id, v.name AS vault_name, vt.name AS table_name,
               c.path AS collection
          FROM vault_tables vt
          JOIN vaults v ON v.id = vt.vault_id
          LEFT JOIN collections c ON c.id = vt.collection_id
         ORDER BY v.name, vt.name
        """
    )

    installed = 0
    skipped = 0
    for row in rows:
        pg_name = pg_table_name(row["vault_name"], row["table_name"])
        if not _DYNAMIC_TABLE_RE.fullmatch(pg_name):
            skipped += 1
            continue
        if not await conn.fetchval(
            "SELECT to_regclass($1) IS NOT NULL", f"public.{pg_name}"
        ):
            skipped += 1
            continue
        await install_dynamic_table_rows_changed_triggers(
            conn,
            pg_name,
            vault_id=row["vault_id"],
            resource_uri=table_uri(
                row["vault_name"], row["table_name"], row["collection"]
            ),
        )
        installed += 1

    logger.info(
        "Migration 086 applied: rows_changed trigger installed on %d dynamic table(s)"
        " (skipped %d missing or non-conforming table(s))",
        installed,
        skipped,
    )


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
