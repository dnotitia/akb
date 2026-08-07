"""Migration 059: admit native text File chunks to the derived vocabulary.

Migration 054 gave native Documents a derived chunk discriminator
(``native_document``) that is deliberately distinct from the legacy
``document`` authority.  Text Files need the same separation for the same
reason, and for one more: a native text File's ``resource_id`` *is* the
public ``vault_files.id``, so reusing the legacy ``file`` discriminator would
make the two authorities collide on one ``(source_type, source_id)`` key —
each one's chunk replacement would silently delete the other's rows.

``direct_grep`` stays in ``native_invalidation_delivery_outcome_check``.  It is
no longer produced (text Files now take the document-parity derived path), but
rows closed by the pre-parity measurement worker are history, not garbage.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("akb.migration.059")


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
            ALTER TABLE chunks
                DROP CONSTRAINT IF EXISTS chunks_source_type_check;
            ALTER TABLE chunks
                ADD CONSTRAINT chunks_source_type_check
                CHECK (source_type IN (
                    'document', 'native_document', 'native_file', 'table', 'file'
                ));
            """
        )
    logger.info("Migration 059: native text File derived chunks admitted")
