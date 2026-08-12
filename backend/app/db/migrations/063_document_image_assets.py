"""Migration 060: classify editor-uploaded document image assets.

Document images share the object-storage substrate with regular Files, but
they are not standalone knowledge-base resources: they should not appear in
file browse/search surfaces or be independently published/deleted through the
File API.  The discriminator keeps that product boundary explicit while the
bytes retain the existing vault-owned lifecycle (vault deletion is the final
backstop).
"""

from __future__ import annotations


async def migrate(conn) -> None:
    await conn.execute(
        """
        ALTER TABLE vault_files
            ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'file';

        ALTER TABLE vault_files
            DROP CONSTRAINT IF EXISTS ck_vault_files_kind;

        ALTER TABLE vault_files
            ADD CONSTRAINT ck_vault_files_kind
            CHECK (kind IN ('file', 'attachment')) NOT VALID;

        ALTER TABLE vault_files
            VALIDATE CONSTRAINT ck_vault_files_kind;
        """
    )
