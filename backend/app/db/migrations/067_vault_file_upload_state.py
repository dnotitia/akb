"""Migration 064: distinguish pending uploads from legacy confirmed Files.

Files created before content hashing was introduced legitimately have no
``hash_verified_at`` value.  Upload readiness therefore needs its own state;
using the hash timestamp would hide those existing Files from publication and
search. Existing rows are confirmed by definition because the old schema had
no durable pending state. New rows default to pending for rolling-deploy
safety; a trigger also recognizes the legacy confirmation write to
``hash_verified_at`` so older application instances remain compatible.
"""

from __future__ import annotations


async def migrate(conn) -> None:
    await conn.execute(
        """
        ALTER TABLE vault_files
            ADD COLUMN IF NOT EXISTS upload_state TEXT;

        UPDATE vault_files
           SET upload_state = 'confirmed'
         WHERE upload_state IS NULL;

        ALTER TABLE vault_files
            ALTER COLUMN upload_state SET DEFAULT 'pending',
            ALTER COLUMN upload_state SET NOT NULL;

        ALTER TABLE vault_files
            DROP CONSTRAINT IF EXISTS ck_vault_files_upload_state;

        ALTER TABLE vault_files
            ADD CONSTRAINT ck_vault_files_upload_state
            CHECK (upload_state IN ('pending', 'confirmed')) NOT VALID;

        ALTER TABLE vault_files
            VALIDATE CONSTRAINT ck_vault_files_upload_state;

        CREATE OR REPLACE FUNCTION akb_confirm_vault_file_upload()
        RETURNS TRIGGER AS $fn$
        BEGIN
            IF NEW.hash_verified_at IS NOT NULL THEN
                NEW.upload_state := 'confirmed';
            END IF;
            RETURN NEW;
        END;
        $fn$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS trg_vault_files_confirm_upload ON vault_files;
        CREATE TRIGGER trg_vault_files_confirm_upload
            BEFORE INSERT OR UPDATE OF hash_verified_at ON vault_files
            FOR EACH ROW EXECUTE FUNCTION akb_confirm_vault_file_upload();
        """
    )
