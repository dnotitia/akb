"""Schema shared by file capability and lifecycle tests."""

import os
from pathlib import Path

import asyncpg
import pytest_asyncio

from tests.helpers.pg_database import database_template


FILE_TEST_DSN = os.getenv(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",  # pragma: allowlist secret
)

# init.sql plus the migrations this contract needs: the transfer-intent table
# and the column naming the key a capability grants.
_MIGRATIONS = (
    "015_events_outbox.py",
    "019_s3_delete_outbox.py",
    "055_native_revision_m1_file_storage.py",
    "067_vault_file_upload_state.py",
    "079_worker_claim_lifecycle.py",
    "104_file_write_capability_key.py",
)


async def _initialize(conn: asyncpg.Connection) -> None:
    from app.db.postgres import _load_migration

    await conn.execute((Path(__file__).resolve().parents[2] / "app" / "db" / "init.sql").read_text())
    for filename in _MIGRATIONS:
        migration = _load_migration(filename)
        assert migration is not None
        await migration.migrate(conn=conn)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def file_capability_schema_template():
    async with database_template(FILE_TEST_DSN, "akb_file_capability_template_", _initialize) as template:
        yield template
