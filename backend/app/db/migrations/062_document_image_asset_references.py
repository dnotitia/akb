"""Migration 062: make document-image reachability explicit and collectable.

``document_asset_refs`` is the live authorization set.  It follows document
identity and cascades away with a document deletion.  The revision table is a
bounded manifest for Git commits; it intentionally has no document FK so a
recent historical revision remains renderable after its document is deleted.
Both tables bind the asset and vault in one composite FK, preventing an
accidental cross-vault reference even in future writer paths.
"""

from __future__ import annotations


async def migrate(conn) -> None:
    await conn.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                 WHERE conname = 'vault_files_id_vault_id_key'
                   AND conrelid = 'vault_files'::regclass
            ) THEN
                ALTER TABLE vault_files
                    ADD CONSTRAINT vault_files_id_vault_id_key UNIQUE (id, vault_id);
            END IF;
        END $$;

        CREATE TABLE IF NOT EXISTS document_asset_refs (
            document_id UUID NOT NULL,
            vault_id UUID NOT NULL,
            asset_id UUID NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (document_id, asset_id),
            CONSTRAINT document_asset_refs_document_fk
                FOREIGN KEY (document_id, vault_id)
                REFERENCES documents(id, vault_id) ON DELETE CASCADE,
            CONSTRAINT document_asset_refs_asset_fk
                FOREIGN KEY (asset_id, vault_id)
                REFERENCES vault_files(id, vault_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_document_asset_refs_asset
            ON document_asset_refs (asset_id, vault_id);

        CREATE TABLE IF NOT EXISTS document_asset_revision_refs (
            vault_id UUID NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
            document_path TEXT NOT NULL,
            commit_hash TEXT NOT NULL,
            asset_id UUID NOT NULL,
            retain_until TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (vault_id, document_path, commit_hash, asset_id),
            CONSTRAINT document_asset_revision_refs_asset_fk
                FOREIGN KEY (asset_id, vault_id)
                REFERENCES vault_files(id, vault_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_document_asset_revision_refs_asset_retention
            ON document_asset_revision_refs (asset_id, retain_until);
        """
    )
