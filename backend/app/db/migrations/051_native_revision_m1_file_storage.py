"""Durable pending/confirmed File storage state for the guarded M1 W4 arm."""

from __future__ import annotations


async def migrate(conn) -> None:
    await conn.execute(
        """
        ALTER TABLE vault_files
            ADD COLUMN IF NOT EXISTS storage_state TEXT NOT NULL DEFAULT 'confirmed',
            ADD COLUMN IF NOT EXISTS storage_driver TEXT,
            ADD COLUMN IF NOT EXISTS storage_locator TEXT;

        ALTER TABLE vault_files DROP CONSTRAINT IF EXISTS vault_files_storage_state_check;
        ALTER TABLE vault_files ADD CONSTRAINT vault_files_storage_state_check
            CHECK (storage_state IN ('pending', 'confirmed'));

        CREATE INDEX IF NOT EXISTS idx_vault_files_measurement_confirmed
            ON vault_files(vault_id, created_at DESC) WHERE storage_state = 'confirmed';

        CREATE TABLE IF NOT EXISTS m1_file_transfer_intents (
            id UUID PRIMARY KEY,
            file_id UUID NOT NULL REFERENCES vault_files(id) ON DELETE CASCADE,
            vault_id UUID NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
            method TEXT NOT NULL CHECK (method IN ('PUT', 'GET')),
            token_digest TEXT NOT NULL UNIQUE CHECK (token_digest ~ '^[0-9a-f]{64}$'),
            expires_at TIMESTAMPTZ NOT NULL,
            consumed_at TIMESTAMPTZ,
            body BYTEA,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK ((method = 'PUT') OR body IS NULL)
        );
        CREATE INDEX IF NOT EXISTS idx_m1_file_transfer_expiry
            ON m1_file_transfer_intents(expires_at) WHERE consumed_at IS NULL;
        CREATE INDEX IF NOT EXISTS idx_m1_file_transfer_file_method
            ON m1_file_transfer_intents(file_id, method);
        """
    )
