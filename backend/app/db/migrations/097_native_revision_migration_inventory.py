"""Migration 097: persist one immutable fixed-ref inventory per migration run."""

from __future__ import annotations

import logging

logger = logging.getLogger("akb.migration.097")


async def migrate(conn=None):
    if conn is None:
        from app.db.postgres import get_pool

        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    if await conn.fetchval("SELECT to_regclass('public.native_revision_migration_runs')") is None:
        logger.info("Migration 097 skipped: native revision migration runs are absent")
        return

    async with conn.transaction():
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS native_revision_migration_inventories (
                run_id UUID PRIMARY KEY,
                namespace_id UUID NOT NULL,
                fixed_git_oid TEXT NOT NULL,
                coverage_version TEXT NOT NULL,
                inventory_digest TEXT NOT NULL,
                payload JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT native_revision_migration_inventories_run_fkey
                    FOREIGN KEY (run_id, namespace_id)
                    REFERENCES native_revision_migration_runs(run_id, namespace_id)
                    ON DELETE CASCADE,
                CONSTRAINT native_revision_migration_inventories_fixed_oid_shape
                    CHECK (fixed_git_oid ~ '^[0-9a-f]{40}$'),
                CONSTRAINT native_revision_migration_inventories_coverage_check
                    CHECK (btrim(coverage_version) <> ''),
                CONSTRAINT native_revision_migration_inventories_digest_shape
                    CHECK (inventory_digest ~ '^[0-9a-f]{64}$'),
                CONSTRAINT native_revision_migration_inventories_payload_shape
                    CHECK (
                        jsonb_typeof(payload) = 'object'
                        AND payload->>'schema' = 'c9-fixed-ref-inventory-v1'
                        AND jsonb_typeof(payload->'documents') = 'array'
                    )
            );

            CREATE OR REPLACE FUNCTION akb_check_native_revision_migration_inventory()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                      FROM native_revision_migration_runs run
                     WHERE run.run_id = NEW.run_id
                       AND run.namespace_id = NEW.namespace_id
                       AND run.fixed_git_oid = NEW.fixed_git_oid
                       AND run.coverage_version = NEW.coverage_version
                       AND run.inventory_digest = NEW.inventory_digest
                ) THEN
                    RAISE EXCEPTION
                        'migration inventory does not match its run authority'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$;

            DROP TRIGGER IF EXISTS native_revision_migration_inventory_binding
                ON native_revision_migration_inventories;
            CREATE TRIGGER native_revision_migration_inventory_binding
                BEFORE INSERT ON native_revision_migration_inventories
                FOR EACH ROW
                EXECUTE FUNCTION akb_check_native_revision_migration_inventory();

            CREATE OR REPLACE FUNCTION akb_reject_native_revision_inventory_update()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION 'migration inventory snapshots are immutable'
                    USING ERRCODE = '55000';
            END;
            $$;

            DROP TRIGGER IF EXISTS native_revision_migration_inventory_immutable
                ON native_revision_migration_inventories;
            CREATE TRIGGER native_revision_migration_inventory_immutable
                BEFORE UPDATE ON native_revision_migration_inventories
                FOR EACH ROW
                EXECUTE FUNCTION akb_reject_native_revision_inventory_update();

            REVOKE EXECUTE
                ON FUNCTION akb_check_native_revision_migration_inventory()
                FROM PUBLIC;
            REVOKE EXECUTE
                ON FUNCTION akb_reject_native_revision_inventory_update()
                FROM PUBLIC;
            REVOKE UPDATE ON TABLE native_revision_migration_inventories FROM PUBLIC;
            """
        )

    logger.info("Migration 097 persisted immutable fixed-ref migration inventories")
