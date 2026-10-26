"""Index the live document feed's deterministic newest-first boundary.

ACL and subscription predicates remain in the query before LIMIT. This index
avoids sorting the whole resource catalog; it is not an authorization cache.
The migration runner applies the index and its ledger receipt atomically.
"""

from app.db.postgres import get_pool

DDL = """
CREATE INDEX IF NOT EXISTS idx_native_resources_live_document_recent
    ON native_resources (updated_at DESC, resource_id DESC)
    WHERE surface = 'document' AND lifecycle = 'live';
"""


async def migrate(conn=None):
    if conn is not None:
        await conn.execute(DDL)
        return
    pool = await get_pool()
    async with pool.acquire() as connection:
        await connection.execute(DDL)
