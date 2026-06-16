"""Migration 036: resource_aliases — rename/move redirect table.

Phase 2 of the doc identity work (docs/designs/doc-identity-slug/00-overview.md).
A former reference (old path/name) maps to the CURRENT resource id, so old
akb:// URIs keep resolving after a document/table/file is moved or renamed.
Keying on the durable id (never on a new path) collapses N renames to one hop —
no redirect chains.

Idempotent: CREATE TABLE / INDEX IF NOT EXISTS. On a fresh DB the table is
already present from init.sql, so this is a no-op there too.
"""

from __future__ import annotations

import logging

from app.db.postgres import get_pool

logger = logging.getLogger("akb.migration.036")


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
        CREATE TABLE IF NOT EXISTS resource_aliases (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            vault_id UUID NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
            resource_type TEXT NOT NULL CHECK(resource_type IN ('document', 'table', 'file')),
            old_ref TEXT NOT NULL,
            resource_id UUID NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(vault_id, resource_type, old_ref)
        )
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_resource_aliases_lookup "
        "ON resource_aliases(vault_id, resource_type, old_ref)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_resource_aliases_resource "
        "ON resource_aliases(resource_id)"
    )
    logger.info("Migration 036: resource_aliases ready")
