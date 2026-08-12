"""Index physical-key reachability and pending-delete barrier probes."""

from __future__ import annotations


async def migrate(conn) -> None:
    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_vault_files_s3_key
            ON vault_files (s3_key);
        CREATE INDEX IF NOT EXISTS idx_s3_delete_pending_key
            ON s3_delete_outbox (s3_key)
            WHERE processed_at IS NULL;
        """
    )
