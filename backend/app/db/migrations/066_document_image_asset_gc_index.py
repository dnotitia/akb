"""Migration 063: index bounded document-image revision expiry scans.

The asset-first index serves reachability checks for one attachment. The GC
worker has the inverse access pattern: find the oldest expired manifests across
all assets. A retain-until-first index keeps that periodic batch from scanning
the full revision table as Git history accumulates.
"""

from __future__ import annotations


async def migrate(conn) -> None:
    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_document_asset_revision_refs_expiry
            ON document_asset_revision_refs (retain_until);
        """
    )
