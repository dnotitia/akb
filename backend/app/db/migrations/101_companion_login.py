"""Bounded BFF assertion replay protection for common SSO completion."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS companion_login_assertions (
 client_id TEXT NOT NULL, jti TEXT NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
 PRIMARY KEY (client_id,jti)
);
CREATE INDEX IF NOT EXISTS companion_login_assertions_expiry ON companion_login_assertions(expires_at);
"""


async def migrate(conn=None):
    if conn is None:
        from app.db.postgres import get_pool
        pool = await get_pool()
        async with pool.acquire() as connection:
            await connection.execute(SCHEMA)
    else:
        await conn.execute(SCHEMA)
