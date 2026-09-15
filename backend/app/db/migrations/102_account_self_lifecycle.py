"""Deletion cleanup survives the account it cleans; confirmation attempts do not."""
from __future__ import annotations

SQL = """
CREATE TABLE IF NOT EXISTS account_deletion_cleanup (
    role_kind TEXT NOT NULL CHECK(role_kind IN ('user','token')),
    resource_id UUID NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    last_error_code TEXT,
    PRIMARY KEY(role_kind,resource_id)
);
CREATE INDEX IF NOT EXISTS account_deletion_cleanup_pending
    ON account_deletion_cleanup(next_attempt_at) WHERE completed_at IS NULL;
CREATE TABLE IF NOT EXISTS account_deletion_worker_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK(singleton),
    last_seen_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS account_lifecycle_attempts (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    window_start TIMESTAMPTZ NOT NULL,
    attempts INTEGER NOT NULL
);
"""


async def migrate(conn=None):
    if conn is None:
        from app.db.postgres import get_pool
        pool = await get_pool()
        async with pool.acquire() as acquired:
            await acquired.execute(SQL)
    else:
        await conn.execute(SQL)
