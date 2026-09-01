"""Migration 047: app desired-state registry.

The registry is control-plane state, not Vault data-plane state. It defines
stable apps, immutable releases, one installation per app/Vault pair,
monotonic grants, Vault-scoped resource ownership, and registry identity
immutability without changing existing users, Vaults, tokens, or Vault content.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("akb.migration.047")


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
            CREATE TABLE IF NOT EXISTS app_definitions (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                app_key TEXT NOT NULL,
                display_name TEXT,
                description TEXT,
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            ALTER TABLE app_definitions
                ADD COLUMN IF NOT EXISTS display_name TEXT,
                ADD COLUMN IF NOT EXISTS description TEXT,
                ADD COLUMN IF NOT EXISTS metadata JSONB
                    NOT NULL DEFAULT '{}'::jsonb,
                ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ
                    NOT NULL DEFAULT NOW(),
                ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ
                    NOT NULL DEFAULT NOW();

            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_definitions_app_key_key'
                       AND conrelid = 'app_definitions'::regclass
                ) THEN
                    ALTER TABLE app_definitions
                        ADD CONSTRAINT app_definitions_app_key_key
                        UNIQUE (app_key);
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_definitions_app_key_nonempty'
                       AND conrelid = 'app_definitions'::regclass
                ) THEN
                    ALTER TABLE app_definitions
                        ADD CONSTRAINT app_definitions_app_key_nonempty
                        CHECK (btrim(app_key) <> '');
                END IF;
            END
            $$;

            CREATE TABLE IF NOT EXISTS app_releases (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                app_id UUID NOT NULL,
                version TEXT NOT NULL,
                manifest JSONB NOT NULL,
                manifest_checksum TEXT NOT NULL,
                registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_releases_app_id_fkey'
                       AND conrelid = 'app_releases'::regclass
                ) THEN
                    ALTER TABLE app_releases
                        ADD CONSTRAINT app_releases_app_id_fkey
                        FOREIGN KEY (app_id) REFERENCES app_definitions(id)
                        ON DELETE RESTRICT;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_releases_app_id_version_key'
                       AND conrelid = 'app_releases'::regclass
                ) THEN
                    ALTER TABLE app_releases
                        ADD CONSTRAINT app_releases_app_id_version_key
                        UNIQUE (app_id, version);
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_releases_app_id_id_key'
                       AND conrelid = 'app_releases'::regclass
                ) THEN
                    ALTER TABLE app_releases
                        ADD CONSTRAINT app_releases_app_id_id_key
                        UNIQUE (app_id, id);
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_releases_version_nonempty'
                       AND conrelid = 'app_releases'::regclass
                ) THEN
                    ALTER TABLE app_releases
                        ADD CONSTRAINT app_releases_version_nonempty
                        CHECK (btrim(version) <> '');
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_releases_manifest_shape'
                       AND conrelid = 'app_releases'::regclass
                ) THEN
                    ALTER TABLE app_releases
                        ADD CONSTRAINT app_releases_manifest_shape
                        CHECK (
                            jsonb_typeof(manifest) = 'object'
                            AND manifest ? 'steps'
                            AND jsonb_typeof(manifest->'steps') = 'array'
                        );
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_releases_checksum_shape'
                       AND conrelid = 'app_releases'::regclass
                ) THEN
                    ALTER TABLE app_releases
                        ADD CONSTRAINT app_releases_checksum_shape
                        CHECK (manifest_checksum ~ '^[0-9a-f]{64}$');
                END IF;
            END
            $$;

            CREATE TABLE IF NOT EXISTS vault_app_installations (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                app_id UUID NOT NULL,
                vault_id UUID NOT NULL,
                desired_release_id UUID,
                current_release_id UUID,
                lifecycle TEXT NOT NULL,
                blocked_reason TEXT,
                grant_generation BIGINT NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'vault_app_installations_app_id_fkey'
                       AND conrelid = 'vault_app_installations'::regclass
                ) THEN
                    ALTER TABLE vault_app_installations
                        ADD CONSTRAINT vault_app_installations_app_id_fkey
                        FOREIGN KEY (app_id) REFERENCES app_definitions(id)
                        ON DELETE RESTRICT;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'vault_app_installations_vault_id_fkey'
                       AND conrelid = 'vault_app_installations'::regclass
                ) OR EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'vault_app_installations_vault_id_fkey'
                       AND conrelid = 'vault_app_installations'::regclass
                       AND confdeltype <> 'c'
                ) THEN
                    ALTER TABLE vault_app_installations
                        DROP CONSTRAINT IF EXISTS
                        vault_app_installations_vault_id_fkey;
                    ALTER TABLE vault_app_installations
                        ADD CONSTRAINT vault_app_installations_vault_id_fkey
                        FOREIGN KEY (vault_id) REFERENCES vaults(id)
                        ON DELETE CASCADE;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'vault_app_installations_app_vault_key'
                       AND conrelid = 'vault_app_installations'::regclass
                ) THEN
                    ALTER TABLE vault_app_installations
                        ADD CONSTRAINT vault_app_installations_app_vault_key
                        UNIQUE (app_id, vault_id);
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'vault_app_installations_id_vault_key'
                       AND conrelid = 'vault_app_installations'::regclass
                ) THEN
                    ALTER TABLE vault_app_installations
                        ADD CONSTRAINT vault_app_installations_id_vault_key
                        UNIQUE (id, vault_id);
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'vault_app_installations_desired_release_fkey'
                       AND conrelid = 'vault_app_installations'::regclass
                ) THEN
                    ALTER TABLE vault_app_installations
                        ADD CONSTRAINT vault_app_installations_desired_release_fkey
                        FOREIGN KEY (app_id, desired_release_id)
                        REFERENCES app_releases(app_id, id)
                        ON DELETE RESTRICT;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'vault_app_installations_current_release_fkey'
                       AND conrelid = 'vault_app_installations'::regclass
                ) THEN
                    ALTER TABLE vault_app_installations
                        ADD CONSTRAINT vault_app_installations_current_release_fkey
                        FOREIGN KEY (app_id, current_release_id)
                        REFERENCES app_releases(app_id, id)
                        ON DELETE RESTRICT;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'vault_app_installations_lifecycle_check'
                       AND conrelid = 'vault_app_installations'::regclass
                ) THEN
                    ALTER TABLE vault_app_installations
                        ADD CONSTRAINT vault_app_installations_lifecycle_check
                        CHECK (
                            lifecycle IN (
                                'installing',
                                'active',
                                'upgrading',
                                'blocked',
                                'uninstalled'
                            )
                        );
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'vault_app_installations_release_coherence'
                       AND conrelid = 'vault_app_installations'::regclass
                ) THEN
                    ALTER TABLE vault_app_installations
                        ADD CONSTRAINT vault_app_installations_release_coherence
                        CHECK (
                            (
                                lifecycle = 'installing'
                                AND desired_release_id IS NOT NULL
                                AND current_release_id IS NULL
                            )
                            OR (
                                lifecycle = 'active'
                                AND desired_release_id IS NOT NULL
                                AND current_release_id IS NOT NULL
                                AND current_release_id = desired_release_id
                            )
                            OR (
                                lifecycle = 'upgrading'
                                AND desired_release_id IS NOT NULL
                                AND current_release_id IS NOT NULL
                                AND current_release_id <> desired_release_id
                            )
                            OR lifecycle = 'blocked'
                            OR (
                                lifecycle = 'uninstalled'
                                AND desired_release_id IS NULL
                            )
                        );
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'vault_app_installations_blocked_reason_check'
                       AND conrelid = 'vault_app_installations'::regclass
                ) THEN
                    ALTER TABLE vault_app_installations
                        ADD CONSTRAINT vault_app_installations_blocked_reason_check
                        CHECK (
                            (
                                lifecycle = 'blocked'
                                AND NULLIF(btrim(blocked_reason), '') IS NOT NULL
                            )
                            OR (
                                lifecycle <> 'blocked'
                                AND blocked_reason IS NULL
                            )
                        );
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'vault_app_installations_grant_generation_check'
                       AND conrelid = 'vault_app_installations'::regclass
                ) THEN
                    ALTER TABLE vault_app_installations
                        ADD CONSTRAINT vault_app_installations_grant_generation_check
                        CHECK (grant_generation >= 0);
                END IF;
            END
            $$;

            CREATE TABLE IF NOT EXISTS installation_grants (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                installation_id UUID NOT NULL,
                generation BIGINT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                capabilities TEXT[] NOT NULL,
                issuer TEXT NOT NULL,
                provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
                issued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                revoked_at TIMESTAMPTZ
            );

            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'installation_grants_installation_id_fkey'
                       AND conrelid = 'installation_grants'::regclass
                ) OR EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'installation_grants_installation_id_fkey'
                       AND conrelid = 'installation_grants'::regclass
                       AND confdeltype <> 'c'
                ) THEN
                    ALTER TABLE installation_grants
                        DROP CONSTRAINT IF EXISTS
                        installation_grants_installation_id_fkey;
                    ALTER TABLE installation_grants
                        ADD CONSTRAINT installation_grants_installation_id_fkey
                        FOREIGN KEY (installation_id)
                        REFERENCES vault_app_installations(id)
                        ON DELETE CASCADE;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'installation_grants_generation_key'
                       AND conrelid = 'installation_grants'::regclass
                ) THEN
                    ALTER TABLE installation_grants
                        ADD CONSTRAINT installation_grants_generation_key
                        UNIQUE (installation_id, generation);
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'installation_grants_generation_check'
                       AND conrelid = 'installation_grants'::regclass
                ) THEN
                    ALTER TABLE installation_grants
                        ADD CONSTRAINT installation_grants_generation_check
                        CHECK (generation > 0);
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'installation_grants_status_check'
                       AND conrelid = 'installation_grants'::regclass
                ) THEN
                    ALTER TABLE installation_grants
                        ADD CONSTRAINT installation_grants_status_check
                        CHECK (status IN ('active', 'revoked'));
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'installation_grants_capabilities_check'
                       AND conrelid = 'installation_grants'::regclass
                ) THEN
                    ALTER TABLE installation_grants
                        ADD CONSTRAINT installation_grants_capabilities_check
                        CHECK (
                            cardinality(capabilities) > 0
                            AND NOT capabilities @> ARRAY['']::text[]
                        );
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'installation_grants_issuer_nonempty'
                       AND conrelid = 'installation_grants'::regclass
                ) THEN
                    ALTER TABLE installation_grants
                        ADD CONSTRAINT installation_grants_issuer_nonempty
                        CHECK (btrim(issuer) <> '');
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'installation_grants_revocation_coherence'
                       AND conrelid = 'installation_grants'::regclass
                ) THEN
                    ALTER TABLE installation_grants
                        ADD CONSTRAINT installation_grants_revocation_coherence
                        CHECK (
                            (status = 'active' AND revoked_at IS NULL)
                            OR (status = 'revoked' AND revoked_at IS NOT NULL)
                        );
                END IF;
            END
            $$;

            CREATE UNIQUE INDEX IF NOT EXISTS
                installation_grants_one_active_idx
                ON installation_grants(installation_id)
                WHERE status = 'active';

            CREATE TABLE IF NOT EXISTS app_owned_resources (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                installation_id UUID NOT NULL,
                vault_id UUID,
                resource_kind TEXT NOT NULL,
                resource_key TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'owned',
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            ALTER TABLE app_owned_resources
                ADD COLUMN IF NOT EXISTS vault_id UUID;

            UPDATE app_owned_resources AS resource
               SET vault_id = installation.vault_id
              FROM vault_app_installations AS installation
             WHERE resource.installation_id = installation.id
               AND resource.vault_id IS NULL;

            ALTER TABLE app_owned_resources
                ALTER COLUMN vault_id SET NOT NULL;

            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_owned_resources_installation_id_fkey'
                       AND conrelid = 'app_owned_resources'::regclass
                ) THEN
                    ALTER TABLE app_owned_resources
                        DROP CONSTRAINT
                        app_owned_resources_installation_id_fkey;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_owned_resources_installation_vault_fkey'
                       AND conrelid = 'app_owned_resources'::regclass
                ) THEN
                    ALTER TABLE app_owned_resources
                        ADD CONSTRAINT
                        app_owned_resources_installation_vault_fkey
                        FOREIGN KEY (installation_id, vault_id)
                        REFERENCES vault_app_installations(id, vault_id)
                        ON DELETE CASCADE;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_owned_resources_identity_key'
                       AND conrelid = 'app_owned_resources'::regclass
                ) THEN
                    ALTER TABLE app_owned_resources
                        ADD CONSTRAINT app_owned_resources_identity_key
                        UNIQUE (installation_id, resource_kind, resource_key);
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_owned_resources_vault_identity_key'
                       AND conrelid = 'app_owned_resources'::regclass
                ) THEN
                    ALTER TABLE app_owned_resources
                        ADD CONSTRAINT app_owned_resources_vault_identity_key
                        UNIQUE (vault_id, resource_kind, resource_key);
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_owned_resources_kind_nonempty'
                       AND conrelid = 'app_owned_resources'::regclass
                ) THEN
                    ALTER TABLE app_owned_resources
                        ADD CONSTRAINT app_owned_resources_kind_nonempty
                        CHECK (btrim(resource_kind) <> '');
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_owned_resources_key_nonempty'
                       AND conrelid = 'app_owned_resources'::regclass
                ) THEN
                    ALTER TABLE app_owned_resources
                        ADD CONSTRAINT app_owned_resources_key_nonempty
                        CHECK (btrim(resource_key) <> '');
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_owned_resources_status_check'
                       AND conrelid = 'app_owned_resources'::regclass
                ) THEN
                    ALTER TABLE app_owned_resources
                        ADD CONSTRAINT app_owned_resources_status_check
                        CHECK (status IN ('owned', 'retained'));
                END IF;
            END
            $$;

            CREATE INDEX IF NOT EXISTS app_releases_app_registered_idx
                ON app_releases(app_id, registered_at DESC);
            CREATE INDEX IF NOT EXISTS vault_app_installations_vault_idx
                ON vault_app_installations(vault_id, app_id);
            CREATE INDEX IF NOT EXISTS installation_grants_latest_idx
                ON installation_grants(installation_id, generation DESC);
            CREATE INDEX IF NOT EXISTS app_owned_resources_installation_status_idx
                ON app_owned_resources(installation_id, status, resource_kind);
            """
        )

        await conn.execute(
            """
            CREATE OR REPLACE FUNCTION akb_registry_set_updated_at()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            BEGIN
                NEW.updated_at = NOW();
                RETURN NEW;
            END;
            $$;

            DROP TRIGGER IF EXISTS app_definitions_set_updated_at
                ON app_definitions;
            CREATE TRIGGER app_definitions_set_updated_at
                BEFORE UPDATE ON app_definitions
                FOR EACH ROW EXECUTE FUNCTION akb_registry_set_updated_at();

            DROP TRIGGER IF EXISTS vault_app_installations_set_updated_at
                ON vault_app_installations;
            CREATE TRIGGER vault_app_installations_set_updated_at
                BEFORE UPDATE ON vault_app_installations
                FOR EACH ROW EXECUTE FUNCTION akb_registry_set_updated_at();

            DROP TRIGGER IF EXISTS app_owned_resources_set_updated_at
                ON app_owned_resources;
            CREATE TRIGGER app_owned_resources_set_updated_at
                BEFORE UPDATE ON app_owned_resources
                FOR EACH ROW EXECUTE FUNCTION akb_registry_set_updated_at();
            """
        )

        await conn.execute(
            """
            CREATE OR REPLACE FUNCTION akb_guard_app_definition_identity()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF NEW.app_key IS DISTINCT FROM OLD.app_key THEN
                    RAISE EXCEPTION
                        'app identity is immutable; app_key cannot be changed'
                        USING ERRCODE = '55000';
                END IF;
                RETURN NEW;
            END;
            $$;

            DROP TRIGGER IF EXISTS app_definitions_identity_immutable
                ON app_definitions;
            CREATE TRIGGER app_definitions_identity_immutable
                BEFORE UPDATE ON app_definitions
                FOR EACH ROW
                EXECUTE FUNCTION akb_guard_app_definition_identity();

            CREATE OR REPLACE FUNCTION akb_guard_installation_mutation()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    IF EXISTS (
                        SELECT 1 FROM vaults WHERE id = OLD.vault_id
                    ) THEN
                        RAISE EXCEPTION
                            'installation rows are retained; uninstall through lifecycle state'
                            USING ERRCODE = '55000';
                    END IF;
                    RETURN OLD;
                END IF;

                IF NEW.app_id IS DISTINCT FROM OLD.app_id
                   OR NEW.vault_id IS DISTINCT FROM OLD.vault_id
                THEN
                    RAISE EXCEPTION
                        'installation app and vault identity are immutable'
                        USING ERRCODE = '55000';
                END IF;
                RETURN NEW;
            END;
            $$;

            DROP TRIGGER IF EXISTS vault_app_installations_identity_immutable
                ON vault_app_installations;
            CREATE TRIGGER vault_app_installations_identity_immutable
                BEFORE UPDATE OR DELETE ON vault_app_installations
                FOR EACH ROW
                EXECUTE FUNCTION akb_guard_installation_mutation();

            CREATE OR REPLACE FUNCTION akb_set_owned_resource_vault()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            DECLARE
                installation_vault_id UUID;
            BEGIN
                SELECT vault_id
                  INTO installation_vault_id
                  FROM vault_app_installations
                 WHERE id = NEW.installation_id;

                IF installation_vault_id IS NULL THEN
                    RAISE EXCEPTION 'installation does not exist'
                        USING ERRCODE = '23503';
                END IF;
                IF NEW.vault_id IS NULL THEN
                    NEW.vault_id = installation_vault_id;
                ELSIF NEW.vault_id IS DISTINCT FROM installation_vault_id THEN
                    RAISE EXCEPTION
                        'owned resource vault must match its installation vault'
                        USING ERRCODE = '23503';
                END IF;
                RETURN NEW;
            END;
            $$;

            DROP TRIGGER IF EXISTS app_owned_resources_set_vault
                ON app_owned_resources;
            CREATE TRIGGER app_owned_resources_set_vault
                BEFORE INSERT ON app_owned_resources
                FOR EACH ROW EXECUTE FUNCTION akb_set_owned_resource_vault();

            CREATE OR REPLACE FUNCTION akb_guard_owned_resource_mutation()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    IF EXISTS (
                        SELECT 1
                          FROM vault_app_installations
                         WHERE id = OLD.installation_id
                    ) THEN
                        RAISE EXCEPTION
                            'owned resource rows are retained; update ownership status instead'
                            USING ERRCODE = '55000';
                    END IF;
                    RETURN OLD;
                END IF;

                IF NEW.installation_id IS DISTINCT FROM OLD.installation_id
                   OR NEW.vault_id IS DISTINCT FROM OLD.vault_id
                   OR NEW.resource_kind IS DISTINCT FROM OLD.resource_kind
                   OR NEW.resource_key IS DISTINCT FROM OLD.resource_key
                THEN
                    RAISE EXCEPTION
                        'owned resource installation, vault, kind, and key are immutable'
                        USING ERRCODE = '55000';
                END IF;
                RETURN NEW;
            END;
            $$;

            DROP TRIGGER IF EXISTS app_owned_resources_identity_immutable
                ON app_owned_resources;
            CREATE TRIGGER app_owned_resources_identity_immutable
                BEFORE UPDATE OR DELETE ON app_owned_resources
                FOR EACH ROW
                EXECUTE FUNCTION akb_guard_owned_resource_mutation();
            """
        )

        await conn.execute(
            """
            CREATE OR REPLACE FUNCTION akb_reject_release_mutation()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION
                    'registered app releases are immutable; register a new release'
                    USING ERRCODE = '55000';
            END;
            $$;

            DROP TRIGGER IF EXISTS app_releases_immutable
                ON app_releases;
            CREATE TRIGGER app_releases_immutable
                BEFORE UPDATE OR DELETE ON app_releases
                FOR EACH ROW EXECUTE FUNCTION akb_reject_release_mutation();
            """
        )

        await conn.execute(
            """
            CREATE OR REPLACE FUNCTION akb_check_grant_generation()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            DECLARE
                advanced_id UUID;
            BEGIN
                IF NEW.status <> 'active' OR NEW.revoked_at IS NOT NULL THEN
                    RAISE EXCEPTION
                        'new installation grants must start active and not revoked'
                        USING ERRCODE = '23514';
                END IF;

                UPDATE vault_app_installations
                   SET grant_generation = NEW.generation
                 WHERE id = NEW.installation_id
                   AND grant_generation + 1 = NEW.generation
                RETURNING id INTO advanced_id;

                IF advanced_id IS NULL THEN
                    IF NOT EXISTS (
                        SELECT 1 FROM vault_app_installations
                         WHERE id = NEW.installation_id
                    ) THEN
                        RAISE EXCEPTION 'installation does not exist'
                            USING ERRCODE = '23503';
                    END IF;
                    RAISE EXCEPTION
                        'grant generation must be the installation next generation'
                        USING ERRCODE = '23514';
                END IF;

                RETURN NEW;
            END;
            $$;

            DROP TRIGGER IF EXISTS installation_grants_generation_guard
                ON installation_grants;
            CREATE TRIGGER installation_grants_generation_guard
                BEFORE INSERT ON installation_grants
                FOR EACH ROW EXECUTE FUNCTION akb_check_grant_generation();
            """
        )

        await conn.execute(
            """
            CREATE OR REPLACE FUNCTION akb_guard_grant_mutation()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    IF EXISTS (
                        SELECT 1
                          FROM vault_app_installations
                         WHERE id = OLD.installation_id
                    ) THEN
                        RAISE EXCEPTION
                            'installation grants are retained as immutable generations'
                            USING ERRCODE = '55000';
                    END IF;
                    RETURN OLD;
                END IF;

                IF OLD.status = 'active'
                   AND NEW.status = 'revoked'
                   AND NEW.revoked_at IS NOT NULL
                   AND NEW.installation_id = OLD.installation_id
                   AND NEW.generation = OLD.generation
                   AND NEW.capabilities = OLD.capabilities
                   AND NEW.issuer = OLD.issuer
                   AND NEW.provenance = OLD.provenance
                   AND NEW.issued_at = OLD.issued_at
                THEN
                    RETURN NEW;
                END IF;

                RAISE EXCEPTION
                    'grant identity, capabilities, issuer, and provenance are immutable; only active to revoked is allowed'
                    USING ERRCODE = '55000';
            END;
            $$;

            DROP TRIGGER IF EXISTS installation_grants_immutable
                ON installation_grants;
            CREATE TRIGGER installation_grants_immutable
                BEFORE UPDATE OR DELETE ON installation_grants
                FOR EACH ROW EXECUTE FUNCTION akb_guard_grant_mutation();
            """
        )

        await conn.execute(
            """
            CREATE OR REPLACE VIEW app_installation_registry
            WITH (security_invoker = true)
            AS
            SELECT
                installation.id AS installation_id,
                installation.app_id,
                app.app_key,
                installation.vault_id,
                vault.name AS vault_name,
                installation.lifecycle,
                installation.blocked_reason,
                installation.desired_release_id,
                desired.version AS desired_version,
                installation.current_release_id,
                current_release.version AS current_version,
                installation.grant_generation,
                latest_grant.generation AS latest_grant_generation,
                latest_grant.status AS latest_grant_status,
                latest_grant.capabilities AS latest_grant_capabilities,
                latest_grant.issuer AS latest_grant_issuer,
                latest_grant.provenance AS latest_grant_provenance,
                latest_grant.issued_at AS latest_grant_issued_at,
                latest_grant.revoked_at AS latest_grant_revoked_at,
                COALESCE(resources.items, '[]'::jsonb) AS resources,
                installation.created_at,
                installation.updated_at
            FROM vault_app_installations AS installation
            JOIN app_definitions AS app
              ON app.id = installation.app_id
            JOIN vaults AS vault
              ON vault.id = installation.vault_id
            LEFT JOIN app_releases AS desired
              ON desired.id = installation.desired_release_id
            LEFT JOIN app_releases AS current_release
              ON current_release.id = installation.current_release_id
            LEFT JOIN LATERAL (
                SELECT
                    grant_row.generation,
                    grant_row.status,
                    grant_row.capabilities,
                    grant_row.issuer,
                    grant_row.provenance,
                    grant_row.issued_at,
                    grant_row.revoked_at
                FROM installation_grants AS grant_row
                WHERE grant_row.installation_id = installation.id
                ORDER BY grant_row.generation DESC
                LIMIT 1
            ) AS latest_grant ON TRUE
            LEFT JOIN LATERAL (
                SELECT jsonb_agg(
                    jsonb_build_object(
                        'kind', resource.resource_kind,
                        'key', resource.resource_key,
                        'status', resource.status,
                        'metadata', resource.metadata
                    )
                    ORDER BY resource.resource_kind, resource.resource_key
                ) AS items
                FROM app_owned_resources AS resource
                WHERE resource.installation_id = installation.id
            ) AS resources ON TRUE;
            """
        )

        await conn.execute(
            """
            REVOKE ALL PRIVILEGES ON TABLE
                app_definitions,
                app_releases,
                vault_app_installations,
                installation_grants,
                app_owned_resources,
                app_installation_registry
            FROM PUBLIC;

            REVOKE EXECUTE ON FUNCTION akb_registry_set_updated_at()
                FROM PUBLIC;
            REVOKE EXECUTE ON FUNCTION akb_guard_app_definition_identity()
                FROM PUBLIC;
            REVOKE EXECUTE ON FUNCTION akb_guard_installation_mutation()
                FROM PUBLIC;
            REVOKE EXECUTE ON FUNCTION akb_set_owned_resource_vault()
                FROM PUBLIC;
            REVOKE EXECUTE ON FUNCTION akb_guard_owned_resource_mutation()
                FROM PUBLIC;
            REVOKE EXECUTE ON FUNCTION akb_reject_release_mutation()
                FROM PUBLIC;
            REVOKE EXECUTE ON FUNCTION akb_check_grant_generation()
                FROM PUBLIC;
            REVOKE EXECUTE ON FUNCTION akb_guard_grant_mutation()
                FROM PUBLIC;
            """
        )

    logger.info("Migration 047 added the app desired-state registry")


async def _main():
    from app.db.postgres import close_pool, init_db

    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
