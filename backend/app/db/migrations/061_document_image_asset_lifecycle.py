"""Migration 061: track whether an editor image reached a document commit.

An uploaded image begins unclaimed.  A document writer atomically marks every
referenced image as claimed in the same PostgreSQL transaction that advances
the document's authoritative ``current_commit``.  Only unclaimed images may be
discarded by the editor; claimed bytes remain available to historical Git
revisions until the vault is deleted.
"""

from __future__ import annotations


async def migrate(conn) -> None:
    await conn.execute(
        """
        ALTER TABLE vault_files
            ADD COLUMN IF NOT EXISTS attachment_claimed_at TIMESTAMPTZ;

        CREATE INDEX IF NOT EXISTS idx_vault_files_unclaimed_attachments
            ON vault_files (vault_id, created_at)
            WHERE kind = 'attachment' AND attachment_claimed_at IS NULL;
        """
    )
