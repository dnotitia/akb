"""Personal inbox and independent transactional delivery work."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS notification_work (
 id BIGSERIAL PRIMARY KEY, source_key TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
 vault_id UUID, resource_id UUID, actor_id TEXT, recipient_ids UUID[] NOT NULL DEFAULT '{}',
 payload JSONB NOT NULL DEFAULT '{}', created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 lease_until TIMESTAMPTZ, attempts INT NOT NULL DEFAULT 0, available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 processed_at TIMESTAMPTZ, failed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS notification_work_pending ON notification_work(available_at,id)
 WHERE processed_at IS NULL AND failed_at IS NULL;
CREATE TABLE IF NOT EXISTS notification_inboxes (
 user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, version BIGINT NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS user_notifications (
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(), user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 kind TEXT NOT NULL, vault_id UUID, resource_id UUID, group_key TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 version BIGINT NOT NULL, read_version BIGINT NOT NULL DEFAULT 0,
 UNIQUE(user_id,group_key)
);
CREATE INDEX IF NOT EXISTS user_notifications_page ON user_notifications(user_id,version DESC,id);
CREATE TABLE IF NOT EXISTS notification_deliveries (
 source_key TEXT NOT NULL, user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(source_key,user_id)
);
CREATE TABLE IF NOT EXISTS notification_subscriptions (
 user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 resource_id UUID NOT NULL, vault_id UUID NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(user_id,resource_id)
);
CREATE INDEX IF NOT EXISTS notification_subscriptions_resource ON notification_subscriptions(resource_id);
"""


async def migrate(conn=None):
    if conn is None:
        from app.db.postgres import get_pool
        pool = await get_pool()
        async with pool.acquire() as connection:
            await connection.execute(SCHEMA)
    else:
        await conn.execute(SCHEMA)
