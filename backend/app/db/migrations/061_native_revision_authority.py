"""Migration 061: explicit PostgreSQL Native authority records."""

from __future__ import annotations

import logging

logger = logging.getLogger("akb.migration.061")


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
            CREATE TABLE IF NOT EXISTS document_revision_bootstrap_claims (
                claim_key BOOLEAN PRIMARY KEY DEFAULT TRUE,
                claim_id UUID NOT NULL UNIQUE,
                record_kind TEXT NOT NULL DEFAULT 'new_database',
                tenant_id TEXT NOT NULL,
                namespace TEXT NOT NULL,
                database_id UUID NOT NULL,
                current_database TEXT NOT NULL,
                runtime_image_digest TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'claimed',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                minted_at TIMESTAMPTZ,
                CONSTRAINT document_revision_bootstrap_claims_singleton_check CHECK (claim_key),
                CONSTRAINT document_revision_bootstrap_claims_kind_check CHECK (record_kind = 'new_database'),
                CONSTRAINT document_revision_bootstrap_claims_tenant_check CHECK (btrim(tenant_id) <> ''),
                CONSTRAINT document_revision_bootstrap_claims_namespace_check CHECK (btrim(namespace) <> ''),
                CONSTRAINT document_revision_bootstrap_claims_database_check CHECK (btrim(current_database) <> ''),
                CONSTRAINT document_revision_bootstrap_claims_digest_check
                    CHECK (runtime_image_digest ~ '^sha256:[0-9a-f]{64}$'),
                CONSTRAINT document_revision_bootstrap_claims_status_check CHECK (status IN ('claimed', 'minted')),
                CONSTRAINT document_revision_bootstrap_claims_minted_shape CHECK (
                    (status = 'claimed' AND minted_at IS NULL)
                    OR (status = 'minted' AND minted_at IS NOT NULL)
                )
            );

            CREATE TABLE IF NOT EXISTS document_revision_authority_pending (
                authority_id UUID PRIMARY KEY,
                claim_id UUID NOT NULL UNIQUE REFERENCES document_revision_bootstrap_claims(claim_id),
                record_kind TEXT NOT NULL DEFAULT 'new_database',
                backend TEXT NOT NULL DEFAULT 'postgres_native',
                tenant_id TEXT NOT NULL,
                namespace TEXT NOT NULL,
                database_id UUID NOT NULL,
                current_database TEXT NOT NULL,
                runtime_image_digest TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                consumed_at TIMESTAMPTZ,
                CONSTRAINT document_revision_authority_pending_kind_check CHECK (record_kind = 'new_database'),
                CONSTRAINT document_revision_authority_pending_backend_check CHECK (backend = 'postgres_native'),
                CONSTRAINT document_revision_authority_pending_tenant_check CHECK (btrim(tenant_id) <> ''),
                CONSTRAINT document_revision_authority_pending_namespace_check CHECK (btrim(namespace) <> ''),
                CONSTRAINT document_revision_authority_pending_database_check CHECK (btrim(current_database) <> ''),
                CONSTRAINT document_revision_authority_pending_digest_check
                    CHECK (runtime_image_digest ~ '^sha256:[0-9a-f]{64}$'),
                CONSTRAINT document_revision_authority_pending_status_check CHECK (status IN ('pending', 'consumed')),
                CONSTRAINT document_revision_authority_pending_consumed_shape CHECK (
                    (status = 'pending' AND consumed_at IS NULL)
                    OR (status = 'consumed' AND consumed_at IS NOT NULL)
                )
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_document_revision_authority_pending_active
                ON document_revision_authority_pending ((TRUE)) WHERE status = 'pending';

            CREATE TABLE IF NOT EXISTS document_revision_authority_marker (
                marker_id BOOLEAN PRIMARY KEY DEFAULT TRUE,
                authority_id UUID NOT NULL UNIQUE REFERENCES document_revision_authority_pending(authority_id),
                claim_id UUID NOT NULL UNIQUE REFERENCES document_revision_bootstrap_claims(claim_id),
                record_kind TEXT NOT NULL DEFAULT 'new_database',
                backend TEXT NOT NULL DEFAULT 'postgres_native',
                tenant_id TEXT NOT NULL,
                namespace TEXT NOT NULL,
                database_id UUID NOT NULL,
                current_database TEXT NOT NULL,
                runtime_image_digest TEXT NOT NULL,
                initialized_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT document_revision_authority_marker_singleton_check CHECK (marker_id),
                CONSTRAINT document_revision_authority_marker_kind_check CHECK (record_kind = 'new_database'),
                CONSTRAINT document_revision_authority_marker_backend_check CHECK (backend = 'postgres_native'),
                CONSTRAINT document_revision_authority_marker_tenant_check CHECK (btrim(tenant_id) <> ''),
                CONSTRAINT document_revision_authority_marker_namespace_check CHECK (btrim(namespace) <> ''),
                CONSTRAINT document_revision_authority_marker_database_check CHECK (btrim(current_database) <> ''),
                CONSTRAINT document_revision_authority_marker_digest_check
                    CHECK (runtime_image_digest ~ '^sha256:[0-9a-f]{64}$')
            );

            CREATE OR REPLACE FUNCTION guard_document_revision_authority_mutation()
            RETURNS TRIGGER LANGUAGE plpgsql AS $guard$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'document revision authority records cannot be deleted';
                END IF;
                IF TG_TABLE_NAME = 'document_revision_bootstrap_claims' THEN
                    IF OLD.status <> 'claimed' OR NEW.status <> 'minted'
                       OR (to_jsonb(OLD) - 'status' - 'minted_at')
                          IS DISTINCT FROM (to_jsonb(NEW) - 'status' - 'minted_at') THEN
                        RAISE EXCEPTION 'invalid document revision bootstrap claim mutation';
                    END IF;
                    RETURN NEW;
                END IF;
                IF TG_TABLE_NAME = 'document_revision_authority_pending' THEN
                    IF OLD.status <> 'pending' OR NEW.status <> 'consumed'
                       OR (to_jsonb(OLD) - 'status' - 'consumed_at')
                          IS DISTINCT FROM (to_jsonb(NEW) - 'status' - 'consumed_at') THEN
                        RAISE EXCEPTION 'invalid document revision pending mutation';
                    END IF;
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION 'document revision authority marker is immutable';
            END;
            $guard$;

            DROP TRIGGER IF EXISTS guard_document_revision_bootstrap_claims
                ON document_revision_bootstrap_claims;
            CREATE TRIGGER guard_document_revision_bootstrap_claims
                BEFORE UPDATE OR DELETE ON document_revision_bootstrap_claims
                FOR EACH ROW EXECUTE FUNCTION guard_document_revision_authority_mutation();

            DROP TRIGGER IF EXISTS guard_document_revision_authority_pending
                ON document_revision_authority_pending;
            CREATE TRIGGER guard_document_revision_authority_pending
                BEFORE UPDATE OR DELETE ON document_revision_authority_pending
                FOR EACH ROW EXECUTE FUNCTION guard_document_revision_authority_mutation();

            DROP TRIGGER IF EXISTS guard_document_revision_authority_marker
                ON document_revision_authority_marker;
            CREATE TRIGGER guard_document_revision_authority_marker
                BEFORE UPDATE OR DELETE ON document_revision_authority_marker
                FOR EACH ROW EXECUTE FUNCTION guard_document_revision_authority_mutation();
            """
        )
    logger.info("Migration 061: PostgreSQL Native authority records ready")
