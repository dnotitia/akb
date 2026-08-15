"""Allow one local and one Keycloak recovery admin during auth handover."""

from __future__ import annotations


async def migrate(conn) -> None:
    async with conn.transaction():
        await conn.execute("DROP INDEX IF EXISTS users_one_recovery_admin")
        await conn.execute(
            """
            DO $$
            BEGIN
              IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                 WHERE conname = 'users_recovery_admin_provider_check'
                   AND conrelid = 'users'::regclass
              ) THEN
                ALTER TABLE users
                  ADD CONSTRAINT users_recovery_admin_provider_check
                  CHECK (
                    NOT is_recovery_admin
                    OR auth_provider IN ('local', 'keycloak')
                  );
              END IF;
            END $$;
            """
        )
        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS users_one_recovery_admin_per_provider
                ON users (auth_provider)
                WHERE is_recovery_admin
            """
        )
