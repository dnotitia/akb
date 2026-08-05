"""Migration 053: scope M1 payload deduplication to its selected placement.

M1 initially admitted only one measured payload placement per namespace.  A
native document using the reference adapter and a native text File using the
PostgreSQL BodyStore are independent Resources, so their verified payloads
must coexist even when their canonical bytes are identical.  The manifest's
existing composite foreign key keeps each Resource pinned to its exact
placement-specific payload facts.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("akb.migration.053")


async def migrate(conn=None):
    if conn is None:
        from app.db.postgres import get_pool

        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
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
