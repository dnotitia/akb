"""Constrain the guarded M1 File placement discriminator and locators."""

from __future__ import annotations


async def migrate(conn) -> None:
    await conn.execute(
        """
        ALTER TABLE vault_files
            DROP CONSTRAINT IF EXISTS ck_vault_files_m1_storage_placement;

        ALTER TABLE vault_files
            ADD CONSTRAINT ck_vault_files_m1_storage_placement CHECK (
                (
                        (
                            storage_driver IS NULL
                            AND storage_locator IS NULL
                            AND native_resource_id IS NULL
                            AND native_revision_id IS NULL
                        )
                        OR
                        (
                            storage_driver IN ('fscas', 's3cas')
                            AND storage_locator IS NOT NULL
                            AND storage_locator <> ''
                            AND native_resource_id IS NULL
                            AND native_revision_id IS NULL
                        )
                        OR
                        (
                            storage_driver = 'native_text'
                            AND storage_locator IS NOT NULL
                            AND storage_locator <> ''
                            AND native_resource_id IS NOT NULL
                            AND native_revision_id ~ '^[0-9a-f]{40}$'
                        )
                ) IS TRUE
            ) NOT VALID;

        ALTER TABLE vault_files
            VALIDATE CONSTRAINT ck_vault_files_m1_storage_placement;
        """
    )
