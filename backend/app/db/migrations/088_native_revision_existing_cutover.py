"""Coordinate and fence an existing-database Native authority cutover.

It groups the existing vault-scoped migration runs into one database-local
plan, records apply/verification progress, and provides one DB-enforced
Legacy write epoch plus immutable authority handoff after verification.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("akb.migration.088")


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

            CREATE TABLE IF NOT EXISTS native_revision_legacy_write_fence (
                fence_key BOOLEAN PRIMARY KEY DEFAULT TRUE,
                epoch BIGINT NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'open',
                cutover_id UUID UNIQUE
                    REFERENCES native_revision_cutover_runs(cutover_id) ON DELETE RESTRICT,
                fenced_at TIMESTAMPTZ,
                committed_at TIMESTAMPTZ,
                CONSTRAINT native_revision_legacy_write_fence_singleton_check
                    CHECK (fence_key),
                CONSTRAINT native_revision_legacy_write_fence_epoch_check
                    CHECK (epoch >= 0),
                CONSTRAINT native_revision_legacy_write_fence_state_check
                    CHECK (state IN ('open', 'fenced', 'committed')),
                CONSTRAINT native_revision_legacy_write_fence_shape_check
                    CHECK (
                        (state = 'open'
                         AND epoch = 0
                         AND cutover_id IS NULL
                         AND fenced_at IS NULL
                         AND committed_at IS NULL)
                        OR
                        (state = 'fenced'
                         AND epoch > 0
                         AND cutover_id IS NOT NULL
                         AND fenced_at IS NOT NULL
                         AND committed_at IS NULL)
                        OR
                        (state = 'committed'
                         AND epoch > 0
                         AND cutover_id IS NOT NULL
                         AND fenced_at IS NOT NULL
                         AND committed_at IS NOT NULL)
                    )
            );

            INSERT INTO native_revision_legacy_write_fence (fence_key)
            VALUES (TRUE)
            ON CONFLICT (fence_key) DO NOTHING;

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
                legacy_write_epoch BIGINT NOT NULL,
                status TEXT NOT NULL DEFAULT 'committed',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                committed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
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
                CONSTRAINT native_revision_existing_authority_epoch_check
                    CHECK (legacy_write_epoch > 0),
                CONSTRAINT native_revision_existing_authority_status_check
                    CHECK (status = 'committed'),
                CONSTRAINT native_revision_existing_authority_committed_shape_check
                    CHECK (committed_at IS NOT NULL)
            );

            CREATE OR REPLACE FUNCTION guard_native_revision_existing_authority_mutation()
            RETURNS TRIGGER LANGUAGE plpgsql AS $guard$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'existing database revision authority cannot be deleted';
                END IF;
                RAISE EXCEPTION 'existing database revision authority is immutable';
            END;
            $guard$;

            DROP TRIGGER IF EXISTS guard_native_revision_existing_authority
                ON native_revision_existing_authority;
            CREATE TRIGGER guard_native_revision_existing_authority
                BEFORE UPDATE OR DELETE ON native_revision_existing_authority
                FOR EACH ROW EXECUTE FUNCTION
                    guard_native_revision_existing_authority_mutation();

            CREATE OR REPLACE FUNCTION guard_native_revision_legacy_fence_mutation()
            RETURNS TRIGGER LANGUAGE plpgsql AS $guard$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'Native Legacy write fence cannot be deleted';
                END IF;
                IF OLD.state = 'open'
                   AND NEW.state = 'fenced'
                   AND NEW.epoch = OLD.epoch + 1
                   AND NEW.cutover_id IS NOT NULL
                   AND NEW.fenced_at IS NOT NULL
                   AND NEW.committed_at IS NULL THEN
                    RETURN NEW;
                END IF;
                IF OLD.state = 'fenced'
                   AND NEW.state = 'committed'
                   AND NEW.epoch = OLD.epoch
                   AND NEW.cutover_id = OLD.cutover_id
                   AND NEW.fenced_at = OLD.fenced_at
                   AND NEW.committed_at IS NOT NULL THEN
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION 'invalid Native Legacy write fence transition';
            END;
            $guard$;

            DROP TRIGGER IF EXISTS guard_native_revision_legacy_fence
                ON native_revision_legacy_write_fence;
            CREATE TRIGGER guard_native_revision_legacy_fence
                BEFORE UPDATE OR DELETE ON native_revision_legacy_write_fence
                FOR EACH ROW EXECUTE FUNCTION
                    guard_native_revision_legacy_fence_mutation();

            CREATE OR REPLACE FUNCTION reject_fenced_legacy_revision_write()
            RETURNS TRIGGER LANGUAGE plpgsql AS $guard$
            DECLARE
                fence_epoch BIGINT;
            BEGIN
                SELECT epoch INTO fence_epoch
                  FROM native_revision_legacy_write_fence
                 WHERE fence_key = TRUE AND state <> 'open';
                IF FOUND THEN
                    RAISE EXCEPTION 'Legacy revision writes are fenced at epoch %', fence_epoch
                        USING ERRCODE = '55000';
                END IF;
                IF TG_OP = 'DELETE' THEN
                    RETURN OLD;
                END IF;
                RETURN NEW;
            END;
            $guard$;

            -- These triggers fence the Legacy revision representation after
            -- authority minting.  Ordinary Native-era vault, collection, and
            -- File catalog writes remain valid; the mint transaction locks and
            -- revalidates those complete inventories before crossing the
            -- boundary.
            DROP TRIGGER IF EXISTS guard_native_legacy_write ON vaults;
            DROP TRIGGER IF EXISTS guard_native_legacy_write ON collections;
            DROP TRIGGER IF EXISTS guard_native_legacy_write ON vault_files;

            DROP TRIGGER IF EXISTS guard_native_legacy_write ON documents;
            CREATE TRIGGER guard_native_legacy_write
                BEFORE INSERT OR UPDATE ON documents
                FOR EACH ROW EXECUTE FUNCTION reject_fenced_legacy_revision_write();

            DROP TRIGGER IF EXISTS guard_native_legacy_write ON resource_aliases;
            CREATE TRIGGER guard_native_legacy_write
                BEFORE INSERT OR UPDATE ON resource_aliases
                FOR EACH ROW EXECUTE FUNCTION reject_fenced_legacy_revision_write();

            DROP TRIGGER IF EXISTS guard_native_legacy_write ON vault_external_git;
            CREATE TRIGGER guard_native_legacy_write
                BEFORE INSERT OR UPDATE OR DELETE ON vault_external_git
                FOR EACH ROW EXECUTE FUNCTION reject_fenced_legacy_revision_write();
            """
        )
    logger.info("Migration 088: fenced existing-database Native cutover ready")
