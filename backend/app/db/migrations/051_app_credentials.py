"""Migration 051: exchange-only app credentials.

Credentials belong to an app deployment, never to a user or Vault membership.
Only a one-way proof hash and non-secret lifecycle metadata are persisted.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("akb.migration.051")


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
            CREATE TABLE IF NOT EXISTS app_credentials (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                app_id UUID NOT NULL,
                deployment TEXT NOT NULL,
                generation BIGINT NOT NULL,
                credential_hash TEXT NOT NULL,
                credential_prefix TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                expires_at TIMESTAMPTZ,
                overlap_until TIMESTAMPTZ,
                revoked_at TIMESTAMPTZ,
                last_exchanged_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_credentials_app_id_fkey'
                       AND conrelid = 'app_credentials'::regclass
                ) THEN
                    ALTER TABLE app_credentials
                        ADD CONSTRAINT app_credentials_app_id_fkey
                        FOREIGN KEY (app_id) REFERENCES app_definitions(id)
                        ON DELETE RESTRICT;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_credentials_deployment_nonempty'
                       AND conrelid = 'app_credentials'::regclass
                ) THEN
                    ALTER TABLE app_credentials
                        ADD CONSTRAINT app_credentials_deployment_nonempty
                        CHECK (btrim(deployment) <> '');
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_credentials_generation_positive'
                       AND conrelid = 'app_credentials'::regclass
                ) THEN
                    ALTER TABLE app_credentials
                        ADD CONSTRAINT app_credentials_generation_positive
                        CHECK (generation > 0);
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_credentials_hash_shape'
                       AND conrelid = 'app_credentials'::regclass
                ) THEN
                    ALTER TABLE app_credentials
                        ADD CONSTRAINT app_credentials_hash_shape
                        CHECK (credential_hash ~ '^[0-9a-f]{64}$');
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_credentials_prefix_nonempty'
                       AND conrelid = 'app_credentials'::regclass
                ) THEN
                    ALTER TABLE app_credentials
                        ADD CONSTRAINT app_credentials_prefix_nonempty
                        CHECK (btrim(credential_prefix) <> '');
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_credentials_status_check'
                       AND conrelid = 'app_credentials'::regclass
                ) THEN
                    ALTER TABLE app_credentials
                        ADD CONSTRAINT app_credentials_status_check
                        CHECK (status IN ('active', 'rotated', 'revoked'));
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_credentials_lifecycle_coherence'
                       AND conrelid = 'app_credentials'::regclass
                ) THEN
                    ALTER TABLE app_credentials
                        ADD CONSTRAINT app_credentials_lifecycle_coherence
                        CHECK (
                            (
                                status = 'active'
                                AND overlap_until IS NULL
                                AND revoked_at IS NULL
                            )
                            OR (
                                status = 'rotated'
                                AND overlap_until IS NOT NULL
                                AND revoked_at IS NULL
                            )
                            OR (
                                status = 'revoked'
                                AND overlap_until IS NULL
                                AND revoked_at IS NOT NULL
                            )
                        );
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_credentials_app_deployment_generation_key'
                       AND conrelid = 'app_credentials'::regclass
                ) THEN
                    ALTER TABLE app_credentials
                        ADD CONSTRAINT app_credentials_app_deployment_generation_key
                        UNIQUE (app_id, deployment, generation);
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_credentials_hash_key'
                       AND conrelid = 'app_credentials'::regclass
                ) THEN
                    ALTER TABLE app_credentials
                        ADD CONSTRAINT app_credentials_hash_key
                        UNIQUE (credential_hash);
                END IF;
            END
            $$;

            CREATE UNIQUE INDEX IF NOT EXISTS app_credentials_one_active_idx
                ON app_credentials(app_id, deployment)
                WHERE status = 'active';

            CREATE UNIQUE INDEX IF NOT EXISTS app_credentials_one_overlap_idx
                ON app_credentials(app_id, deployment)
                WHERE status = 'rotated';

            CREATE INDEX IF NOT EXISTS app_credentials_lookup_idx
                ON app_credentials(credential_hash);

            CREATE INDEX IF NOT EXISTS app_credentials_list_idx
                ON app_credentials(app_id, deployment, generation DESC);

            CREATE OR REPLACE FUNCTION akb_app_credential_set_updated_at()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            BEGIN
                NEW.updated_at = NOW();
                RETURN NEW;
            END;
            $$;

            DROP TRIGGER IF EXISTS app_credentials_set_updated_at
                ON app_credentials;
            CREATE TRIGGER app_credentials_set_updated_at
                BEFORE UPDATE ON app_credentials
                FOR EACH ROW
                EXECUTE FUNCTION akb_app_credential_set_updated_at();

            REVOKE ALL PRIVILEGES ON TABLE app_credentials FROM PUBLIC;
            REVOKE EXECUTE ON FUNCTION akb_app_credential_set_updated_at()
                FROM PUBLIC;
            """
        )

    logger.info("Migration 051 added exchange-only app credentials")


async def _main():
    from app.db.postgres import close_pool, init_db

    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
