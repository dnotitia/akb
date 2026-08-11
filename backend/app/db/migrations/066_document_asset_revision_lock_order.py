"""Remove a redundant parent FK from retained document-image manifests.

The composite ``(asset_id, vault_id)`` FK already proves vault ownership and
cascades revision rows when a vault's assets are removed. A second direct FK to
``vaults`` added no integrity but made every manifest insert acquire a parent
key-share lock after locking its asset row, opposite to vault deletion's lock
order. Removing only the redundant edge preserves both isolation and cleanup.
"""

from __future__ import annotations


async def migrate(conn) -> None:
    await conn.execute(
        """
        ALTER TABLE document_asset_revision_refs
            DROP CONSTRAINT IF EXISTS document_asset_revision_refs_vault_id_fkey;
        """
    )
