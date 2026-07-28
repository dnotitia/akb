"""Live-Postgres tests for the MCP tool-usage rollup/purge.

These are the *primary* correctness proofs for this feature. They cannot be
written against a fake connection, because the properties are properties of the
SQL and of Postgres' commit visibility — an earlier version of this feature
passed twenty mocked unit tests while being wrong in two different ways, and the
`MAX(id)` watermark it then used was wrong in a third that only a real
out-of-order commit exposes.

Excluded from the unit job by the `_e2e` filename filter; run by the
`pgvector-e2e` job, which lists this file explicitly. Skips when
`AKB_TEST_DSN` is unreachable so a local `pytest tests/` stays green.

Isolation: everything runs in a dedicated schema created and dropped per
module, so the suite can never touch application data (an earlier revision
issued a bare `TRUNCATE tool_calls` against whatever database the app config
happened to resolve).
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import pathlib
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

from app.config import settings
from app.services import tool_usage

_DSN = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@localhost:15432/akb")
_SCHEMA = "tool_usage_e2e"


async def _can_connect(dsn: str) -> bool:
    try:
        conn = await asyncpg.connect(dsn, timeout=2.0)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


def _migration():
    path = pathlib.Path(__file__).resolve().parents[1] / "app/db/migrations/046_tool_usage.py"
    spec = importlib.util.spec_from_file_location("m046", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # type: ignore[union-attr]
    return mod


@pytest_asyncio.fixture
async def db(monkeypatch):
    """A pool bound to a throwaway schema, with the service pointed at it.

    Function-scoped because each test gets its own event loop and an asyncpg
    pool cannot be shared across loops. The migration is `IF NOT EXISTS`
    throughout, so re-applying per test is a no-op after the first.
    """
    if not await _can_connect(_DSN):
        pytest.skip("Postgres unreachable at AKB_TEST_DSN")
    pool = await asyncpg.create_pool(
        _DSN, min_size=1, max_size=8,
        server_settings={"search_path": _SCHEMA},
    )
    async with pool.acquire() as c:
        await c.execute(f"CREATE SCHEMA IF NOT EXISTS {_SCHEMA}")
        await _migration().migrate(c)
        await c.execute("TRUNCATE tool_calls, tool_usage_daily")

    async def _get_pool():
        return pool

    monkeypatch.setattr(tool_usage, "get_pool", _get_pool)
    yield pool
    async with pool.acquire() as c:
        await c.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await pool.close()


async def _seed(conn, *, days: float, tool: str, outcome: str = "ok", n: int = 1) -> None:
    when = datetime.now(timezone.utc) + timedelta(days=days)
    for _ in range(n):
        await conn.execute(
            "INSERT INTO tool_calls (occurred_at, tool, outcome, duration_ms) "
            "VALUES ($1, $2, $3, 10)", when, tool, outcome,
        )


async def _unclaimed(pool) -> int:
    async with pool.acquire() as c:
        return int(await c.fetchval("SELECT COUNT(*) FROM tool_calls WHERE rolled_at IS NULL"))


async def _folded(pool, tool: str | None = None) -> int:
    q = "SELECT COALESCE(SUM(calls), 0) FROM tool_usage_daily"
    async with pool.acquire() as c:
        if tool:
            return int(await c.fetchval(q + " WHERE tool = $1", tool))
        return int(await c.fetchval(q))


@pytest.mark.asyncio
async def test_migration_is_idempotent(db):
    """Re-applying must not fail or change the shape — the runner skips a
    ledgered filename, so a second apply only ever happens by hand."""
    async with db.acquire() as c:
        await _migration().migrate(c)
        cols = int(await c.fetchval(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = $1 AND table_name = 'tool_calls'", _SCHEMA,
        ))
    assert cols == 12


@pytest.mark.asyncio
async def test_rollup_folds_and_stamps_then_is_a_noop(db):
    async with db.acquire() as c:
        await _seed(c, days=-40, tool="akb_put", n=5)     # older than retention
        await _seed(c, days=-1, tool="akb_search", n=3)

    assert await tool_usage.rollup_once() == 8
    assert await _folded(db) == 8
    assert await _unclaimed(db) == 0

    assert await tool_usage.rollup_once() == 0, "nothing left to claim"
    assert await _folded(db) == 8, "a second pass must not double count"


@pytest.mark.asyncio
async def test_row_committed_after_a_higher_id_is_still_folded(db):
    """The case a `MAX(id)` watermark got wrong, and the reason this design
    claims rows instead.

    Postgres allocates sequence values BEFORE commit. If a watermark is taken
    while a lower id is still uncommitted, that row lands permanently below the
    mark: never aggregated, and then deleted by a purge predicate that trusts
    the mark. Claiming has no ordering assumption — the row is simply invisible
    until it commits, and claimable immediately after.
    """
    conn_a = await db.acquire()
    conn_b = await db.acquire()
    try:
        await conn_a.execute("BEGIN")
        await _seed(conn_a, days=0, tool="akb_slow")      # lower id, still open
        await conn_b.execute("BEGIN")
        await _seed(conn_b, days=0, tool="akb_fast")      # higher id
        await conn_b.execute("COMMIT")                    # commits FIRST

        first = await tool_usage.rollup_once()
        await conn_a.execute("COMMIT")                    # lower id commits LATE
        second = await tool_usage.rollup_once()
    finally:
        await db.release(conn_a)
        await db.release(conn_b)

    # Both halves matter: the first pass must have seen ONLY the higher id
    # (proving the lower one was genuinely invisible), and the second must then
    # pick it up. Without both, this would pass on a design that never had the
    # race to begin with.
    assert first == 1, "the open row must not have been visible to the first fold"
    assert second == 1, "the late commit must be folded once it is visible"
    assert await _folded(db, "akb_slow") == 1
    assert await _unclaimed(db) == 0


@pytest.mark.asyncio
async def test_concurrent_runners_each_take_a_distinct_slice(db, monkeypatch):
    """`SKIP LOCKED` is what lets a second runner make progress instead of
    blocking on the first one's locked rows.

    The batch is deliberately smaller than the row count: with a batch large
    enough to swallow everything, one runner finishes the work and the others
    return zero, and the test would pass without any overlap ever occurring.
    """
    monkeypatch.setattr(settings.tool_usage, "maintenance_batch", 50)
    async with db.acquire() as c:
        await _seed(c, days=0, tool="akb_race", n=200)

    results = await asyncio.gather(*(tool_usage.rollup_once() for _ in range(4)))

    assert sum(results) == 200, f"every row folded exactly once, got {results}"
    assert all(r > 0 for r in results), (
        f"each runner must claim a slice — {results} means they serialised"
    )
    assert await _folded(db, "akb_race") == 200
    assert await _unclaimed(db) == 0


@pytest.mark.asyncio
async def test_late_arrival_adds_to_an_already_folded_day(db):
    """The upsert is additive, so a row inserted after its day was aggregated
    increments that day. A recompute-style rollup would instead overwrite the
    complete total with whatever fragment it could still see."""
    async with db.acquire() as c:
        await _seed(c, days=0, tool="akb_late", n=5)
    await tool_usage.rollup_once()

    async with db.acquire() as c:
        await _seed(c, days=0, tool="akb_late", n=7)
    await tool_usage.rollup_once()

    assert await _folded(db, "akb_late") == 12


@pytest.mark.asyncio
async def test_purge_removes_only_old_and_folded_rows(db):
    async with db.acquire() as c:
        await _seed(c, days=-40, tool="akb_old", n=5)
        await _seed(c, days=0, tool="akb_new", n=2)
    await tool_usage.rollup_once()
    async with db.acquire() as c:
        await _seed(c, days=-40, tool="akb_orphan", n=1)   # old but NOT folded

    removed = await tool_usage.purge_once()

    async with db.acquire() as c:
        orphan = int(await c.fetchval(
            "SELECT COUNT(*) FROM tool_calls WHERE tool = 'akb_orphan'"))
        old = int(await c.fetchval(
            "SELECT COUNT(*) FROM tool_calls WHERE tool = 'akb_old'"))
        new = int(await c.fetchval(
            "SELECT COUNT(*) FROM tool_calls WHERE tool = 'akb_new'"))

    assert removed == 5
    assert old == 0, "old + folded rows are purged"
    assert new == 2, "recent rows are kept"
    assert orphan == 1, "an unfolded row survives however old it is"
    assert await _folded(db, "akb_old") == 5, "the aggregate outlives its source rows"


@pytest.mark.asyncio
async def test_purge_is_not_starved_by_a_continuing_backlog(db, monkeypatch):
    """`maintenance_once` must prune on the same tick it folds. Gating purge on
    the rollup reaching zero starves it under sustained arrivals, and the table
    the retention policy exists to bound grows without limit."""
    monkeypatch.setattr(settings.tool_usage, "maintenance_batch", 10)
    async with db.acquire() as c:
        await _seed(c, days=-40, tool="akb_purgeable", n=10)
    await tool_usage.rollup_once()                      # now old AND folded
    async with db.acquire() as c:
        await _seed(c, days=0, tool="akb_arriving", n=50)   # a standing backlog

    moved = await tool_usage.maintenance_once()

    async with db.acquire() as c:
        left = int(await c.fetchval(
            "SELECT COUNT(*) FROM tool_calls WHERE tool = 'akb_purgeable'"))
    assert left == 0, "purge ran despite the rollup still having work"
    assert moved > 10, "the tick reports both the fold and the prune"


@pytest.mark.asyncio
async def test_one_statement_is_bounded(db, monkeypatch):
    """Catching up after an outage must not become a single transaction large
    enough to hit the statement timeout or spike WAL."""
    monkeypatch.setattr(settings.tool_usage, "maintenance_batch", 10)
    async with db.acquire() as c:
        await _seed(c, days=0, tool="akb_batch", n=25)

    assert await tool_usage.rollup_once() == 10
    assert await _unclaimed(db) == 15
