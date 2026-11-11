"""Live-PostgreSQL contract for migration 130 (one index on tokens.token_hash).

`tokens.token_hash` is UNIQUE, so PostgreSQL already indexes it as
`tokens_token_hash_key`. Older init.sql also created `idx_tokens_hash`, a plain
btree over the same column. These tests boot the real schema path, init.sql
followed by the migration runner, and check that:

  * an existing installation that has the duplicate loses it at boot, keeps
    the unique index, and still rejects a duplicate hash;
  * a token lookup by hash is still served by an index;
  * booting again, re-running the migration, and re-running init.sql all
    leave the schema as it is (the duplicate does not come back);
  * a fresh installation never has the duplicate.

Runs in a disposable database. Registered in the `pgvector e2e (live DB)` CI
job, which sets `AKB_TEST_DSN`.
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import pytest

pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[1]
_INIT_SQL = (_BACKEND / "app" / "db" / "init.sql").read_text()
_MIGRATION = "130_drop_duplicate_tokens_hash_index.py"
# What init.sql ran on every boot before this migration existed.
_OLD_INIT_DUPLICATE_INDEX = "CREATE INDEX IF NOT EXISTS idx_tokens_hash ON tokens(token_hash);"
_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",  # pragma: allowlist secret
)


async def _can_connect(dsn: str) -> bool:
    try:
        conn = await asyncpg.connect(dsn, timeout=2)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


@asynccontextmanager
async def _database():
    if not await _can_connect(_DSN):
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"REQUIRE_REAL_PG=1 but Postgres is not reachable at {_DSN}")
        pytest.skip(f"Postgres not reachable at {_DSN}")
    admin = await asyncpg.connect(_DSN)
    name = f"akb_tokens_hash_index_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    base, _ = _DSN.rsplit("/", 1)
    pool = None
    try:
        pool = await asyncpg.create_pool(f"{base}/{name}", min_size=1, max_size=4)
        yield pool
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


async def _boot(monkeypatch, pool) -> None:
    """Run the startup schema path, init.sql then pending migrations, on `pool`."""
    from app.db import postgres

    async def get_test_pool():
        return pool

    @asynccontextmanager
    async def test_migration_pool():
        yield pool

    monkeypatch.setattr(postgres, "get_pool", get_test_pool)
    monkeypatch.setattr(postgres, "_migration_pool", test_migration_pool)
    await postgres.init_db(max_retries=1, delay=0)


async def _token_hash_indexes(conn) -> dict[str, bool]:
    """Every index whose only key is tokens.token_hash, and whether it is unique."""
    rows = await conn.fetch(
        """
        SELECT index_class.relname AS name, index_info.indisunique AS is_unique
          FROM pg_index index_info
          JOIN pg_class index_class ON index_class.oid = index_info.indexrelid
          JOIN pg_attribute attribute
            ON attribute.attrelid = index_info.indrelid
           AND attribute.attnum = index_info.indkey[0]
         WHERE index_info.indrelid = 'public.tokens'::regclass
           AND index_info.indnkeyatts = 1
           AND attribute.attname = 'token_hash'
        """
    )
    return {row["name"]: row["is_unique"] for row in rows}


def _index_names(node: dict) -> set[str]:
    names = {node["Index Name"]} if "Index Name" in node else set()
    for child in node.get("Plans", []):
        names |= _index_names(child)
    return names


async def _ledger_rows(conn) -> int:
    return await conn.fetchval("SELECT count(*) FROM schema_migrations WHERE filename = $1", _MIGRATION)


async def _insert_token(conn, user_id: uuid.UUID, token_hash: str) -> None:
    await conn.execute(
        """
        INSERT INTO tokens (user_id, name, token_hash, token_prefix)
        VALUES ($1, 'fixture', $2, 'akb_fixt')
        """,
        user_id,
        token_hash,
    )


async def test_boot_drops_the_duplicate_index_and_keeps_uniqueness(monkeypatch):
    async with _database() as pool:
        async with pool.acquire() as conn:
            # An installation created by an older release: same tables, plus
            # the duplicate index its init.sql created.
            await conn.execute(_INIT_SQL)
            await conn.execute(_OLD_INIT_DUPLICATE_INDEX)
            assert await _token_hash_indexes(conn) == {"tokens_token_hash_key": True, "idx_tokens_hash": False}

        await _boot(monkeypatch, pool)

        async with pool.acquire() as conn:
            assert await _token_hash_indexes(conn) == {"tokens_token_hash_key": True}
            assert await _ledger_rows(conn) == 1

            user_id = await conn.fetchval(
                """
                INSERT INTO users (username, email, password_hash)
                VALUES ('token-owner', 'token-owner@example.test', 'unusable')
                RETURNING id
                """
            )
            await _insert_token(conn, user_id, "a" * 64)
            with pytest.raises(asyncpg.UniqueViolationError):
                await _insert_token(conn, user_id, "a" * 64)

            # Authentication looks a token up by its hash; the unique index
            # serves it.
            await conn.execute("SET enable_seqscan = off")
            raw = await conn.fetchval(
                "EXPLAIN (FORMAT JSON) SELECT id FROM tokens WHERE token_hash = $1", "a" * 64,
            )
            plan = json.loads(raw) if isinstance(raw, str) else raw
            assert "tokens_token_hash_key" in _index_names(plan[0]["Plan"]), plan
            await conn.execute("RESET enable_seqscan")

        # Booting again changes nothing: the ledger skips 130, and init.sql no
        # longer recreates the duplicate.
        await _boot(monkeypatch, pool)

        from app.db.postgres import _load_migration, _run_one_migration

        migration = _load_migration(_MIGRATION)
        assert migration is not None
        async with pool.acquire() as conn:
            # The migration itself is a no-op when re-run, directly or through
            # the runner (whose ledger write is ON CONFLICT DO NOTHING).
            await migration.migrate(conn=conn)
            await _run_one_migration(conn, _MIGRATION, migration)
            await conn.execute(_INIT_SQL)
            assert await _token_hash_indexes(conn) == {"tokens_token_hash_key": True}
            assert await _ledger_rows(conn) == 1


async def test_fresh_install_never_has_the_duplicate_index(monkeypatch):
    async with _database() as pool:
        await _boot(monkeypatch, pool)

        async with pool.acquire() as conn:
            assert await _token_hash_indexes(conn) == {"tokens_token_hash_key": True}
            assert await _ledger_rows(conn) == 1
