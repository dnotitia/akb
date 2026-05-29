"""Migration 030: resource content hash projection.

Adds queryable hash/version metadata for Git-backed documents and S3-backed
files. Existing rows are left nullable; services lazily repair document hashes
from Git on read/browse and file hashes are populated on the next confirmed
upload or future repair pass.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db

logger = logging.getLogger("akb.migration.030")


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    await conn.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_hash TEXT")
    await conn.execute(
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS hash_algorithm TEXT DEFAULT 'sha256'"
    )
    await conn.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_hash_commit TEXT")
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_documents_content_hash ON documents(content_hash)"
    )

    await conn.execute("ALTER TABLE vault_files ADD COLUMN IF NOT EXISTS content_hash TEXT")
    await conn.execute(
        "ALTER TABLE vault_files ADD COLUMN IF NOT EXISTS hash_algorithm TEXT DEFAULT 'sha256'"
    )
    await conn.execute("ALTER TABLE vault_files ADD COLUMN IF NOT EXISTS etag TEXT")
    await conn.execute("ALTER TABLE vault_files ADD COLUMN IF NOT EXISTS storage_version TEXT")
    await conn.execute(
        "ALTER TABLE vault_files ADD COLUMN IF NOT EXISTS hash_verified_at TIMESTAMPTZ"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_vault_files_content_hash ON vault_files(content_hash)"
    )

    logger.info("Migration 030 added document/file content hash projection columns.")


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
