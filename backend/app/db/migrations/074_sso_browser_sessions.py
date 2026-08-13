"""Add encrypted, exact-bound ordinary-user SSO browser sessions."""

from __future__ import annotations


async def migrate(conn) -> None:
    async with conn.transaction():
        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS external_identities_id_user_key
                ON external_identities(id, user_id)
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sso_browser_sessions (
                id UUID PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE
                    CHECK (token_hash ~ '^[0-9a-f]{64}$'),
                csrf_token_hash TEXT NOT NULL
                    CHECK (csrf_token_hash ~ '^[0-9a-f]{64}$'),
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                external_identity_id UUID NOT NULL,
                identity_issuer TEXT NOT NULL
                    CHECK (char_length(identity_issuer) BETWEEN 1 AND 2048),
                identity_subject TEXT NOT NULL
                    CHECK (char_length(identity_subject) BETWEEN 1 AND 1024),
                keycloak_sid TEXT NOT NULL
                    CHECK (char_length(keycloak_sid) BETWEEN 1 AND 255),
                token_envelope TEXT NOT NULL
                    CHECK (char_length(token_envelope) BETWEEN 32 AND 65536),
                access_expires_at TIMESTAMPTZ NOT NULL,
                refresh_expires_at TIMESTAMPTZ NOT NULL,
                idle_expires_at TIMESTAMPTZ NOT NULL,
                absolute_expires_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT sso_browser_session_external_user_fk
                    FOREIGN KEY (external_identity_id, user_id)
                    REFERENCES external_identities(id, user_id) ON DELETE CASCADE,
                CONSTRAINT sso_browser_session_positive_lifetime
                    CHECK (
                        access_expires_at > created_at
                        AND refresh_expires_at > created_at
                        AND idle_expires_at > created_at
                        AND absolute_expires_at > created_at
                        AND idle_expires_at <= absolute_expires_at
                    )
            )
            """
        )
        for name, columns in (
            ("idx_sso_browser_sessions_idle_expiry", "idle_expires_at"),
            ("idx_sso_browser_sessions_absolute_expiry", "absolute_expires_at"),
            ("idx_sso_browser_sessions_user", "user_id"),
            ("idx_sso_browser_sessions_sid", "identity_issuer, keycloak_sid"),
            (
                "idx_sso_browser_sessions_subject",
                "identity_issuer, identity_subject",
            ),
        ):
            await conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON sso_browser_sessions({columns})")
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sso_browser_logout_fences (
                identity_issuer TEXT NOT NULL
                    CHECK (char_length(identity_issuer) BETWEEN 1 AND 2048),
                keycloak_sid TEXT NOT NULL
                    CHECK (char_length(keycloak_sid) BETWEEN 1 AND 255),
                identity_subject TEXT
                    CHECK (
                        identity_subject IS NULL OR
                        char_length(identity_subject) BETWEEN 1 AND 1024
                    ),
                logout_issued_at TIMESTAMPTZ NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (identity_issuer, keycloak_sid),
                CONSTRAINT sso_browser_logout_fence_positive_lifetime
                    CHECK (expires_at > logout_issued_at)
            )
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_sso_browser_logout_fences_expiry
                ON sso_browser_logout_fences(expires_at)
            """
        )
