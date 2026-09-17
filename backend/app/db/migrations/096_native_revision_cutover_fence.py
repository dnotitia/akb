"""Migration 096: durable two-phase Native cutover fence state."""

from __future__ import annotations

import logging

logger = logging.getLogger("akb.migration.096")


async def migrate(conn=None):
    if conn is None:
        from app.db.postgres import get_pool

        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    required = (
        "native_revision_cutover_runs",
        "native_revision_legacy_write_fence",
    )
    missing = [
        name
        for name in required
        if await conn.fetchval("SELECT to_regclass($1) IS NULL", f"public.{name}")
    ]
    if missing:
        logger.info(
            "Migration 096 skipped: required cutover table(s) are absent: %s",
            ", ".join(missing),
        )
        return

    authority_table_present = await conn.fetchval(
        "SELECT to_regclass('public.native_revision_existing_authority') IS NOT NULL"
    )

    async with conn.transaction():
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS native_revision_existing_authority_fence (
                fence_key BOOLEAN PRIMARY KEY DEFAULT TRUE,
                fence_token UUID NOT NULL UNIQUE,
                cutover_id UUID NOT NULL UNIQUE
                    REFERENCES native_revision_cutover_runs(cutover_id) ON DELETE RESTRICT,
                legacy_write_epoch BIGINT NOT NULL,
                tenant_id TEXT NOT NULL,
                namespace TEXT NOT NULL,
                database_id UUID NOT NULL,
                current_database TEXT NOT NULL,
                runtime_image_digest TEXT NOT NULL,
                inventory_digest TEXT NOT NULL,
                verification_digest TEXT NOT NULL,
                vault_binding_digest TEXT NOT NULL,
                fenced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT native_revision_existing_authority_fence_singleton_check
                    CHECK (fence_key),
                CONSTRAINT native_revision_existing_authority_fence_epoch_check
                    CHECK (legacy_write_epoch > 0),
                CONSTRAINT native_revision_existing_authority_fence_tenant_check
                    CHECK (btrim(tenant_id) <> ''),
                CONSTRAINT native_revision_existing_authority_fence_namespace_check
                    CHECK (btrim(namespace) <> ''),
                CONSTRAINT native_revision_existing_authority_fence_database_check
                    CHECK (btrim(current_database) <> ''),
                CONSTRAINT native_revision_existing_authority_fence_image_check
                    CHECK (runtime_image_digest ~ '^sha256:[0-9a-f]{64}$'),
                CONSTRAINT native_revision_existing_authority_fence_inventory_check
                    CHECK (inventory_digest ~ '^[0-9a-f]{64}$'),
                CONSTRAINT native_revision_existing_authority_fence_verification_check
                    CHECK (verification_digest ~ '^[0-9a-f]{64}$'),
                CONSTRAINT native_revision_existing_authority_fence_binding_check
                    CHECK (vault_binding_digest ~ '^[0-9a-f]{64}$')
            );

            -- Databases that already completed the pre-096 one-transaction
            -- cutover need a durable phase-A receipt before the new API is
            -- allowed to inspect or resume that authority.  This is a
            -- one-time compatibility backfill; future rows are inserted by
            -- phase A and are never updated.

            CREATE OR REPLACE FUNCTION guard_native_revision_existing_authority_fence_mutation()
            RETURNS TRIGGER LANGUAGE plpgsql AS $guard$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'existing database authority fence cannot be deleted'
                        USING ERRCODE = '55000';
                END IF;
                RAISE EXCEPTION 'existing database authority fence is immutable'
                    USING ERRCODE = '55000';
            END;
            $guard$;

            DROP TRIGGER IF EXISTS guard_native_revision_existing_authority_fence
                ON native_revision_existing_authority_fence;
            CREATE TRIGGER guard_native_revision_existing_authority_fence
                BEFORE UPDATE OR DELETE ON native_revision_existing_authority_fence
                FOR EACH ROW EXECUTE FUNCTION
                    guard_native_revision_existing_authority_fence_mutation();

            CREATE OR REPLACE FUNCTION reject_fenced_native_revision_cutover_write()
            RETURNS TRIGGER LANGUAGE plpgsql AS $guard$
            DECLARE
                fence_state TEXT;
                fence_epoch BIGINT;
            BEGIN
                SELECT state, epoch
                  INTO fence_state, fence_epoch
                  FROM native_revision_legacy_write_fence
                 WHERE fence_key = TRUE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'Native Legacy write fence is missing'
                        USING ERRCODE = '55000';
                END IF;
                IF fence_state = 'fenced' THEN
                    RAISE EXCEPTION 'Native cutover writes are fenced at epoch %', fence_epoch
                        USING ERRCODE = '55000';
                END IF;
                IF TG_OP = 'TRUNCATE' THEN
                    RETURN NULL;
                END IF;
                IF TG_OP = 'DELETE' THEN
                    RETURN OLD;
                END IF;
                RETURN NEW;
            END;
            $guard$;
            """
        )
        if authority_table_present:
            await conn.execute(
                """
                INSERT INTO native_revision_existing_authority_fence (
                    fence_key, fence_token, cutover_id, legacy_write_epoch,
                    tenant_id, namespace, database_id, current_database,
                    runtime_image_digest, inventory_digest, verification_digest,
                    vault_binding_digest, fenced_at, created_at
                )
                SELECT TRUE, uuid_generate_v4(), authority.cutover_id,
                       authority.legacy_write_epoch, authority.tenant_id,
                       authority.namespace, authority.database_id,
                       authority.current_database, authority.runtime_image_digest,
                       authority.inventory_digest, authority.verification_digest,
                       authority.vault_binding_digest,
                       COALESCE(legacy.fenced_at, authority.created_at, NOW()),
                       COALESCE(authority.created_at, NOW())
                  FROM native_revision_existing_authority authority
                  JOIN native_revision_legacy_write_fence legacy
                    ON legacy.fence_key = TRUE
                   AND legacy.state = 'committed'
                   AND legacy.epoch = authority.legacy_write_epoch
                   AND legacy.cutover_id = authority.cutover_id
                 WHERE authority.marker_id = TRUE
                   AND authority.status = 'committed'
                ON CONFLICT (fence_key) DO NOTHING
                """
            )
    logger.info("Migration 096: two-phase Native cutover fence state ready")
