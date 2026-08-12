"""Migration 060: additive C9 native/legacy revision bridge metadata.

The migration ledger remains the sole rollout authority.  These relations
only record a bounded migration run, its per-document observations, and the
stable selector metadata needed to resolve retained legacy history.  They do
not add a cutover flag, change the legacy writer, or backfill any rows.

The run and mapping namespace columns are intentionally repeated where
needed for composite foreign keys.  PostgreSQL can then reject completed
authority links that combine a run, Resource, or Revision from different
vaults without coupling the pre-authority inventory ledger to either the
legacy projection or native rows.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("akb.migration.060")


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
            CREATE TABLE IF NOT EXISTS native_revision_migration_runs (
                run_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                namespace_id UUID NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
                fixed_git_oid TEXT NOT NULL,
                coverage_version TEXT NOT NULL,
                inventory_digest TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'planned',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                started_at TIMESTAMPTZ,
                completed_at TIMESTAMPTZ,
                error TEXT,
                CONSTRAINT native_revision_migration_runs_fixed_oid_shape
                    CHECK (fixed_git_oid ~ '^[0-9a-f]{40}$'),
                CONSTRAINT native_revision_migration_runs_coverage_version_check
                    CHECK (btrim(coverage_version) <> ''),
                CONSTRAINT native_revision_migration_runs_inventory_digest_shape
                    CHECK (inventory_digest ~ '^[0-9a-f]{64}$'),
                CONSTRAINT native_revision_migration_runs_status_check
                    CHECK (status IN ('planned', 'running', 'complete', 'failed')),
                CONSTRAINT native_revision_migration_runs_error_check
                    CHECK (
                        (status <> 'failed' AND error IS NULL)
                        OR
                        (status = 'failed' AND btrim(error) <> '')
                    ),
                CONSTRAINT native_revision_migration_runs_scope_key
                    UNIQUE (run_id, namespace_id),
                CONSTRAINT native_revision_migration_runs_identity_key
                    UNIQUE (namespace_id, fixed_git_oid, coverage_version)
            );

            CREATE INDEX IF NOT EXISTS idx_native_revision_migration_runs_scope_status
                ON native_revision_migration_runs(namespace_id, status, created_at DESC);

            CREATE TABLE IF NOT EXISTS native_revision_migration_items (
                run_id UUID NOT NULL,
                namespace_id UUID NOT NULL,
                legacy_document_id UUID NOT NULL,
                native_resource_id UUID NOT NULL,
                captured_path TEXT NOT NULL,
                legacy_head_oid TEXT NOT NULL,
                native_head_revision_id TEXT,
                body_digest TEXT NOT NULL,
                byte_size BIGINT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                error_code TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT native_revision_migration_items_pkey
                    PRIMARY KEY (run_id, legacy_document_id),
                CONSTRAINT native_revision_migration_items_run_fkey
                    FOREIGN KEY (run_id, namespace_id)
                    REFERENCES native_revision_migration_runs(run_id, namespace_id)
                    ON DELETE CASCADE,
                CONSTRAINT native_revision_migration_items_head_fkey
                    FOREIGN KEY (
                        namespace_id,
                        native_resource_id,
                        native_head_revision_id
                    )
                    REFERENCES native_revisions(namespace_id, resource_id, revision_id)
                    DEFERRABLE INITIALLY DEFERRED,
                CONSTRAINT native_revision_migration_items_identity_check
                    CHECK (native_resource_id = legacy_document_id),
                CONSTRAINT native_revision_migration_items_path_check
                    CHECK (btrim(captured_path) <> ''),
                CONSTRAINT native_revision_migration_items_legacy_oid_shape
                    CHECK (legacy_head_oid ~ '^[0-9a-f]{40}$'),
                CONSTRAINT native_revision_migration_items_body_digest_shape
                    CHECK (body_digest ~ '^[0-9a-f]{64}$'),
                CONSTRAINT native_revision_migration_items_size_check
                    CHECK (byte_size >= 0),
                CONSTRAINT native_revision_migration_items_status_check
                    CHECK (status IN ('pending', 'complete', 'failed')),
                CONSTRAINT native_revision_migration_items_error_code_check
                    CHECK (
                        error_code IS NULL
                        OR error_code ~ '^[a-z][a-z0-9_]*$'
                    ),
                CONSTRAINT native_revision_migration_items_error_state_check
                    CHECK (
                        (status = 'pending'
                         AND native_head_revision_id IS NULL
                         AND error_code IS NULL)
                        OR
                        (status = 'complete'
                         AND native_head_revision_id IS NOT NULL
                         AND error_code IS NULL)
                        OR
                        (status = 'failed'
                         AND native_head_revision_id IS NULL
                         AND error_code IS NOT NULL)
                    ),
                CONSTRAINT native_revision_migration_items_resource_head_key
                    UNIQUE (native_resource_id, legacy_head_oid)
            );

            CREATE INDEX IF NOT EXISTS idx_native_revision_migration_items_run_status
                ON native_revision_migration_items(run_id, status, legacy_document_id);
            CREATE INDEX IF NOT EXISTS idx_native_revision_migration_items_resource
                ON native_revision_migration_items(native_resource_id, legacy_head_oid);

            CREATE TABLE IF NOT EXISTS legacy_revision_mappings (
                namespace_id UUID NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
                resource_id UUID NOT NULL,
                legacy_git_oid TEXT NOT NULL,
                path_at_revision TEXT NOT NULL,
                resolution TEXT NOT NULL,
                native_revision_id TEXT,
                run_id UUID NOT NULL,
                lineage_ordinal INTEGER NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT legacy_revision_mappings_pkey
                    PRIMARY KEY (resource_id, legacy_git_oid),
                CONSTRAINT legacy_revision_mappings_resource_fkey
                    FOREIGN KEY (namespace_id, resource_id)
                    REFERENCES native_resources(namespace_id, resource_id)
                    ON DELETE CASCADE,
                CONSTRAINT legacy_revision_mappings_run_fkey
                    FOREIGN KEY (run_id, namespace_id)
                    REFERENCES native_revision_migration_runs(run_id, namespace_id)
                    ON DELETE CASCADE,
                CONSTRAINT legacy_revision_mappings_revision_fkey
                    FOREIGN KEY (namespace_id, resource_id, native_revision_id)
                    REFERENCES native_revisions(namespace_id, resource_id, revision_id)
                    ON DELETE NO ACTION,
                CONSTRAINT legacy_revision_mappings_legacy_oid_shape
                    CHECK (legacy_git_oid ~ '^[0-9a-f]{40}$'),
                CONSTRAINT legacy_revision_mappings_path_check
                    CHECK (btrim(path_at_revision) <> ''),
                CONSTRAINT legacy_revision_mappings_resolution_check
                    CHECK (resolution IN ('native', 'bridge')),
                CONSTRAINT legacy_revision_mappings_resolution_link_check
                    CHECK (
                        (resolution = 'native' AND native_revision_id IS NOT NULL)
                        OR
                        (resolution = 'bridge' AND native_revision_id IS NULL)
                    ),
                CONSTRAINT legacy_revision_mappings_revision_shape
                    CHECK (
                        native_revision_id IS NULL
                        OR native_revision_id ~ '^[0-9a-f]{40}$'
                    ),
                CONSTRAINT legacy_revision_mappings_lineage_ordinal_check
                    CHECK (lineage_ordinal >= 0)
            );

            CREATE INDEX IF NOT EXISTS idx_legacy_revision_mappings_namespace_resource
                ON legacy_revision_mappings(namespace_id, resource_id, lineage_ordinal);
            CREATE INDEX IF NOT EXISTS idx_legacy_revision_mappings_run
                ON legacy_revision_mappings(run_id, namespace_id);
            CREATE UNIQUE INDEX IF NOT EXISTS uq_legacy_revision_mappings_native_revision
                ON legacy_revision_mappings(resource_id, native_revision_id)
             WHERE native_revision_id IS NOT NULL;
            """
        )
    logger.info("Migration 060: native revision migration bridge schema ready")
