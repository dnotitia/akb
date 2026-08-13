"""Add the rollback-compatible SSO runtime-generation boundary.

This is an explicit ``stop-the-world-v1`` bridge, not a rolling mixed-writer
cutover.  Existing tables gain nullable epoch columns so the pre-epoch image
can still be used after a prepared rollback.  Startup later purges all legacy
rows and changes the machine-readable upgrade state to ``enforced``; database
triggers then reject every legacy NULL write.  Migration itself never deletes
browser authority and never makes the bridge columns NOT NULL.
"""

from __future__ import annotations


async def migrate(conn) -> None:
    async with conn.transaction():
        # Multiple replicas may all observe migration 076 as pending. Serialize
        # this idempotent DDL before either one acquires catalog/relation locks;
        # reconciliation then shares the explicit authority-first table order.
        await conn.execute(
            """
            SELECT pg_advisory_xact_lock(
                hashtextextended('akb-migration-076-sso-session-epoch', 0)
            )
            """
        )
        pre_epoch_schema = await conn.fetchval(
            """
            SELECT COUNT(*) < 3
              FROM information_schema.columns
             WHERE table_schema = 'public'
               AND column_name = 'session_epoch'
               AND table_name = ANY($1::TEXT[])
            """,
            [
                "admin_browser_sessions",
                "sso_browser_sessions",
                "sso_browser_logout_fences",
            ],
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_runtime_state (
                singleton BOOLEAN PRIMARY KEY DEFAULT TRUE
                    CHECK (singleton),
                runtime_generation BIGINT NOT NULL
                    CHECK (runtime_generation > 0),
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
            CREATE TABLE IF NOT EXISTS auth_runtime_epoch_upgrade (
                singleton BOOLEAN PRIMARY KEY DEFAULT TRUE
                    CHECK (singleton),
                state TEXT NOT NULL
                    CHECK (
                        state IN (
                            'ready', 'required', 'enforced', 'rollback_ready'
                        )
                    ),
                runtime_generation_floor BIGINT NOT NULL DEFAULT 0
                    CHECK (runtime_generation_floor >= 0),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        await conn.execute(
            """
            INSERT INTO auth_runtime_epoch_upgrade (singleton, state)
            VALUES (TRUE, 'ready')
            ON CONFLICT (singleton) DO NOTHING
            """
        )

        # Every migration/reconciliation path takes relations in this order.
        # The authority relation is acquired before any session relation, so a
        # request already holding the authority row can finish before DDL waits
        # on a session table. Whole-migration deadlock retry lives in the
        # migration runner, where transaction rollback makes replay sound.
        await conn.execute("LOCK TABLE auth_runtime_state IN ACCESS EXCLUSIVE MODE")
        await conn.execute(
            """
            ALTER TABLE auth_runtime_state
                ADD COLUMN IF NOT EXISTS runtime_generation BIGINT
            """
        )
        # Development databases that ran the unmerged pre-review migration may
        # already have this singleton without a generation. Zero is a dormant
        # migration sentinel: current config is strictly positive, so startup
        # must advance and purge rather than accepting the old state as exact.
        await conn.execute(
            """
            UPDATE auth_runtime_state
               SET runtime_generation = 0
             WHERE runtime_generation IS NULL
            """
        )
        await conn.execute(
            """
            ALTER TABLE auth_runtime_state
                ALTER COLUMN runtime_generation SET NOT NULL
            """
        )
        generation_check_exists = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                  FROM pg_constraint
                 WHERE conname = 'auth_runtime_state_runtime_generation_check'
                   AND conrelid = 'auth_runtime_state'::regclass
            )
            """
        )
        if not generation_check_exists:
            await conn.execute(
                """
                ALTER TABLE auth_runtime_state
                    ADD CONSTRAINT auth_runtime_state_runtime_generation_check
                    CHECK (runtime_generation >= 0)
                """
            )
        await conn.execute(
            """
            ALTER TABLE auth_runtime_epoch_upgrade
                ADD COLUMN IF NOT EXISTS runtime_generation_floor BIGINT
                NOT NULL DEFAULT 0
            """
        )
        await conn.execute("LOCK TABLE admin_browser_sessions IN ACCESS EXCLUSIVE MODE")
        await conn.execute("LOCK TABLE sso_browser_sessions IN ACCESS EXCLUSIVE MODE")
        await conn.execute("LOCK TABLE sso_browser_logout_fences IN ACCESS EXCLUSIVE MODE")

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

        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                auth_runtime_state_sso_session_epoch_key
                ON auth_runtime_state(sso_session_epoch)
            """
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

        # Preserve the pre-epoch conflict target for a prepared rollback.
        # Current code includes session_epoch in values and predicates, while
        # the post-cutover trigger prevents a legacy NULL from occupying it.
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
                PRIMARY KEY (identity_issuer, keycloak_sid)
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

        await conn.execute(
            """
            CREATE OR REPLACE FUNCTION reject_legacy_sso_session_epoch()
            RETURNS TRIGGER AS $$
            BEGIN
                IF NEW.session_epoch IS NULL AND EXISTS (
                    SELECT 1
                      FROM auth_runtime_epoch_upgrade
                     WHERE singleton = TRUE AND state = 'enforced'
                ) THEN
                    RAISE EXCEPTION 'legacy SSO session writes are disabled'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        for table in (
            "admin_browser_sessions",
            "sso_browser_sessions",
            "sso_browser_logout_fences",
        ):
            trigger = f"{table}_epoch_guard"
            await conn.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
            await conn.execute(
                f"""
                CREATE TRIGGER {trigger}
                BEFORE INSERT OR UPDATE OF session_epoch ON {table}
                FOR EACH ROW EXECUTE FUNCTION reject_legacy_sso_session_epoch()
                """
            )

        if pre_epoch_schema:
            await conn.execute(
                """
                UPDATE auth_runtime_epoch_upgrade
                   SET state = 'required', updated_at = NOW()
                 WHERE singleton = TRUE AND state = 'ready'
                """
            )
