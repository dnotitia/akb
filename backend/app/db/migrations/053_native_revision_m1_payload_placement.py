"""Migration 053: scope M1 payload deduplication to its selected placement.

M1 initially admitted only one measured payload placement per namespace.  A
native document using the reference adapter and a native text File using the
PostgreSQL BodyStore are independent Resources, so their verified payloads
must coexist even when their canonical bytes are identical.  The manifest's
existing composite foreign key keeps each Resource pinned to its exact
placement-specific payload facts.

This contract is intentionally limited to the disposable M1 measurement
database (and its isolated, suffixed test derivatives).  That database is
forward-only and owned by one exact candidate image: rollback to an older
binary requires recreating the measurement database, never reusing a schema
to which this migration was applied.  Normal AKB databases retain the
pre-053 schema.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("akb.migration.053")
MEASUREMENT_DATABASE_PREFIX = "akb_revision_m1_measurement"


async def migrate(conn=None):
    if conn is None:
        from app.db.postgres import get_pool

        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    database = await conn.fetchval("SELECT current_database()")
    if not isinstance(database, str) or not (
        database == MEASUREMENT_DATABASE_PREFIX
        or database.startswith(MEASUREMENT_DATABASE_PREFIX + "_")
    ):
        logger.info("Migration 053: non-measurement database left unchanged")
        return
    async with conn.transaction():
        await conn.execute(
            """
            DROP TRIGGER IF EXISTS trg_m1_namespace_payload_placement
                ON m1_reference_payloads;
            DROP FUNCTION IF EXISTS akb_m1_enforce_namespace_payload_placement();

            ALTER TABLE m1_reference_payloads
                DROP CONSTRAINT IF EXISTS m1_reference_payloads_dedup_key;
            ALTER TABLE m1_reference_payloads
                ADD CONSTRAINT m1_reference_payloads_dedup_key
                UNIQUE (namespace_id, digest, byte_size, selected_placement);
            """
        )
    logger.info("Migration 053: M1 payload deduplication is placement-scoped")
