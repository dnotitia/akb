"""Permit an aborted completed cutover to transfer its active reservation."""

from __future__ import annotations

import logging

logger = logging.getLogger("akb.migrations")


async def migrate(conn) -> None:
    """Keep completed audit rows while a replacement run adopts their work."""
    async with conn.transaction():
        await conn.execute(
            """
            ALTER TABLE native_revision_migration_items
                DROP CONSTRAINT IF EXISTS native_revision_migration_items_reservation_state_check;
            ALTER TABLE native_revision_migration_items
                ADD CONSTRAINT native_revision_migration_items_reservation_state_check
                CHECK (reservation_active OR status IN ('pending', 'complete'));
            """
        )
    logger.info("Migration 094: completed aborted cutovers may transfer reservations")
