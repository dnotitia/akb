"""Migration 049: add the explicit M1 PostgreSQL BodyStore candidate.

Migration 048 deliberately labelled its BYTEA payload as a reference adapter so
the B-core experiment could not accidentally select a physical text profile.
M1's B-text arm needs a separately labelled candidate while retaining the same
manifest foreign-key, integrity, and content-addressed deduplication checks.
The experiment keeps one placement profile per namespace; the existing
``(namespace_id, digest, byte_size)`` key intentionally rejects mixed-profile
coexistence rather than creating ambiguous manifest identity.
It admits ``pg-bodystore-v1`` without changing any legacy/default write path.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("akb.migration.049")


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
            ALTER TABLE m1_reference_payloads
                DROP CONSTRAINT IF EXISTS m1_reference_payloads_placement_check;
            ALTER TABLE m1_reference_payloads
                ADD CONSTRAINT m1_reference_payloads_placement_check
                CHECK (selected_placement IN (
                    'm1-reference-payload-v1',
                    'pg-bodystore-v1'
                ));
            """
        )
    logger.info("Migration 049: explicit M1 PostgreSQL BodyStore candidate ready")
