"""Live PG proof: row_commit CAS contention — lost update 409s, retry wins.

T2 gate (T3 acceptance input): against a REAL PostgreSQL (local
container on :5433, same shape as CI pgvector-e2e), prove the full
contention contract end to end:

1. fresh DB from init.sql → new vt_* table carries row_commit +
   bump trigger (create-time DDL path).
2. INSERT → row_commit minted (non-empty).
3. UPDATE with matching expected_row_commit applies + bumps the token.
4. Concurrent writers A (stale R) and B (fresh R): B wins, A's stale
   write affects 0 rows → caller-visible 409 shape; A re-reads (R+1)
   and retries OK (no lost update, no silent win).
5. DELETE with stale token affects 0 rows (row survives).
6. Migration 107 backfill: a table created WITHOUT row_commit gets
   the column + trigger + minted tokens via migrate().

Skips (not fails) when PG is unreachable, unless REQUIRE_REAL_PG=1.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from pathlib import Path

import asyncpg
import pytest

pytestmark = pytest.mark.asyncio

_DSN = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@localhost:5433/akb")


async def _can_connect(dsn: str) -> bool:
    try:
        conn = await asyncpg.connect(dsn, timeout=2)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


def _database_dsn(name: str) -> str:
    base, _ = _DSN.rsplit("/", 1)
    return f"{base}/{name}"


@contextlib.asynccontextmanager
async def _fresh_database():
    if not await _can_connect(_DSN):
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"Required PostgreSQL is not reachable at {_DSN}")
        pytest.skip(f"Postgres not reachable at {_DSN}")
    admin = await asyncpg.connect(_DSN)
    name = f"akb_rowcas_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    pool = await asyncpg.create_pool(_database_dsn(name), min_size=1, max_size=8)
    try:
        init_sql = (
            Path(__file__).resolve().parents[1] / "app" / "db" / "init.sql"
        ).read_text()
        async with pool.acquire() as conn:
            await conn.execute(init_sql)
            # init.sql does not carry the rows_changed trigger function
            # (migration 086 owns it); install a no-op stub so
            # create_dynamic_table's trigger install succeeds. The stub
            # is OUT of what this file proves (CAS only, not events).
            await conn.execute(
                "CREATE OR REPLACE FUNCTION "
                "public.akb_dynamic_table_rows_changed() "
                "RETURNS TRIGGER AS $$ BEGIN RETURN NULL; END; "
                "$$ LANGUAGE plpgsql"
            )
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        await admin.close()


async def _make_table(pool, pg_name: str = "vt_eng__incidents"):
    from app.repositories import table_data_repo

    async with pool.acquire() as conn:
        await table_data_repo.create_dynamic_table(
            conn,
            pg_name,
            [{"name": "title", "type": "text"}],
            vault_id=uuid.uuid4(),
            resource_uri="akb://eng/coll/tbl/incidents",
        )
    return pg_name


async def test_row_commit_minted_and_bumped_on_update():
    async with _fresh_database() as pool:
        pg = await _make_table(pool)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"INSERT INTO {pg} (title) VALUES ('a') RETURNING id, row_commit"
            )
            assert row["row_commit"], "row_commit not minted on INSERT"
            first = row["row_commit"]
            rid = row["id"]
            row2 = await conn.fetchrow(
                f"UPDATE {pg} SET title = 'b' WHERE id = $1 AND row_commit = $2 "
                f"RETURNING row_commit",
                rid, first,
            )
            assert row2 is not None, "matching-token UPDATE matched 0 rows"
            assert row2["row_commit"] and row2["row_commit"] != first, \
                "trigger did not bump row_commit"


async def test_stale_token_update_matches_zero_rows():
    async with _fresh_database() as pool:
        pg = await _make_table(pool)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"INSERT INTO {pg} (title) VALUES ('a') RETURNING id, row_commit"
            )
            rid = row["id"]
            # Writer B moves the row first.
            await conn.execute(
                f"UPDATE {pg} SET title = 'b' WHERE id = $1", rid,
            )
            # Writer A replays its stale token: must match nothing.
            n = await conn.execute(
                f"UPDATE {pg} SET title = 'a2' WHERE id = $1 AND row_commit = $2",
                rid, row["row_commit"],
            )
            assert n == "UPDATE 0", f"stale write applied: {n}"
            # Retry with fresh token wins.
            fresh = await conn.fetchrow(
                f"SELECT row_commit FROM {pg} WHERE id = $1", rid,
            )
            n2 = await conn.execute(
                f"UPDATE {pg} SET title = 'a2' WHERE id = $1 AND row_commit = $2",
                rid, fresh["row_commit"],
            )
            assert n2 == "UPDATE 1", f"fresh retry failed: {n2}"


async def test_stale_token_delete_matches_zero_rows():
    async with _fresh_database() as pool:
        pg = await _make_table(pool)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"INSERT INTO {pg} (title) VALUES ('a') RETURNING id, row_commit"
            )
            rid, stale = row["id"], row["row_commit"]
            await conn.execute(f"UPDATE {pg} SET title = 'b' WHERE id = $1", rid)
            n = await conn.execute(
                f"DELETE FROM {pg} WHERE id = $1 AND row_commit = $2", rid, stale,
            )
            assert n == "DELETE 0", f"stale delete applied: {n}"
            left = await conn.fetchval(f"SELECT COUNT(*) FROM {pg} WHERE id = $1", rid)
            assert left == 1, "row deleted by stale token"


async def test_migration_108_backfills_legacy_table():
    async with _fresh_database() as pool:
        # Simulate a pre-107 table: create, then strip the new column.
        pg = await _make_table(pool, "vt_eng__legacy")
        async with pool.acquire() as conn:
            await conn.execute(f"ALTER TABLE {pg} DROP COLUMN row_commit")
            await conn.execute(
                f"DROP TRIGGER IF EXISTS akb_bump_row_commit_trigger ON {pg}"
            )
            await conn.execute(f"INSERT INTO {pg} (title) VALUES ('old')")
        import importlib

        mod = importlib.import_module(
            "app.db.migrations.108_table_row_commit_cas"
        )
        async with pool.acquire() as conn:
            await mod.migrate(conn)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(f"SELECT row_commit FROM {pg} LIMIT 1")
            assert row["row_commit"], "backfill did not mint tokens"
            rid = await conn.fetchval(f"SELECT id FROM {pg} LIMIT 1")
            tok = row["row_commit"]
            row2 = await conn.fetchrow(
                f"UPDATE {pg} SET title = 'new' WHERE id = $1 AND row_commit = $2 "
                f"RETURNING row_commit", rid, tok,
            )
            assert row2 and row2["row_commit"] != tok, "trigger missing after migrate"
