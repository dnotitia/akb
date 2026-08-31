"""Coordinate existing-database Native backfill across manual vaults.

It groups the existing vault-scoped migration runs into one database-local
plan, records apply/verification progress, and provides one immutable
existing-database authority handoff after verification.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("akb.migration.087")


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
            CREATE TABLE IF NOT EXISTS native_revision_cutover_runs (
                cutover_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                coverage_version TEXT NOT NULL,
                inventory_digest TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'planned',
                verification_digest TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                applied_at TIMESTAMPTZ,
                verified_at TIMESTAMPTZ,
                CONSTRAINT native_revision_cutover_runs_coverage_check
                    CHECK (btrim(coverage_version) <> ''),
                CONSTRAINT native_revision_cutover_runs_inventory_digest_check
                    CHECK (inventory_digest ~ '^[0-9a-f]{64}$'),
                CONSTRAINT native_revision_cutover_runs_status_check
                    CHECK (status IN ('planned', 'applied', 'verified')),
                CONSTRAINT native_revision_cutover_runs_verification_check
                    CHECK (
                        (status <> 'verified' AND verification_digest IS NULL AND verified_at IS NULL)
                        OR
                        (status = 'verified'
                         AND verification_digest ~ '^[0-9a-f]{64}$'
                         AND verified_at IS NOT NULL)
                    ),
                CONSTRAINT native_revision_cutover_runs_applied_check
                    CHECK (
                        (status = 'planned' AND applied_at IS NULL)
                        OR
                        (status IN ('applied', 'verified') AND applied_at IS NOT NULL)
                    ),
                CONSTRAINT native_revision_cutover_runs_identity_key
                    UNIQUE (coverage_version, inventory_digest)
            );

            CREATE TABLE IF NOT EXISTS native_revision_cutover_vaults (
                cutover_id UUID NOT NULL
                    REFERENCES native_revision_cutover_runs(cutover_id) ON DELETE CASCADE,
                -- Keep the exclusion receipt even after an operator removes or
                -- hands off the source vault and prepares a fresh cutover plan.
                namespace_id UUID NOT NULL,
                migration_run_id UUID NOT NULL,
                fixed_git_oid TEXT NOT NULL,
                inventory_digest TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'planned',
                verification_digest TEXT,
                applied_at TIMESTAMPTZ,
                verified_at TIMESTAMPTZ,
                CONSTRAINT native_revision_cutover_vaults_pkey
                    PRIMARY KEY (cutover_id, namespace_id),
                CONSTRAINT native_revision_cutover_vaults_migration_fkey
                    FOREIGN KEY (migration_run_id, namespace_id)
                    REFERENCES native_revision_migration_runs(run_id, namespace_id)
                    ON DELETE RESTRICT,
                CONSTRAINT native_revision_cutover_vaults_migration_key
                    UNIQUE (migration_run_id),
                CONSTRAINT native_revision_cutover_vaults_fixed_oid_check
                    CHECK (fixed_git_oid ~ '^[0-9a-f]{40}$'),
                CONSTRAINT native_revision_cutover_vaults_inventory_digest_check
                    CHECK (inventory_digest ~ '^[0-9a-f]{64}$'),
                CONSTRAINT native_revision_cutover_vaults_status_check
                    CHECK (status IN ('planned', 'applied', 'verified')),
                CONSTRAINT native_revision_cutover_vaults_verification_check
                    CHECK (
                        (status <> 'verified' AND verification_digest IS NULL AND verified_at IS NULL)
                        OR
                        (status = 'verified'
                         AND verification_digest ~ '^[0-9a-f]{64}$'
                         AND verified_at IS NOT NULL)
                    ),
                CONSTRAINT native_revision_cutover_vaults_applied_check
                    CHECK (
                        (status = 'planned' AND applied_at IS NULL)
                        OR
                        (status IN ('applied', 'verified') AND applied_at IS NOT NULL)
                    )
            );

            CREATE INDEX IF NOT EXISTS idx_native_revision_cutover_vaults_status
                ON native_revision_cutover_vaults(cutover_id, status, namespace_id);

            CREATE TABLE IF NOT EXISTS native_revision_cutover_exclusions (
                cutover_id UUID NOT NULL
                    REFERENCES native_revision_cutover_runs(cutover_id) ON DELETE CASCADE,
                -- This is a durable cutover receipt, not live vault membership.
                -- Keep the excluded namespace UUID after Collector handoff or
                -- source-vault retirement so the authority decision remains
                -- explainable and replay-safe.
                namespace_id UUID NOT NULL,
                fixed_git_oid TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT native_revision_cutover_exclusions_pkey
                    PRIMARY KEY (cutover_id, namespace_id),
                CONSTRAINT native_revision_cutover_exclusions_fixed_oid_check
                    CHECK (fixed_git_oid ~ '^[0-9a-f]{40}$'),
                CONSTRAINT native_revision_cutover_exclusions_reason_check
                    CHECK (reason IN ('external_git_requires_collector'))
            );

            CREATE INDEX IF NOT EXISTS idx_native_revision_cutover_exclusions_reason
                ON native_revision_cutover_exclusions(cutover_id, reason, namespace_id);

            CREATE TABLE IF NOT EXISTS native_revision_cutover_files (
                cutover_id UUID NOT NULL
                    REFERENCES native_revision_cutover_runs(cutover_id) ON DELETE CASCADE,
                namespace_id UUID NOT NULL,
                file_id UUID NOT NULL,
                logical_path TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                byte_size BIGINT NOT NULL,
                s3_key TEXT NOT NULL,
                etag TEXT,
                storage_version TEXT,
                created_by TEXT,
                disposition TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'planned',
                native_revision_id TEXT,
                verification_digest TEXT,
                applied_at TIMESTAMPTZ,
                verified_at TIMESTAMPTZ,
                CONSTRAINT native_revision_cutover_files_pkey
                    PRIMARY KEY (cutover_id, file_id),
                CONSTRAINT native_revision_cutover_files_source_fkey
                    FOREIGN KEY (file_id, namespace_id)
                    REFERENCES vault_files(id, vault_id) ON DELETE RESTRICT,
                CONSTRAINT native_revision_cutover_files_path_check
                    CHECK (btrim(logical_path) <> ''),
                CONSTRAINT native_revision_cutover_files_mime_check
                    CHECK (btrim(mime_type) <> ''),
                CONSTRAINT native_revision_cutover_files_digest_check
                    CHECK (content_hash ~ '^[0-9a-f]{64}$'),
                CONSTRAINT native_revision_cutover_files_size_check
                    CHECK (byte_size >= 0),
                CONSTRAINT native_revision_cutover_files_s3_key_check
                    CHECK (btrim(s3_key) <> ''),
                CONSTRAINT native_revision_cutover_files_disposition_check
                    CHECK (disposition IN ('native_text', 'preserved_binary')),
                CONSTRAINT native_revision_cutover_files_status_check
                    CHECK (status IN ('planned', 'applied', 'verified')),
                CONSTRAINT native_revision_cutover_files_native_shape_check
                    CHECK (
                        (disposition = 'native_text'
                         AND status = 'planned'
                         AND native_revision_id IS NULL)
                        OR
                        (disposition = 'native_text'
                         AND status IN ('applied', 'verified')
                         AND native_revision_id ~ '^[0-9a-f]{40}$')
                        OR
                        (disposition = 'preserved_binary'
                         AND native_revision_id IS NULL)
                    ),
                CONSTRAINT native_revision_cutover_files_verification_check
                    CHECK (
                        (status <> 'verified'
                         AND verification_digest IS NULL
                         AND verified_at IS NULL)
                        OR
                        (status = 'verified'
                         AND verification_digest ~ '^[0-9a-f]{64}$'
                         AND verified_at IS NOT NULL)
                    ),
                CONSTRAINT native_revision_cutover_files_applied_check
                    CHECK (
                        (status = 'planned' AND applied_at IS NULL)
                        OR
                        (status IN ('applied', 'verified') AND applied_at IS NOT NULL)
                    )
            );

            CREATE INDEX IF NOT EXISTS idx_native_revision_cutover_files_status
                ON native_revision_cutover_files(cutover_id, status, namespace_id, file_id);

            CREATE TABLE IF NOT EXISTS native_revision_existing_authority (
                marker_id BOOLEAN PRIMARY KEY DEFAULT TRUE,
                authority_id UUID NOT NULL UNIQUE DEFAULT uuid_generate_v4(),
                cutover_id UUID NOT NULL UNIQUE
                    REFERENCES native_revision_cutover_runs(cutover_id) ON DELETE RESTRICT,
                record_kind TEXT NOT NULL DEFAULT 'existing_database_cutover',
                backend TEXT NOT NULL DEFAULT 'postgres_native',
                tenant_id TEXT NOT NULL,
                namespace TEXT NOT NULL,
                database_id UUID NOT NULL,
                current_database TEXT NOT NULL,
                runtime_image_digest TEXT NOT NULL,
                inventory_digest TEXT NOT NULL,
                verification_digest TEXT NOT NULL,
                vault_binding_digest TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                committed_at TIMESTAMPTZ,
                CONSTRAINT native_revision_existing_authority_singleton_check
                    CHECK (marker_id),
                CONSTRAINT native_revision_existing_authority_kind_check
                    CHECK (record_kind = 'existing_database_cutover'),
                CONSTRAINT native_revision_existing_authority_backend_check
                    CHECK (backend = 'postgres_native'),
                CONSTRAINT native_revision_existing_authority_tenant_check
                    CHECK (btrim(tenant_id) <> ''),
                CONSTRAINT native_revision_existing_authority_namespace_check
                    CHECK (btrim(namespace) <> ''),
                CONSTRAINT native_revision_existing_authority_database_check
                    CHECK (btrim(current_database) <> ''),
                CONSTRAINT native_revision_existing_authority_image_check
                    CHECK (runtime_image_digest ~ '^sha256:[0-9a-f]{64}$'),
                CONSTRAINT native_revision_existing_authority_inventory_check
                    CHECK (inventory_digest ~ '^[0-9a-f]{64}$'),
                CONSTRAINT native_revision_existing_authority_verification_check
                    CHECK (verification_digest ~ '^[0-9a-f]{64}$'),
                CONSTRAINT native_revision_existing_authority_vault_binding_check
                    CHECK (vault_binding_digest ~ '^[0-9a-f]{64}$'),
                CONSTRAINT native_revision_existing_authority_status_check
                    CHECK (status IN ('pending', 'committed')),
                CONSTRAINT native_revision_existing_authority_committed_shape_check
                    CHECK (
                        (status = 'pending' AND committed_at IS NULL)
                        OR (status = 'committed' AND committed_at IS NOT NULL)
                    )
            );

            CREATE OR REPLACE FUNCTION guard_native_revision_existing_authority_mutation()
            RETURNS TRIGGER LANGUAGE plpgsql AS $guard$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'existing database revision authority cannot be deleted';
                END IF;
                IF OLD.status <> 'pending' OR NEW.status <> 'committed'
                   OR (to_jsonb(OLD) - 'status' - 'committed_at')
                      IS DISTINCT FROM (to_jsonb(NEW) - 'status' - 'committed_at') THEN
                    RAISE EXCEPTION 'invalid existing database revision authority mutation';
                END IF;
                RETURN NEW;
            END;
            $guard$;

            DROP TRIGGER IF EXISTS guard_native_revision_existing_authority
                ON native_revision_existing_authority;
            CREATE TRIGGER guard_native_revision_existing_authority
                BEFORE UPDATE OR DELETE ON native_revision_existing_authority
                FOR EACH ROW EXECUTE FUNCTION
                    guard_native_revision_existing_authority_mutation();
            """
        )
    logger.info("Migration 087: existing-database Native cutover coordination ready")
