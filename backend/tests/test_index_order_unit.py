"""Index ASC/DESC order is PHYSICALLY applied, not just stored as metadata
(#220 review). The e2e only checks index metadata via akb_vault_info; a
regression that dropped the per-column order in create_index (e.g. ignoring
the _ORDER_SQL map) would not be caught there. This asserts the real
PostgreSQL index definition via pg_indexes.indexdef.

Talks to a real Postgres via `AKB_TEST_DSN`; skips when unreachable. Runs in
a disposable database so it never touches a dev DB.
"""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

from app.repositories import table_data_repo

_DSN = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@localhost:5433/akb")


async def _can_connect(dsn: str) -> bool:
    try:
        conn = await asyncpg.connect(dsn, timeout=2.0)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


@pytest.mark.asyncio
async def test_create_index_emits_physical_asc_desc_order():
    if not await _can_connect(_DSN):
        pytest.skip(f"Postgres not reachable at {_DSN}")
    admin = await asyncpg.connect(_DSN)
    dbname = f"akb_idxord_{uuid.uuid4().hex[:8]}"
    await admin.execute(f'CREATE DATABASE "{dbname}"')
    try:
        base, _ = _DSN.rsplit("/", 1)
        conn = await asyncpg.connect(f"{base}/{dbname}")
        try:
            await conn.execute("CREATE TABLE t (a text, b text, c text)")
            # asc (default, no keyword rendered), then desc — through the real
            # create_index path so we test the production DDL builder.
            await table_data_repo.create_index(
                conn, "t", "myidx",
                [("a", "asc"), ("b", "desc"), ("c", "asc")],
            )
            indexdef = await conn.fetchval(
                "SELECT indexdef FROM pg_indexes WHERE indexname = 'myidx'"
            )
            # PG normalises implicit-ASC to no keyword and renders only DESC.
            assert "USING btree (a, b DESC, c)" in indexdef, indexdef
            # discriminating: the non-default order is physically present
            assert "b DESC" in indexdef

            # A unique constraint's implicit index is a plain (all-ASC) btree.
            await table_data_repo.create_unique_constraint(conn, "t", "t_a_key", ["a"])
            ukdef = await conn.fetchval(
                "SELECT indexdef FROM pg_indexes WHERE indexname = 't_a_key'"
            )
            assert "UNIQUE INDEX" in ukdef and "(a)" in ukdef, ukdef
        finally:
            await conn.close()
    finally:
        await admin.execute(f'DROP DATABASE "{dbname}" WITH (FORCE)')
        await admin.close()
