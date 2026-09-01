"""Freeze durable cutover receipts once existing-database authority commits."""

from __future__ import annotations

import logging

logger = logging.getLogger("akb.migrations")


async def migrate(conn) -> None:
    """Make the committed cutover receipt set immutable at the database edge."""
    async with conn.transaction():
        await conn.execute(
            """
            CREATE OR REPLACE FUNCTION guard_committed_native_revision_cutover_receipt()
            RETURNS TRIGGER LANGUAGE plpgsql AS $guard$
            DECLARE
                source_cutover_id UUID;
            BEGIN
                IF TG_OP = 'INSERT' THEN
                    source_cutover_id := NEW.cutover_id;
                ELSE
                    source_cutover_id := OLD.cutover_id;
                END IF;

                IF EXISTS (
                    SELECT 1
                      FROM native_revision_existing_authority
                     WHERE cutover_id = source_cutover_id
                       AND status = 'committed'
                ) THEN
                    RAISE EXCEPTION 'committed Native revision cutover receipt is immutable'
                        USING ERRCODE = '55000';
                END IF;

                IF TG_OP = 'UPDATE' THEN
                    IF NEW.cutover_id <> OLD.cutover_id AND EXISTS (
                        SELECT 1
                          FROM native_revision_existing_authority
                         WHERE cutover_id = NEW.cutover_id
                           AND status = 'committed'
                    ) THEN
                        RAISE EXCEPTION 'committed Native revision cutover receipt is immutable'
                            USING ERRCODE = '55000';
                    END IF;
                END IF;

                IF TG_OP = 'DELETE' THEN
                    RETURN OLD;
                END IF;
                RETURN NEW;
            END;
            $guard$;

            DROP TRIGGER IF EXISTS guard_committed_native_revision_cutover_run
                ON native_revision_cutover_runs;
            CREATE TRIGGER guard_committed_native_revision_cutover_run
                BEFORE INSERT OR UPDATE OR DELETE ON native_revision_cutover_runs
                FOR EACH ROW EXECUTE FUNCTION
                    guard_committed_native_revision_cutover_receipt();

            DROP TRIGGER IF EXISTS guard_committed_native_revision_cutover_vault
                ON native_revision_cutover_vaults;
            CREATE TRIGGER guard_committed_native_revision_cutover_vault
                BEFORE INSERT OR UPDATE OR DELETE ON native_revision_cutover_vaults
                FOR EACH ROW EXECUTE FUNCTION
                    guard_committed_native_revision_cutover_receipt();

            DROP TRIGGER IF EXISTS guard_committed_native_revision_cutover_file
                ON native_revision_cutover_files;
            CREATE TRIGGER guard_committed_native_revision_cutover_file
                BEFORE INSERT OR UPDATE OR DELETE ON native_revision_cutover_files
                FOR EACH ROW EXECUTE FUNCTION
                    guard_committed_native_revision_cutover_receipt();

            DROP TRIGGER IF EXISTS guard_committed_native_revision_cutover_exclusion
                ON native_revision_cutover_exclusions;
            CREATE TRIGGER guard_committed_native_revision_cutover_exclusion
                BEFORE INSERT OR UPDATE OR DELETE ON native_revision_cutover_exclusions
                FOR EACH ROW EXECUTE FUNCTION
                    guard_committed_native_revision_cutover_receipt();
            """
        )
    logger.info("Migration 091: committed Native cutover receipts are immutable")
