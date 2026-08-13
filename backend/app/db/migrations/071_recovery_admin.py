"""Add the explicit singleton recovery-administrator designation."""

from __future__ import annotations


async def migrate(conn) -> None:
    async with conn.transaction():
        await conn.execute(
            """
            ALTER TABLE users
              ADD COLUMN IF NOT EXISTS is_recovery_admin BOOLEAN NOT NULL DEFAULT false;
            ALTER TABLE external_identities
              ADD COLUMN IF NOT EXISTS username_snapshot TEXT;
            """
        )
        await conn.execute(
            """
            DO $$
            BEGIN
              IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                 WHERE conname = 'users_recovery_admin_requires_admin'
                   AND conrelid = 'users'::regclass
              ) THEN
                ALTER TABLE users
                  ADD CONSTRAINT users_recovery_admin_requires_admin
                  CHECK (NOT is_recovery_admin OR is_admin);
              END IF;
            END $$;
            """
        )
        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS users_one_recovery_admin
                ON users ((is_recovery_admin))
                WHERE is_recovery_admin
            """
        )
