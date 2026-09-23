"""PostgreSQL proof that resolving known BM25 terms takes no row locks.

`get_or_create_term_ids` runs once per encoded chunk, from every indexing
worker and every backfill writer at once, and almost every term it is handed
already exists. It used to answer with `INSERT ... ON CONFLICT DO UPDATE` whose
update was a no-op. A no-op update is still an update: it locks the existing
row until the statement's transaction ends and writes a new row version, and
`nextval()` runs for every term, conflicting or not. Encoders that share common
terms therefore queued behind one another on the same rows. On a live 2.1M-chunk
sweep that queue was 42% of the writers' sampled wait time; the vocabulary had
taken 161M updates for 953k rows.

These tests hold a lock on an existing term from another session and require
the lookup to answer anyway, and require an existing row to keep its version
and the sequence to stay where it was.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from pathlib import Path

import asyncpg
import pytest

from app.services import sparse_encoder

pytestmark = pytest.mark.asyncio

_DSN = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@localhost:15432/akb")


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
    name = f"akb_bm25_vocab_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    pool = await asyncpg.create_pool(_database_dsn(name), min_size=1, max_size=8)
    previous_pool = None
    try:
        init_sql = (
            Path(__file__).resolve().parents[1] / "app" / "db" / "init.sql"
        ).read_text()
        async with pool.acquire() as conn:
            await conn.execute(init_sql)
        from app.db import postgres as postgres_module

        previous_pool = postgres_module._pool
        postgres_module._pool = pool
        await postgres_module._apply_migrations()
        yield pool
    finally:
        from app.db import postgres as postgres_module

        postgres_module._pool = previous_pool
        await pool.close()
        with contextlib.suppress(asyncpg.PostgresError):
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def _snapshot(pool, terms):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT term, term_id, xmin::text AS version FROM bm25_vocab "
            "WHERE term = ANY($1::text[])",
            list(terms),
        )
        sequence = await conn.fetchval("SELECT last_value FROM bm25_term_id_seq")
    return {r["term"]: (r["term_id"], r["version"]) for r in rows}, sequence


async def test_a_locked_existing_term_is_resolved_without_waiting():
    async with _fresh_database() as pool:
        known = await sparse_encoder.get_or_create_term_ids(["alpha", "beta"])
        holder = await pool.acquire()
        try:
            transaction = holder.transaction()
            await transaction.start()
            # Another encoder mid-statement holds exactly this row lock.
            await holder.execute("SELECT 1 FROM bm25_vocab WHERE term = 'alpha' FOR UPDATE")
            try:
                resolved = await asyncio.wait_for(
                    sparse_encoder.get_or_create_term_ids(["alpha", "beta"]), timeout=5
                )
            finally:
                await transaction.rollback()
        finally:
            await pool.release(holder)
        assert resolved == known


async def test_existing_terms_keep_their_row_version_and_burn_no_ids():
    async with _fresh_database() as pool:
        await sparse_encoder.get_or_create_term_ids(["alpha", "beta", "gamma"])
        before, sequence_before = await _snapshot(pool, ["alpha", "beta", "gamma"])
        for _ in range(3):
            await sparse_encoder.get_or_create_term_ids(["gamma", "alpha", "beta"])
        after, sequence_after = await _snapshot(pool, ["alpha", "beta", "gamma"])
        assert after == before
        assert sequence_after == sequence_before


async def test_new_terms_get_fresh_ids_next_to_existing_ones():
    async with _fresh_database() as pool:
        first = await sparse_encoder.get_or_create_term_ids(["alpha", "beta"])
        mixed = await sparse_encoder.get_or_create_term_ids(["beta", "delta", "", "alpha", "epsilon"])
        assert set(mixed) == {"alpha", "beta", "delta", "epsilon"}
        assert mixed["alpha"] == first["alpha"] and mixed["beta"] == first["beta"]
        assert min(mixed["delta"], mixed["epsilon"]) > max(first.values())
        rows, _ = await _snapshot(pool, ["alpha", "beta", "delta", "epsilon"])
        assert {term: row[0] for term, row in rows.items()} == mixed


async def test_concurrent_callers_agree_on_one_id_per_new_term():
    async with _fresh_database() as pool:
        words = [f"w{i:02d}" for i in range(40)]
        results = await asyncio.gather(*(
            sparse_encoder.get_or_create_term_ids(words[i % 7:] + words[: i % 7])
            for i in range(16)
        ))
        assert all(result == results[0] for result in results)
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM bm25_vocab WHERE term = ANY($1::text[])", words
            ) == len(words)


async def test_a_term_another_session_commits_mid_call_resolves_to_its_id():
    async with _fresh_database() as pool:
        other = await pool.acquire()
        try:
            transaction = other.transaction()
            await transaction.start()
            theirs = await other.fetchval(
                "INSERT INTO bm25_vocab (term, term_id) "
                "VALUES ('zeta', nextval('bm25_term_id_seq')) RETURNING term_id"
            )
            call = asyncio.create_task(sparse_encoder.get_or_create_term_ids(["zeta", "eta"]))
            await asyncio.sleep(0.5)
            # Our insert must wait for their uncommitted row, not duplicate it.
            assert not call.done()
            await transaction.commit()
        finally:
            await pool.release(other)
        resolved = await asyncio.wait_for(call, timeout=5)
        assert resolved["zeta"] == theirs
        assert "eta" in resolved
