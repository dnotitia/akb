"""Add exact-bound, short-lived opaque sessions for the SSO admin surface."""

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
            CREATE TABLE IF NOT EXISTS admin_browser_sessions (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
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
                expires_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT admin_browser_session_external_user_fk
                    FOREIGN KEY (external_identity_id, user_id)
                    REFERENCES external_identities(id, user_id) ON DELETE CASCADE,
                CONSTRAINT admin_browser_session_positive_lifetime
                    CHECK (expires_at > created_at)
            )
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_admin_browser_sessions_expiry
                ON admin_browser_sessions(expires_at)
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_admin_browser_sessions_user
                ON admin_browser_sessions(user_id)
            """
        )
