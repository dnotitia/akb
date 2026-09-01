"""Migration 095: make the app release registry v2-only."""

from __future__ import annotations

import logging

logger = logging.getLogger("akb.migration.095")


async def migrate(conn=None):
    if conn is None:
        from app.db.postgres import get_pool

        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    async with conn.transaction():
        await conn.execute(
            """
            ALTER TABLE app_releases
                DROP CONSTRAINT IF EXISTS app_releases_manifest_shape,
                DROP CONSTRAINT IF EXISTS app_releases_checksum_shape,
                DROP CONSTRAINT IF EXISTS app_releases_version_nonempty,
                DROP CONSTRAINT IF EXISTS app_releases_version_semver,
                DROP CONSTRAINT IF EXISTS app_releases_manifest_v2_shape,
                DROP CONSTRAINT IF EXISTS app_releases_checksum_v2_shape;

            ALTER TABLE app_releases
                ADD CONSTRAINT app_releases_version_semver
                CHECK (
                    version ~ '^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)(-[0-9A-Za-z-]+(\\.[0-9A-Za-z-]+)*)?(\\+[0-9A-Za-z-]+(\\.[0-9A-Za-z-]+)*)?$'
                ),
                ADD CONSTRAINT app_releases_manifest_v2_shape
                CHECK (
                    jsonb_typeof(manifest) = 'object'
                    AND manifest->>'manifest_version' = '2'
                    AND jsonb_typeof(manifest->'app_key') = 'string'
                    AND btrim(manifest->>'app_key') <> ''
                    AND jsonb_typeof(manifest->'source_revision') = 'string'
                    AND manifest->>'source_revision' ~ '^[0-9A-Fa-f]{40,64}$'
                    AND jsonb_typeof(manifest->'image_digest') = 'string'
                    AND manifest->>'image_digest' ~ '^sha256:[0-9a-f]{64}$'
                    AND jsonb_typeof(manifest->'schema_version') = 'number'
                    AND (manifest->>'schema_version') ~ '^[1-9][0-9]*$'
                    AND jsonb_typeof(manifest->'schema') = 'object'
                    AND jsonb_typeof(manifest->'schema'->'tables') = 'array'
                    AND jsonb_typeof(manifest->'schema'->'fingerprint') = 'string'
                    AND manifest->'schema'->>'fingerprint' ~ '^[0-9a-f]{64}$'
                    AND jsonb_typeof(manifest->'transition_plans') = 'array'
                    AND jsonb_array_length(manifest->'transition_plans') > 0
                ),
                ADD CONSTRAINT app_releases_checksum_v2_shape
                CHECK (manifest_checksum ~ '^[0-9a-f]{64}$');
            """
        )

        await conn.execute(
            """
            CREATE OR REPLACE FUNCTION akb_check_release_manifest_app_key()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            DECLARE
                registered_app_key TEXT;
            BEGIN
                SELECT app_key INTO registered_app_key
                  FROM app_definitions
                 WHERE id = NEW.app_id;
                IF registered_app_key IS NULL
                   OR NEW.manifest->>'app_key' IS DISTINCT FROM registered_app_key
                THEN
                    RAISE EXCEPTION
                        'release manifest app identity does not match its app'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$;

            DROP TRIGGER IF EXISTS app_releases_manifest_app_key
                ON app_releases;
            CREATE TRIGGER app_releases_manifest_app_key
                BEFORE INSERT ON app_releases
                FOR EACH ROW EXECUTE FUNCTION akb_check_release_manifest_app_key();

            REVOKE EXECUTE ON FUNCTION akb_check_release_manifest_app_key()
                FROM PUBLIC;
            """
        )

        await conn.execute(
            """
            REVOKE ALL PRIVILEGES ON TABLE app_releases FROM PUBLIC;
            """
        )

    logger.info("Migration 095 enforced the v2-only app release manifest contract")
