"""Bind Native public links to a vault-scoped Document identity, not a path."""
from __future__ import annotations

SCHEMA = """
ALTER TABLE publications ADD COLUMN IF NOT EXISTS native_document_id UUID;
DO $$ BEGIN
    ALTER TABLE publications ADD CONSTRAINT publications_native_document_binding_check
        CHECK (native_document_id IS NULL OR
               (resource_type = 'document' AND document_id IS NULL));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE publications ADD CONSTRAINT publications_native_document_fk
        FOREIGN KEY (vault_id, resource_type, native_document_id)
        REFERENCES native_resources(namespace_id, surface, resource_id)
        ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
CREATE INDEX IF NOT EXISTS idx_publications_native_document_id
    ON publications(vault_id, native_document_id)
    WHERE native_document_id IS NOT NULL;

CREATE OR REPLACE FUNCTION native_document_publication_lifecycle()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE vault_name TEXT; location TEXT;
BEGIN
    IF NEW.surface <> 'document' THEN RETURN NEW; END IF;
    IF NEW.lifecycle <> 'live' THEN
        DELETE FROM publications
         WHERE vault_id = NEW.namespace_id AND native_document_id = NEW.resource_id;
    ELSIF NEW.current_path IS DISTINCT FROM OLD.current_path THEN
        SELECT name INTO vault_name FROM vaults WHERE id = NEW.namespace_id;
        IF strpos(NEW.current_path, '/') > 0 THEN
            location := 'akb://' || vault_name || '/coll/' ||
                regexp_replace(NEW.current_path, '/[^/]*$', '') || '/doc/' ||
                regexp_replace(NEW.current_path, '^.*/', '');
        ELSE
            location := 'akb://' || vault_name || '/doc/' || NEW.current_path;
        END IF;
        UPDATE publications SET resource_uri = location, updated_at = NOW()
         WHERE vault_id = NEW.namespace_id AND native_document_id = NEW.resource_id;
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS native_document_publication_lifecycle ON native_resources;
CREATE TRIGGER native_document_publication_lifecycle
    AFTER UPDATE OF lifecycle, current_path ON native_resources
    FOR EACH ROW EXECUTE FUNCTION native_document_publication_lifecycle();
"""


async def migrate(conn=None):
    from app.services.native_publication_binding import rebind_verified_native_publications
    if conn is None:
        from app.db.postgres import get_pool
        pool = await get_pool()
        async with pool.acquire() as connection:
            await migrate(connection)
        return
    async with conn.transaction():
        await conn.execute(SCHEMA)
        await rebind_verified_native_publications(conn)
