"""Migration 065: index attachment health counts and stale File uploads."""

from __future__ import annotations


async def migrate(conn) -> None:
    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_vault_files_claimed_attachments
            ON vault_files (updated_at, created_at, id)
            WHERE kind = 'attachment' AND attachment_claimed_at IS NOT NULL;

        CREATE INDEX IF NOT EXISTS idx_vault_files_pending_uploads
            ON vault_files (updated_at, id)
            WHERE kind = 'file' AND upload_state = 'pending';
        """
    )
