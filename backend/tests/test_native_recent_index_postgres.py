"""The recent index is installed by normal migrations, ordered and idempotent."""

import pytest

from app.db.postgres import _load_migration
from tests.test_event_tail_postgres import _fresh_database

pytestmark = pytest.mark.asyncio


async def test_recent_index_matches_live_document_keyset_and_is_idempotent():
    async with _fresh_database() as pool, pool.acquire() as conn:
        migration = _load_migration("116_native_recent_order.py")
        assert migration is not None, "Recent feed order migration must be installed"
        await migration.migrate(conn)
        await migration.migrate(conn)
        definition = await conn.fetchval("""
            SELECT pg_get_indexdef(indexrelid) FROM pg_index
            WHERE indexrelid = 'idx_native_resources_live_document_recent'::regclass
        """)
        assert "(updated_at DESC, resource_id DESC)" in definition
        assert "surface = 'document'::text" in definition
        assert "lifecycle = 'live'::text" in definition
        assert await conn.fetchval("""
            SELECT count(*) FROM schema_migrations WHERE filename='116_native_recent_order.py'
        """) == 1
