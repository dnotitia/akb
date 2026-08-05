"""Durable transfer intents and confirmed-only File storage metadata for M1 W4."""

from __future__ import annotations


async def migrate(conn) -> None:
    await conn.execute(
        """
        ALTER TABLE vault_files
            ADD COLUMN IF NOT EXISTS storage_driver TEXT,
            ADD COLUMN IF NOT EXISTS storage_locator TEXT,
            ADD COLUMN IF NOT EXISTS native_resource_id UUID,
            ADD COLUMN IF NOT EXISTS native_revision_id TEXT;

        CREATE UNIQUE INDEX IF NOT EXISTS uq_vault_files_m1_exact_content
            ON vault_files (
                vault_id,
                COALESCE(collection_id, '00000000-0000-0000-0000-000000000000'::uuid),
                name,
                content_hash
            )
            WHERE content_hash IS NOT NULL AND storage_driver IS NOT NULL;

        CREATE TABLE IF NOT EXISTS m1_file_transfer_intents (
            id UUID PRIMARY KEY,
            file_id UUID NOT NULL,
            vault_id UUID NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
            collection_id UUID REFERENCES collections(id) ON DELETE CASCADE,
            method TEXT NOT NULL CHECK (method IN ('PUT', 'GET')),
            filename TEXT,
            mime_type TEXT,
            description TEXT,
            actor_id TEXT,
            declared_content_hash TEXT,
            token_digest TEXT NOT NULL UNIQUE CHECK (token_digest ~ '^[0-9a-f]{64}$'),
            expires_at TIMESTAMPTZ NOT NULL,
            consumed_at TIMESTAMPTZ,
            state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'transferred')),
            body BYTEA,
            actual_content_hash TEXT,
            actual_size BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK (declared_content_hash IS NULL OR declared_content_hash ~ '^[0-9a-f]{64}$'),
            CHECK (actual_content_hash IS NULL OR actual_content_hash ~ '^[0-9a-f]{64}$'),
            CHECK (actual_size IS NULL OR actual_size >= 0),
            CHECK (body IS NULL OR octet_length(body) <= 134217728),
            CHECK (
                (method = 'GET' AND filename IS NULL AND body IS NULL)
                OR
                (method = 'PUT' AND filename IS NOT NULL AND mime_type IS NOT NULL AND actor_id IS NOT NULL)
            )
        );
        CREATE INDEX IF NOT EXISTS idx_m1_file_transfer_file_method
            ON m1_file_transfer_intents(file_id, vault_id, method, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_m1_file_transfer_expiry
            ON m1_file_transfer_intents(expires_at);
        """
    )
