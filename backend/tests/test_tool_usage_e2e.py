"""Live-Postgres tests for the MCP tool-usage rollup/purge.

These are the *primary* correctness proofs for this feature. They cannot be
written against a fake connection, because the properties are properties of the
SQL and of Postgres' commit visibility — an earlier version of this feature
passed twenty mocked unit tests while being wrong in two different ways, and the
`MAX(id)` watermark it then used was wrong in a third that only a real
out-of-order commit exposes.

Excluded from the unit job by the `_e2e` filename filter; run by the
`pgvector-e2e` job, which lists this file explicitly. Skips only when no
`AKB_TEST_DSN` was set, so a local `pytest tests/` stays green; if CI set one
and it is unreachable the suite FAILS, because a gate that green-skips is not
a gate.

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

# When CI sets the DSN explicitly, an unreachable database is a BROKEN GATE,
# not a reason to pass: a wrong password or port would otherwise turn into
# eight skips and a green job — exactly the "gate that never fires" the
# workflow comment warns about. Only the local-development default may skip.
_DSN_FROM_ENV = os.environ.get("AKB_TEST_DSN")
# Matches the dev-compose override and the CI service; not a credential.
_DSN = _DSN_FROM_ENV or "postgresql://akb:akb@localhost:15432/akb"  # pragma: allowlist secret
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
        if _DSN_FROM_ENV:
            pytest.fail(
                f"AKB_TEST_DSN was set but is unreachable ({_DSN_FROM_ENV}) — "
                "this suite is a merge gate and must not skip when CI configured it"
            )
        pytest.skip("Postgres unreachable and no AKB_TEST_DSN set")
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
async def test_skip_locked_lets_a_claimer_past_rows_another_holds(db, monkeypatch):
    """`SKIP LOCKED` is what lets a second claimer make progress instead of
    blocking on rows the first one holds.

    Merely gathering four one-shot rollups does NOT prove this: four fully
    serialised executions over 200 rows with a 50-row batch also produce
    `[50, 50, 50, 50]` and satisfy every assertion, and with a pool that opens
    connections lazily, non-overlap is the likely outcome. So hold the first
    slice locked in an explicit transaction and require the claimer to finish
    anyway — without `SKIP LOCKED` it would block until the holder commits and
    the `wait_for` below would expire.
    """
    monkeypatch.setattr(settings.tool_usage, "maintenance_batch", 50)
    async with db.acquire() as c:
        await _seed(c, days=0, tool="akb_lock", n=100)

    holder = await db.acquire()
    try:
        await holder.execute("BEGIN")
        held = await holder.fetch(
            "SELECT id FROM tool_calls WHERE rolled_at IS NULL "
            "ORDER BY id LIMIT 50 FOR UPDATE"
        )
        assert len(held) == 50, "the holder must actually own the first slice"

        folded = await asyncio.wait_for(tool_usage.rollup_once(), timeout=5.0)

        assert folded == 50, "the claimer must take the NEXT slice, not wait"
        held_ids = {r["id"] for r in held}
        async with db.acquire() as c:
            claimed = {
                r["id"] for r in
                await c.fetch("SELECT id FROM tool_calls WHERE rolled_at IS NOT NULL")
            }
        assert not (held_ids & claimed), "locked rows must have been skipped, not claimed"
    finally:
        await holder.execute("ROLLBACK")
        await db.release(holder)


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


@pytest.mark.asyncio
async def test_record_and_flush_write_each_field_to_its_own_column(db, monkeypatch):
    """The only end-to-end check of `_Row`'s field order against
    `_INSERT_SQL`'s column list.

    `_Row` is a positional NamedTuple; every other test seeds rows with its own
    hand-written INSERT, and the unit suite's fake connection just collects the
    tuples it is handed without binding them to columns. Swapping two
    same-typed fields — `actor_id`/`actor`, or `session_id`/`vault` — would
    type-check, pass everything else, and silently write each value into its
    neighbour's column in production.
    """
    monkeypatch.setattr(settings.tool_usage, "enabled", True)
    tool_usage.reset()
    tool_usage.record(
        "akb_probe", {"uri": "akb://vlt/doc/x.md"},
        type("U", (), {"user_id": "uid-1", "username": "alice"})(),
        {"error": "nope", "code": "NOT_FOUND"},
        session_id="sess-9", duration_ms=77, is_write=True,
    )

    assert await tool_usage.flush_once() == 1

    async with db.acquire() as c:
        row = await c.fetchrow(
            "SELECT tool, actor_id, actor, session_id, vault, outcome, code, "
            "duration_ms, is_write FROM tool_calls"
        )
    assert dict(row) == {
        "tool": "akb_probe", "actor_id": "uid-1", "actor": "alice",
        "session_id": tool_usage._session_ref("sess-9"), "vault": "vlt", "outcome": "error",
        "code": "NOT_FOUND", "duration_ms": 77, "is_write": True,
    }
    tool_usage.reset()


def _raw(tool: str, *, poison: bool = False):
    return tool_usage._Row(
        datetime.now(timezone.utc), tool, "u", "a", None,
        "bad\x00" if poison else "ok", "ok", None, 1, False,
    )


@pytest.mark.asyncio
async def test_poison_isolation_is_atomic_and_exact(db, monkeypatch):
    """The insert path halves a failed batch to isolate rows PostgreSQL will
    never accept, inside ONE transaction with a SAVEPOINT per probe.

    Three properties, none of which a fake connection can show:

    * A class-22 failure inside a nested transaction is recoverable — the
      outer transaction survives it and still commits the siblings.
    * Its `(written, dropped)` accounting matches what is actually stored,
      including when the statement budget runs out mid-traversal.
    * Any OTHER failure rolls the whole traversal back, which is what makes
      `flush_once`'s requeue safe. Before the outer transaction existed, each
      child committed on its own, so a mid-traversal connection reset left
      rows written AND requeued — and the retry inserted them twice.
    """
    batch = [_raw(f"akb_t{i}", poison=(i == 5)) for i in range(16)]

    async with db.acquire() as conn:
        written, dropped = await tool_usage._insert_isolating_poison(conn, batch)
        stored = int(await conn.fetchval("SELECT COUNT(*) FROM tool_calls"))
    assert (written, dropped) == (15, 1)
    assert stored == written, "the reported count must match what is committed"

    # Budget exhaustion still accounts for every row. The exact split matters:
    # `written + dropped == 16` alone would also hold if the budget were
    # ignored entirely and the traversal simply ran to completion, so it would
    # not prove exhaustion happened at all.
    async with db.acquire() as conn:
        await conn.execute("TRUNCATE tool_calls")
    monkeypatch.setattr(tool_usage, "_BISECT_BUDGET", 3)
    async with db.acquire() as conn:
        written, dropped = await tool_usage._insert_isolating_poison(conn, batch)
        stored = int(await conn.fetchval("SELECT COUNT(*) FROM tool_calls"))
    assert (written, dropped) == (4, 12), (
        f"three probes reach one clean quarter and abandon the rest, got "
        f"{(written, dropped)} — an unbounded traversal would give (15, 1)"
    )
    assert stored == written, "and the reported count matches what is committed"


@pytest.mark.asyncio
async def test_a_transient_failure_mid_traversal_commits_nothing(db, monkeypatch):
    """The duplicate-insert case, pinned. A non-`DataError` part-way through
    must leave the table untouched so the requeued batch cannot be written a
    second time on retry."""
    real = tool_usage._probe
    calls = {"n": 0}

    async def _flaky(conn, batch, budget):
        calls["n"] += 1
        # Probe #3 is the FIRST that succeeds (16 fails, 8 fails, 4 commits).
        # The injection has to land after it: failing earlier proves nothing,
        # because nothing had been written yet — an earlier version of this
        # test injected at #3 and passed even with the outer transaction
        # removed, i.e. it did not test the defect it was written for.
        if calls["n"] == 4:
            raise ConnectionResetError("failover after a committed probe")
        return await real(conn, batch, budget)

    monkeypatch.setattr(tool_usage, "_probe", _flaky)
    batch = [_raw(f"akb_t{i}", poison=(i == 5)) for i in range(16)]

    with pytest.raises(ConnectionResetError):
        async with db.acquire() as conn:
            await tool_usage._insert_isolating_poison(conn, batch)

    async with db.acquire() as conn:
        stored = int(await conn.fetchval("SELECT COUNT(*) FROM tool_calls"))
    assert stored == 0, "a partial commit here is what caused duplicate inserts"

    # And the retry writes each surviving row exactly once.
    monkeypatch.setattr(tool_usage, "_probe", real)
    async with db.acquire() as conn:
        written, dropped = await tool_usage._insert_isolating_poison(conn, batch)
        tools = [r["tool"] for r in await conn.fetch("SELECT tool FROM tool_calls")]
    assert (written, dropped) == (15, 1)
    assert len(tools) == len(set(tools)) == 15, f"duplicates: {tools}"
