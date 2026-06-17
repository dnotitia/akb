"""Regression guard for the add-unique-key concurrency race (#220 review).

`alter_table`'s FOR UPDATE locks only the `vault_tables` registry row, NOT the
data table — so a concurrent INSERT can land a duplicate BETWEEN the
duplicate-preflight SELECT and the `ADD CONSTRAINT`. The preflight is therefore
best-effort; `ADD CONSTRAINT` is the real enforcement, and its
`asyncpg.UniqueViolationError` must surface as a clean `ValidationError` (422),
NOT an uncaught 500 — with the whole alter rolled back (no half-applied
constraint).

This reproduces the window DETERMINISTICALLY (no sleeps): a monkeypatched
`unique_key_duplicates` runs the real preflight, then commits a duplicate on a
SEPARATE connection, then returns the (clean) preflight result.

Talks to a real Postgres via `AKB_TEST_DSN`; skips when unreachable.
"""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest
import pytest_asyncio

from app.exceptions import ValidationError
from app.repositories import table_data_repo
from app.services import table_service

_DSN = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@localhost:5433/akb")


async def _can_connect(dsn: str) -> bool:
    try:
        conn = await asyncpg.connect(dsn, timeout=2.0)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


@pytest_asyncio.fixture
async def seeded():
    if not await _can_connect(_DSN):
        pytest.skip(f"Postgres not reachable at {_DSN}")
    from app.repositories.vault_repo import VaultRepository
    pool = await asyncpg.create_pool(dsn=_DSN, min_size=2, max_size=6)
    vname = f"race_{uuid.uuid4().hex[:8]}"
    tname = "people"
    pg_name = table_data_repo.pg_table_name(vname, tname)
    cols = [{"name": "email", "type": "text"}]
    # Real vaults/vault_tables schema (from init.sql, already present in the DB).
    vid = await VaultRepository(pool).create(
        name=vname, description="ephemeral race-test vault",
        git_path=f"/tmp/{vname}.git", owner_id=None,
    )
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO vault_tables (vault_id, name, columns) VALUES ($1, $2, $3::jsonb)",
            vid, tname, '[{"name": "email", "type": "text"}]',
        )
        await table_data_repo.create_dynamic_table(conn, pg_name, cols)
        # seed a single (non-duplicate) row so the preflight is genuinely clean
        await conn.execute(f"INSERT INTO {pg_name} (email) VALUES ('seed@x')")
    # wire the module-global pool that alter_table's get_pool() returns
    from app.db import postgres as pg_mod
    prev = pg_mod._pool
    pg_mod._pool = pool
    try:
        yield {"vid": vid, "vname": vname, "tname": tname, "pg_name": pg_name, "pool": pool}
    finally:
        async with pool.acquire() as conn:
            await conn.execute(f"DROP TABLE IF EXISTS {pg_name}")
            await conn.execute("DELETE FROM vault_tables WHERE vault_id = $1", vid)
            await conn.execute("DELETE FROM vaults WHERE id = $1", vid)
        pg_mod._pool = prev
        await pool.close()


@pytest.mark.asyncio
async def test_concurrent_insert_after_preflight_yields_clean_422(seeded, monkeypatch):
    pg_name = seeded["pg_name"]
    orig = table_data_repo.unique_key_duplicates

    async def racing(conn, name, columns, limit=5):
        res = await orig(conn, name, columns, limit=limit)  # real preflight → clean
        # a concurrent committed writer introduces a duplicate in the window
        w = await asyncpg.connect(_DSN)
        try:
            await w.execute(f"INSERT INTO {name} (email) VALUES ('seed@x')")
        finally:
            await w.close()
        return res

    monkeypatch.setattr(table_data_repo, "unique_key_duplicates", racing)

    with pytest.raises(ValidationError) as ei:
        await table_service.alter_table(
            seeded["vid"], seeded["tname"], actor_id="tester",
            add_unique_keys=[{"name": "people_email_key", "columns": ["email"]}],
        )
    # mapped to a clean 422, not a 500
    assert ei.value.status_code == 422

    # atomicity: no unique constraint was left half-applied, and the registry
    # still advertises no unique key.
    async with seeded["pool"].acquire() as conn:
        ucount = await conn.fetchval(
            "SELECT count(*) FROM pg_constraint WHERE conrelid = $1::regclass AND contype = 'u'",
            pg_name,
        )
        assert ucount == 0
        uk = await conn.fetchval(
            "SELECT unique_keys FROM vault_tables WHERE vault_id = $1", seeded["vid"]
        )
        assert uk == "[]"
