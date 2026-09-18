"""Let an inline image be owned by a Native document, not only a Git one."""

from __future__ import annotations

SCHEMA = """
-- A document image becomes readable by being REFERENCED: `read_document_image`
-- authorizes bytes when a live row here names the asset. That row could only
-- ever name a `documents.id`, which is the Git arm's table — a Native document
-- lives in `native_resources` and has no row there. So on a postgres_native
-- deployment every uploaded inline image stayed unreferenced and therefore
-- unreadable, and the document rendered a broken image with no error anywhere.
--
-- The shape here is the one `publications` already uses for the same problem
-- (migration 106): one nullable column per arm, exactly one of them set.
ALTER TABLE document_asset_refs
    ADD COLUMN IF NOT EXISTS native_document_id UUID;

-- The primary key was (document_id, asset_id), and PostgreSQL refuses to make
-- a primary-key column nullable, so the key goes first. Two partial unique
-- indexes below keep exactly the same guarantee per arm — one live reference
-- from a given document to a given asset.
ALTER TABLE document_asset_refs DROP CONSTRAINT IF EXISTS document_asset_refs_pkey;

-- `document_id` was NOT NULL because the Git arm was the only arm.
ALTER TABLE document_asset_refs
    ALTER COLUMN document_id DROP NOT NULL;

-- The composite FK below needs a `surface` to match on, and an asset
-- reference may only ever be owned by a document. Generating the value from
-- `native_document_id` rather than storing it makes "this resource is a
-- document" a property of the row instead of a rule someone has to remember:
-- pointing the column at a Native *file* fails the FK rather than quietly
-- authorizing that file's bytes to be served as an inline image.
ALTER TABLE document_asset_refs
    ADD COLUMN IF NOT EXISTS native_surface TEXT
    GENERATED ALWAYS AS (
        CASE WHEN native_document_id IS NULL THEN NULL ELSE 'document' END
    ) STORED;

DO $$ BEGIN
    ALTER TABLE document_asset_refs
        ADD CONSTRAINT document_asset_refs_one_arm_check
        CHECK (num_nonnulls(document_id, native_document_id) = 1);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    ALTER TABLE document_asset_refs
        ADD CONSTRAINT document_asset_refs_native_document_fk
        FOREIGN KEY (vault_id, native_surface, native_document_id)
        REFERENCES native_resources(namespace_id, surface, resource_id)
        ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_document_asset_refs_git
    ON document_asset_refs (document_id, asset_id)
    WHERE document_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_document_asset_refs_native
    ON document_asset_refs (native_document_id, asset_id)
    WHERE native_document_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_document_asset_refs_native_document
    ON document_asset_refs (vault_id, native_document_id)
    WHERE native_document_id IS NOT NULL;

-- Deleting a Native document does not delete its row: `delete_resource` sets
-- `lifecycle = 'deleted'` and leaves it in place, so `ON DELETE CASCADE` never
-- fires. Without this the live reference would outlive the document and keep
-- authorizing its images forever. The service layer extends the historical
-- manifest before it gets here, so retention is unaffected — this drops only
-- the LIVE edge, and it is the backstop for any path that retires a resource
-- without going through the document service.
CREATE OR REPLACE FUNCTION native_document_asset_ref_lifecycle()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.surface <> 'document' THEN RETURN NEW; END IF;
    IF NEW.lifecycle <> 'live' THEN
        DELETE FROM document_asset_refs
         WHERE vault_id = NEW.namespace_id
           AND native_document_id = NEW.resource_id;
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS native_document_asset_ref_lifecycle ON native_resources;
CREATE TRIGGER native_document_asset_ref_lifecycle
    AFTER UPDATE OF lifecycle ON native_resources
    FOR EACH ROW EXECUTE FUNCTION native_document_asset_ref_lifecycle();
"""


async def migrate(conn=None):
    if conn is None:
        from app.db.postgres import get_pool
        pool = await get_pool()
        async with pool.acquire() as connection:
            await connection.execute(SCHEMA)
    else:
        await conn.execute(SCHEMA)
