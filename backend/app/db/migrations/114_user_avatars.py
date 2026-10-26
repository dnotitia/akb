"""Migration 114: private account photo state, tombstone revisions and budget."""

DDL = """
CREATE TABLE IF NOT EXISTS user_avatars (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    revision UUID NOT NULL,
    object_key TEXT UNIQUE,
    content_type TEXT,
    size_bytes INTEGER,
    width INTEGER,
    height INTEGER,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK ((object_key IS NULL AND content_type IS NULL AND size_bytes IS NULL
            AND width IS NULL AND height IS NULL) OR
           (object_key IS NOT NULL AND content_type IS NOT NULL AND size_bytes IS NOT NULL
            AND width IS NOT NULL AND height IS NOT NULL
            AND object_key LIKE '.akb-account-assets/avatars/%' AND content_type='image/webp'
            AND size_bytes BETWEEN 1 AND 262144 AND width=512 AND height=512))
);
CREATE TABLE IF NOT EXISTS user_avatar_write_budget (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    window_start TIMESTAMPTZ NOT NULL,
    attempts INTEGER NOT NULL CHECK (attempts > 0)
);
"""


async def migrate(conn):
    await conn.execute(DDL)
