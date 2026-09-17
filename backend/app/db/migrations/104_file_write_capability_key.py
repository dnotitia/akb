"""Name the one object key a write capability grants."""

from __future__ import annotations

SCHEMA = """
-- A write capability authorizes exactly one PUT to exactly one key. Storing
-- that key is the direct statement of what the token grants, and it is the
-- only thing that keeps a capability issued for a staging key from being
-- redeemed against a live one.
--
-- Nullable because the measurement lane's PUT intents carry their bytes in
-- `body` and address no object store at all. A resolver that needs a key
-- refuses a row without one rather than inferring it.
ALTER TABLE m1_file_transfer_intents ADD COLUMN IF NOT EXISTS object_key TEXT;
"""


async def migrate(conn=None):
    if conn is None:
        from app.db.postgres import get_pool
        pool = await get_pool()
        async with pool.acquire() as connection:
            await connection.execute(SCHEMA)
    else:
        await conn.execute(SCHEMA)
