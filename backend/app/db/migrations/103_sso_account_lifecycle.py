"""Fence SSO browser callbacks and persist managed-account sync status."""

SQL = """
CREATE SEQUENCE IF NOT EXISTS sso_browser_login_sequence AS BIGINT;
CREATE TABLE IF NOT EXISTS sso_browser_user_revocations (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    login_sequence BIGINT NOT NULL CHECK (login_sequence > 0),
    revoked_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS sso_account_sync_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK(singleton),
    last_completed_at TIMESTAMPTZ,
    last_error_code TEXT,
    checked INTEGER NOT NULL DEFAULT 0,
    suspended INTEGER NOT NULL DEFAULT 0
);
"""


async def migrate(conn):
    await conn.execute(SQL)
