"""Upgrade-path test for migration 037 (#220 review).

037 adds `vault_tables.unique_keys` + `vault_tables.indexes`. Its first cut
short-circuited on a guard keyed only on `unique_keys`, so a PARTIAL state
(unique_keys present, indexes missing) would never get `indexes` re-added —
after which every read of vault_tables (which SELECTs both) 500s. The guard
was removed in favour of the idempotent `ADD COLUMN IF NOT EXISTS` pair; this
test pins that the upgrade self-heals a partial schema on EXISTING data.

Talks to a real Postgres via `AKB_TEST_DSN` (default the audit stack's
`localhost:5433`); skips when unreachable so the suite runs unattended.
Runs in a disposable database so it never touches a dev DB's data.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from pathlib import Path

import asyncpg
import pytest

_DSN = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@localhost:5433/akb")


async def _can_connect(dsn: str) -> bool:
    try:
        conn = await asyncpg.connect(dsn, timeout=2.0)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


@pytest.mark.asyncio
async def test_migration_037_repairs_partial_state_on_existing_data():
    if not await _can_connect(_DSN):
        pytest.skip(f"Postgres not reachable at {_DSN}")
    admin = await asyncpg.connect(_DSN)
    dbname = f"akb_mig037_{uuid.uuid4().hex[:8]}"
    await admin.execute(f'CREATE DATABASE "{dbname}"')
    try:
        base, _ = _DSN.rsplit("/", 1)
        conn = await asyncpg.connect(f"{base}/{dbname}")
        try:
            await conn.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
            # Pre-037 PARTIAL state: unique_keys EXISTS but indexes does NOT —
            # exactly what the old single-column guard mishandled.
            await conn.execute(
                """
                CREATE TABLE vault_tables (
                    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
                    vault_id uuid,
                    name text,
                    columns jsonb NOT NULL DEFAULT '[]',
                    unique_keys jsonb NOT NULL DEFAULT '[]'
                )
                """
            )
            await conn.execute(
                "INSERT INTO vault_tables (vault_id, name, columns, unique_keys) "
                "VALUES (uuid_generate_v4(), 't', '[]', '[{\"name\": \"uk_x\"}]')"
            )

            # Load the migration straight from its source file (NOT
            # importlib.import_module, which would honour a stale __pycache__
            # .pyc and could mask a future source regression).
            mig_path = (
                Path(__file__).resolve().parents[1]
                / "app" / "db" / "migrations" / "037_table_unique_keys_indexes.py"
            )
            spec = importlib.util.spec_from_file_location("mig_037_under_test", mig_path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            await mod.migrate(conn=conn)

            names = {
                r["column_name"]
                for r in await conn.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'vault_tables'"
                )
            }
            # BOTH columns present (old guard would have skipped → no `indexes`).
            assert "unique_keys" in names and "indexes" in names

            row = await conn.fetchrow(
                "SELECT unique_keys, indexes FROM vault_tables WHERE name = 't'"
            )
            assert "uk_x" in row["unique_keys"]      # pre-existing data intact
            assert row["indexes"] == "[]"            # new column defaulted

            # Idempotent: a second run is a clean no-op.
            await mod.migrate(conn=conn)
        finally:
            await conn.close()
    finally:
        await admin.execute(f'DROP DATABASE "{dbname}" WITH (FORCE)')
        await admin.close()
