"""Bind all SSO browser authority to one explicit runtime epoch.

This migration intentionally revokes existing ordinary/admin browser sessions
and logout fences. Rows created before the epoch contract cannot be attributed
to a safe authority generation, so trying to backfill them would recreate the
mode-transition resurrection this migration closes.
"""

from __future__ import annotations


async def migrate(conn) -> None:
    async with conn.transaction():
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_runtime_state (
                singleton BOOLEAN PRIMARY KEY DEFAULT TRUE
                    CHECK (singleton),
                auth_mode TEXT NOT NULL
                    CHECK (auth_mode IN ('local', 'sso')),
                sso_session_epoch UUID,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT auth_runtime_state_sso_session_epoch_key
                    UNIQUE (sso_session_epoch),
                CONSTRAINT auth_runtime_state_epoch_shape
                    CHECK (
                        (auth_mode = 'local' AND sso_session_epoch IS NULL)
                        OR
                        (auth_mode = 'sso' AND sso_session_epoch IS NOT NULL)
                    )
            )
            """
        )
        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                auth_runtime_state_sso_session_epoch_key
                ON auth_runtime_state(sso_session_epoch)
            """
        )
        for table in (
            "admin_browser_sessions",
            "sso_browser_sessions",
            "sso_browser_logout_fences",
        ):
            await conn.execute(
                f"""
                ALTER TABLE {table}
                    ADD COLUMN IF NOT EXISTS session_epoch UUID
                """
            )

        # No pre-epoch row has a trustworthy authority-generation binding.
        # Delete in the same transaction before making the new columns strict.
        await conn.execute("DELETE FROM admin_browser_sessions")
        await conn.execute("DELETE FROM sso_browser_sessions")
        await conn.execute("DELETE FROM sso_browser_logout_fences")
        for table in (
            "admin_browser_sessions",
            "sso_browser_sessions",
            "sso_browser_logout_fences",
        ):
            await conn.execute(
                f"ALTER TABLE {table} ALTER COLUMN session_epoch SET NOT NULL"
            )

        for table, constraint_name in (
            ("admin_browser_sessions", "admin_browser_session_epoch_fk"),
            ("sso_browser_sessions", "sso_browser_session_epoch_fk"),
            (
                "sso_browser_logout_fences",
                "sso_browser_logout_fence_epoch_fk",
            ),
        ):
            exists = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                      FROM pg_constraint
                     WHERE conname = $1
                       AND conrelid = $2::regclass
                )
                """,
                constraint_name,
                table,
            )
            if not exists:
                await conn.execute(
                    f"""
                    ALTER TABLE {table}
                        ADD CONSTRAINT {constraint_name}
                        FOREIGN KEY (session_epoch)
                        REFERENCES auth_runtime_state(sso_session_epoch)
                    """
                )

        await conn.execute(
            """
            ALTER TABLE sso_browser_logout_fences
                DROP CONSTRAINT IF EXISTS sso_browser_logout_fences_pkey
            """
        )
        await conn.execute(
            """
            ALTER TABLE sso_browser_logout_fences
                ADD CONSTRAINT sso_browser_logout_fences_pkey
                PRIMARY KEY (session_epoch, identity_issuer, keycloak_sid)
            """
        )
        await conn.execute("DROP INDEX IF EXISTS idx_sso_browser_sessions_sid")
        await conn.execute(
            """
            CREATE INDEX idx_sso_browser_sessions_sid
                ON sso_browser_sessions(
                    session_epoch, identity_issuer, keycloak_sid
                )
            """
        )
        await conn.execute("DROP INDEX IF EXISTS idx_sso_browser_sessions_subject")
        await conn.execute(
            """
            CREATE INDEX idx_sso_browser_sessions_subject
                ON sso_browser_sessions(
                    session_epoch, identity_issuer, identity_subject
                )
            """
        )
