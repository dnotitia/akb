"""Migration 025: drop phantom ``path=''`` collection rows.

Pre-fix, ``document_service.put()`` called
``coll_repo.get_or_create(vault_id, "")`` whenever a user put a doc
without specifying a collection. That inserted a phantom row with
``path=''`` and ``name=''`` per affected vault, and every "vault-root"
doc/file/table pointed its ``collection_id`` FK at it.

Two visible bugs followed (issues #81, #82):
  * ``akb_browse`` emitted an empty-name collection marker — every
    rendering client had to special-case it out of the response.
  * Root-level docs lived under a phantom collection rather than
    ``collection_id IS NULL``, so the unified browse couldn't surface
    them at ``depth=1`` (which is the symmetric behaviour we want with
    root-level tables/files).

The fix landed at the call site
(``document_service.py`` — only call ``get_or_create`` when the
normalized collection path is non-empty, matching the peer pattern in
``file_service``, ``table_service``, ``external_git_service``). This
migration cleans up the data already in production:

  1. Re-NULL the FK on documents / vault_files / vault_tables that
     pointed at any phantom collection.
  2. DELETE the phantom rows themselves.

The FKs already declare ``ON DELETE SET NULL`` (init.sql:100/180/238),
so step 1 is technically redundant — but explicit is clearer in the
audit log and removes the brief window where the cascade hasn't
finished. Idempotent: re-running after the prod DB is clean is a no-op
(`UPDATE` over an empty set, `DELETE` over an empty set).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db

logger = logging.getLogger("akb.migration.025")


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    async with conn.transaction():
        # FK is ON DELETE SET NULL, so the explicit UPDATEs below are
        # belt-and-braces — they fire even on a schema variant that lost
        # the cascade, and they leave a clear audit trail.
        doc_count = await conn.fetchval(
            """
            WITH updated AS (
                UPDATE documents
                   SET collection_id = NULL
                 WHERE collection_id IN (
                     SELECT id FROM collections WHERE path = ''
                 )
                RETURNING 1
            )
            SELECT COUNT(*) FROM updated
            """
        )
        file_count = await conn.fetchval(
            """
            WITH updated AS (
                UPDATE vault_files
                   SET collection_id = NULL
                 WHERE collection_id IN (
                     SELECT id FROM collections WHERE path = ''
                 )
                RETURNING 1
            )
            SELECT COUNT(*) FROM updated
            """
        )
        table_count = await conn.fetchval(
            """
            WITH updated AS (
                UPDATE vault_tables
                   SET collection_id = NULL
                 WHERE collection_id IN (
                     SELECT id FROM collections WHERE path = ''
                 )
                RETURNING 1
            )
            SELECT COUNT(*) FROM updated
            """
        )
        phantom_count = await conn.fetchval(
            """
            WITH deleted AS (
                DELETE FROM collections WHERE path = ''
                RETURNING 1
            )
            SELECT COUNT(*) FROM deleted
            """
        )

    if phantom_count:
        logger.info(
            "Migration 025: cleared %d phantom path='' collection(s); "
            "re-NULLed FK on %d documents, %d files, %d tables",
            phantom_count, doc_count, file_count, table_count,
        )
    else:
        logger.info("Migration 025: no phantom collections found; nothing to clean up")


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
