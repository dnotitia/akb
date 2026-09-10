"""Migration 100: record the native path a cutover File was actually published at.

Legacy File names are not unique — the storage key is what
`UNIQUE(vault_id, s3_key)` covers — and native paths are one authority namespace
across Document and File Resources. So the path a File is published at can differ
from the `logical_path` the plan froze, and both `apply` and `verify` need to
agree on which one it was.

The column is also what makes a retry safe: the publication fingerprint includes
the path, so re-deriving it against live state after a partial apply would raise
`Native idempotency key was reused with different input`. Writing the resolved
path before publishing means a retry replays the same input.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("akb.migration.100")


async def migrate(conn=None):
    if conn is None:
        from app.db.postgres import get_pool

        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    if await conn.fetchval("SELECT to_regclass('public.native_revision_cutover_files')") is None:
        logger.info("Migration 100 skipped: cutover File inventory is absent")
        return

    async with conn.transaction():
        await conn.execute(
            """
            ALTER TABLE native_revision_cutover_files
                ADD COLUMN IF NOT EXISTS applied_path TEXT;

            ALTER TABLE native_revision_cutover_files
                DROP CONSTRAINT IF EXISTS native_revision_cutover_files_applied_path_shape;
            ALTER TABLE native_revision_cutover_files
                ADD CONSTRAINT native_revision_cutover_files_applied_path_shape
                CHECK (applied_path IS NULL OR btrim(applied_path) <> '');
            """
        )

    logger.info("Migration 100 recorded the published native path per cutover File")
