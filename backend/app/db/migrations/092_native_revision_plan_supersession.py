"""Release never-applied migration reservations after an explicit plan abort."""

from __future__ import annotations

import logging

logger = logging.getLogger("akb.migrations")


async def migrate(conn) -> None:
    """Keep aborted plan evidence while allowing a new coverage version to plan."""
    async with conn.transaction():
        await conn.execute(
            """
            ALTER TABLE native_revision_migration_runs
                DROP CONSTRAINT IF EXISTS native_revision_migration_runs_status_check;
            ALTER TABLE native_revision_migration_runs
                ADD CONSTRAINT native_revision_migration_runs_status_check
                CHECK (status IN ('planned', 'running', 'complete', 'failed', 'superseded'));

            ALTER TABLE native_revision_migration_items
                ADD COLUMN IF NOT EXISTS reservation_active BOOLEAN NOT NULL DEFAULT TRUE;
            ALTER TABLE native_revision_migration_items
                DROP CONSTRAINT IF EXISTS native_revision_migration_items_reservation_state_check;
            ALTER TABLE native_revision_migration_items
                ADD CONSTRAINT native_revision_migration_items_reservation_state_check
                CHECK (reservation_active OR status = 'pending');
            ALTER TABLE native_revision_migration_items
                DROP CONSTRAINT IF EXISTS native_revision_migration_items_resource_head_key;
            CREATE UNIQUE INDEX IF NOT EXISTS
                native_revision_migration_items_active_resource_head_key
                ON native_revision_migration_items(native_resource_id, legacy_head_oid)
                WHERE reservation_active;

            -- A prior version of abort retained a planned cutover and its
            -- all-pending inventory forever.  Those rows are durable audit
            -- evidence, but cannot still reserve a document/head: the linked
            -- cutover is permanently non-applicable and no native material was
            -- published.  Do not release any applied, failed, or active run.
            UPDATE native_revision_migration_items item
               SET reservation_active = FALSE,
                   updated_at = NOW()
             WHERE item.run_id IN (
                 SELECT DISTINCT run.run_id
                   FROM native_revision_migration_runs run
                   JOIN native_revision_cutover_vaults vault
                     ON vault.migration_run_id = run.run_id
                   JOIN native_revision_cutover_runs cutover
                     ON cutover.cutover_id = vault.cutover_id
                  WHERE run.status = 'planned'
                    AND cutover.status = 'aborted'
                    AND cutover.aborted_from_status = 'planned'
                    AND NOT EXISTS (
                        SELECT 1
                          FROM native_revision_migration_items candidate
                         WHERE candidate.run_id = run.run_id
                           AND candidate.status <> 'pending'
                    )
                    AND NOT EXISTS (
                        SELECT 1
                          FROM native_revision_cutover_vaults other_vault
                          JOIN native_revision_cutover_runs other_cutover
                            ON other_cutover.cutover_id = other_vault.cutover_id
                         WHERE other_vault.migration_run_id = run.run_id
                           AND (
                               other_cutover.status <> 'aborted'
                               OR other_cutover.aborted_from_status <> 'planned'
                           )
                    )
             );

            UPDATE native_revision_migration_runs run
               SET status = 'superseded'
             WHERE run.status = 'planned'
               AND EXISTS (
                   SELECT 1
                     FROM native_revision_cutover_vaults vault
                     JOIN native_revision_cutover_runs cutover
                       ON cutover.cutover_id = vault.cutover_id
                    WHERE vault.migration_run_id = run.run_id
                      AND cutover.status = 'aborted'
                      AND cutover.aborted_from_status = 'planned'
               )
               AND NOT EXISTS (
                   SELECT 1
                     FROM native_revision_migration_items candidate
                    WHERE candidate.run_id = run.run_id
                      AND candidate.status <> 'pending'
               )
               AND NOT EXISTS (
                   SELECT 1
                     FROM native_revision_cutover_vaults other_vault
                     JOIN native_revision_cutover_runs other_cutover
                       ON other_cutover.cutover_id = other_vault.cutover_id
                    WHERE other_vault.migration_run_id = run.run_id
                      AND (
                          other_cutover.status <> 'aborted'
                          OR other_cutover.aborted_from_status <> 'planned'
                      )
               );
            """
        )
    logger.info("Migration 092: aborted Native cutover plans release inactive reservations")
