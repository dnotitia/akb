"""Durable, credential-free receipts for offline external-Git retirement."""

from __future__ import annotations

import logging

logger = logging.getLogger("akb.migrations")


async def migrate(conn) -> None:
    """Add the one-way retirement intent/receipt ledger.

    ``vault_id`` intentionally has no foreign key: this is audit evidence for
    a completed handoff and must not vanish if a later, separately-authorized
    whole-vault lifecycle purge removes the live vault rows.  The table never
    stores the manifest payload, document content, or external Git token.
    """
    async with conn.transaction():
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS external_git_retirements (
                retirement_id UUID NOT NULL UNIQUE DEFAULT uuid_generate_v4(),
                vault_id UUID PRIMARY KEY,
                vault_name TEXT NOT NULL,
                manifest_digest TEXT NOT NULL,
                document_count INTEGER NOT NULL,
                remote_url TEXT NOT NULL,
                remote_branch TEXT NOT NULL,
                last_synced_sha TEXT NOT NULL,
                idempotency_key UUID NOT NULL UNIQUE,
                requested_by TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'quarantined',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                retired_at TIMESTAMPTZ,
                CONSTRAINT external_git_retirements_vault_name_check
                    CHECK (btrim(vault_name) <> ''),
                CONSTRAINT external_git_retirements_manifest_digest_check
                    CHECK (manifest_digest ~ '^[0-9a-f]{64}$'),
                CONSTRAINT external_git_retirements_document_count_check
                    CHECK (document_count >= 0),
                CONSTRAINT external_git_retirements_remote_url_check
                    CHECK (btrim(remote_url) <> ''),
                CONSTRAINT external_git_retirements_remote_branch_check
                    CHECK (btrim(remote_branch) <> ''),
                CONSTRAINT external_git_retirements_last_synced_sha_check
                    CHECK (last_synced_sha ~ '^[0-9a-f]{40}$'),
                CONSTRAINT external_git_retirements_requested_by_check
                    CHECK (btrim(requested_by) <> ''),
                CONSTRAINT external_git_retirements_status_check
                    CHECK (status IN ('quarantined', 'retired')),
                CONSTRAINT external_git_retirements_status_shape_check
                    CHECK (
                        (status = 'quarantined' AND retired_at IS NULL)
                        OR (status = 'retired' AND retired_at IS NOT NULL)
                    )
            );

            CREATE INDEX IF NOT EXISTS idx_external_git_retirements_status
                ON external_git_retirements(status, created_at);

            CREATE OR REPLACE FUNCTION guard_external_git_retirement_receipt()
            RETURNS TRIGGER LANGUAGE plpgsql AS $guard$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'external Git retirement receipt cannot be deleted'
                        USING ERRCODE = '55000';
                END IF;
                IF NEW.retirement_id IS DISTINCT FROM OLD.retirement_id
                   OR NEW.vault_id IS DISTINCT FROM OLD.vault_id
                   OR NEW.vault_name IS DISTINCT FROM OLD.vault_name
                   OR NEW.manifest_digest IS DISTINCT FROM OLD.manifest_digest
                   OR NEW.document_count IS DISTINCT FROM OLD.document_count
                   OR NEW.remote_url IS DISTINCT FROM OLD.remote_url
                   OR NEW.remote_branch IS DISTINCT FROM OLD.remote_branch
                   OR NEW.last_synced_sha IS DISTINCT FROM OLD.last_synced_sha
                   OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
                   OR NEW.requested_by IS DISTINCT FROM OLD.requested_by
                   OR NEW.created_at IS DISTINCT FROM OLD.created_at
                THEN
                    RAISE EXCEPTION 'external Git retirement receipt identity is immutable'
                        USING ERRCODE = '55000';
                END IF;
                IF OLD.status = 'quarantined'
                   AND NEW.status = 'retired'
                   AND OLD.retired_at IS NULL
                   AND NEW.retired_at IS NOT NULL
                THEN
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION 'external Git retirement receipt transition is invalid'
                    USING ERRCODE = '55000';
            END;
            $guard$;

            DROP TRIGGER IF EXISTS guard_external_git_retirement_receipt
                ON external_git_retirements;
            CREATE TRIGGER guard_external_git_retirement_receipt
                BEFORE UPDATE OR DELETE ON external_git_retirements
                FOR EACH ROW EXECUTE FUNCTION guard_external_git_retirement_receipt();
            """
        )
    logger.info("Migration 093: external-Git retirement receipt ledger ready")
