"""Keep cutover receipts durable while allowing an authorized vault purge."""

from __future__ import annotations

import logging

logger = logging.getLogger("akb.migrations")


async def migrate(conn) -> None:
    """Detach retained receipts from live rows and narrow the DELETE exception."""
    async with conn.transaction():
        await conn.execute(
            """
            -- A completed cutover is durable authority evidence, not live
            -- vault membership.  Let a whole-vault lifecycle cascade remove
            -- its transient migration/source rows while preserving this
            -- immutable receipt for future authority validation.
            ALTER TABLE native_revision_cutover_vaults
                DROP CONSTRAINT IF EXISTS native_revision_cutover_vaults_migration_fkey;
            ALTER TABLE native_revision_cutover_files
                DROP CONSTRAINT IF EXISTS native_revision_cutover_files_source_fkey;

            CREATE OR REPLACE FUNCTION reject_fenced_legacy_revision_write()
            RETURNS TRIGGER LANGUAGE plpgsql AS $guard$
            DECLARE
                fence_epoch BIGINT;
            BEGIN
                SELECT epoch INTO fence_epoch
                  FROM native_revision_legacy_write_fence
                 WHERE fence_key = TRUE AND state <> 'open';
                IF FOUND THEN
                    IF TG_OP = 'DELETE'
                       AND TG_TABLE_NAME IN ('documents', 'resource_aliases')
                       AND current_setting('akb.native_revision_vault_purge_id', TRUE)
                           = OLD.vault_id::text
                       AND EXISTS (
                           SELECT 1
                             FROM native_revision_existing_authority
                            WHERE marker_id = TRUE AND status = 'committed'
                       ) THEN
                        RETURN OLD;
                    END IF;
                    RAISE EXCEPTION 'Legacy revision writes are fenced at epoch %', fence_epoch
                        USING ERRCODE = '55000';
                END IF;
                IF TG_OP = 'DELETE' THEN
                    RETURN OLD;
                END IF;
                RETURN NEW;
            END;
            $guard$;
            """
        )
    logger.info("Migration 090: durable cutover receipts permit authorized Native vault purges")
