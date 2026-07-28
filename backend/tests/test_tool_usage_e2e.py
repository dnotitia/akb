"""E2E tests for MCP tool-usage rollup/purge against a live PostgreSQL.

Runs inside the backend pod (or the local docker stack) so it exercises real
transaction visibility, which is the whole point: the properties below are
properties of the SQL and of Postgres' commit semantics, and a faked connection
cannot show any of them. An earlier version of this feature passed 20 mocked
unit tests while being wrong in two different ways.

  T1  Migration is idempotent — re-running creates nothing and drops nothing
  T2  Rollup folds pending rows and stamps them claimed
  T3  Rollup re-run is a no-op — no double count
  T4  **Out-of-order commit** — a row whose transaction was still open when an
      later-id row committed is still folded once it commits. This is the case
      a `MAX(id)` watermark got wrong: sequences are allocated BEFORE commit,
      so the mark advanced past the open row, which was then never aggregated
      and purged anyway. Silent permanent undercount.
  T5  Concurrent rollup runners fold each row exactly once
  T6  A late arrival for an already-folded day ADDS to that day
  T7  Purge removes only rows that are both past retention and claimed
  T8  An old but unclaimed row survives purge
  T9  Aggregates outlive the raw rows they came from
  T10 One statement moves at most `rollup_batch` rows

Usage:  python tests/test_tool_usage_e2e.py
"""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "/app")

from app.config import settings  # noqa: E402
from app.db.postgres import close_pool, get_pool, init_db  # noqa: E402
from app.services import tool_usage  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}  {detail}")


async def _reset(pool) -> None:
    async with pool.acquire() as c:
        await c.execute("TRUNCATE tool_calls, tool_usage_daily")


async def _seed(conn, *, days: int, tool: str, outcome: str = "ok", n: int = 1) -> None:
    for _ in range(n):
        await conn.execute(
            "INSERT INTO tool_calls (occurred_at, tool, outcome, duration_ms) "
            "VALUES (NOW() + ($1 || ' days')::interval, $2, $3, 10)",
            str(days), tool, outcome,
        )


async def _counts(pool) -> tuple[int, int, int]:
    async with pool.acquire() as c:
        raw = await c.fetchval("SELECT COUNT(*) FROM tool_calls")
        unclaimed = await c.fetchval(
            "SELECT COUNT(*) FROM tool_calls WHERE rolled_at IS NULL"
        )
        agg = await c.fetchval("SELECT COALESCE(SUM(calls), 0) FROM tool_usage_daily")
    return int(raw), int(unclaimed), int(agg)


async def main() -> None:
    await init_db()
    pool = await get_pool()

    # ── T1 migration idempotency ────────────────────────────────
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "m046", "/app/app/db/migrations/046_tool_usage.py"
    )
    m046 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m046)
    await m046.migrate()
    await m046.migrate()
    async with pool.acquire() as c:
        cols = await c.fetchval(
            "SELECT COUNT(*) FROM information_schema.columns WHERE table_name='tool_calls'"
        )
        has_state = await c.fetchval("SELECT to_regclass('tool_usage_rollup_state')")
    check("T1 migration idempotent", int(cols) == 12, f"columns={cols}")
    check("T1 no sequence-watermark table", has_state is None, f"{has_state}")

    # ── T2/T3 fold + no double count ────────────────────────────
    await _reset(pool)
    async with pool.acquire() as c:
        await _seed(c, days=-40, tool="akb_put", n=5)
        await _seed(c, days=-1, tool="akb_search", n=3)
    folded = await tool_usage.rollup_once()
    raw, unclaimed, agg = await _counts(pool)
    check("T2 rollup folds pending rows", folded == 8 and agg == 8, f"folded={folded} agg={agg}")
    check("T2 folded rows are stamped", unclaimed == 0, f"unclaimed={unclaimed}")

    again = await tool_usage.rollup_once()
    _, _, agg2 = await _counts(pool)
    check("T3 re-run is a no-op", again == 0 and agg2 == 8, f"again={again} agg={agg2}")

    # ── T4 out-of-order commit (the watermark killer) ───────────
    await _reset(pool)
    conn_a = await pool.acquire()
    conn_b = await pool.acquire()
    try:
        await conn_a.execute("BEGIN")
        await _seed(conn_a, days=0, tool="akb_slow")      # id N, still open
        await conn_b.execute("BEGIN")
        await _seed(conn_b, days=0, tool="akb_fast")      # id N+1
        await conn_b.execute("COMMIT")                    # commits FIRST

        first = await tool_usage.rollup_once()            # only sees akb_fast
        await conn_a.execute("COMMIT")                    # lower id commits LATE
        second = await tool_usage.rollup_once()
    finally:
        await pool.release(conn_a)
        await pool.release(conn_b)

    async with pool.acquire() as c:
        slow = await c.fetchval(
            "SELECT COALESCE(SUM(calls),0) FROM tool_usage_daily WHERE tool='akb_slow'"
        )
        left = await c.fetchval("SELECT COUNT(*) FROM tool_calls WHERE rolled_at IS NULL")
    # first==1/second==1 is what makes this non-tautological: the first fold
    # must have seen ONLY the higher-id row (proving the lower id was still
    # invisible), and the second must then pick the lower id up.
    check(
        "T4 late-committing lower id is still folded",
        first == 1 and second == 1 and int(slow) == 1 and int(left) == 0,
        f"first={first} second={second} akb_slow={slow} unclaimed={left}",
    )

    # ── T5 concurrent runners ───────────────────────────────────
    await _reset(pool)
    async with pool.acquire() as c:
        await _seed(c, days=0, tool="akb_race", n=200)
    results = await asyncio.gather(*(tool_usage.rollup_once() for _ in range(4)))
    raw, unclaimed, agg = await _counts(pool)
    check(
        "T5 concurrent runners fold each row once",
        sum(results) == 200 and agg == 200 and unclaimed == 0,
        f"results={results} agg={agg} unclaimed={unclaimed}",
    )

    # ── T6 late arrival adds to an already-folded day ───────────
    async with pool.acquire() as c:
        await _seed(c, days=0, tool="akb_race", n=7)
    await tool_usage.rollup_once()
    async with pool.acquire() as c:
        total = await c.fetchval(
            "SELECT SUM(calls) FROM tool_usage_daily WHERE tool='akb_race'"
        )
    check("T6 late arrival adds (200+7)", int(total) == 207, f"total={total}")

    # ── T7/T8/T9 purge ──────────────────────────────────────────
    await _reset(pool)
    async with pool.acquire() as c:
        await _seed(c, days=-40, tool="akb_old", n=5)     # old, will be claimed
        await _seed(c, days=0, tool="akb_new", n=2)       # recent
    await tool_usage.rollup_once()
    async with pool.acquire() as c:
        await _seed(c, days=-40, tool="akb_orphan", n=1)  # old, NOT claimed
    removed = await tool_usage.purge_once()
    async with pool.acquire() as c:
        orphan = await c.fetchval("SELECT COUNT(*) FROM tool_calls WHERE tool='akb_orphan'")
        old = await c.fetchval("SELECT COUNT(*) FROM tool_calls WHERE tool='akb_old'")
        agg_old = await c.fetchval(
            "SELECT COALESCE(SUM(calls),0) FROM tool_usage_daily WHERE tool='akb_old'"
        )
    check("T7 purge removes old+claimed only", removed == 5 and int(old) == 0, f"removed={removed}")
    check("T8 old but unclaimed row survives", int(orphan) == 1, f"orphan={orphan}")
    check("T9 aggregate outlives its raw rows", int(agg_old) == 5, f"agg_old={agg_old}")

    # ── T10 batch bound ─────────────────────────────────────────
    await _reset(pool)
    original = settings.tool_usage.rollup_batch
    try:
        settings.tool_usage.rollup_batch = 10
        async with pool.acquire() as c:
            await _seed(c, days=0, tool="akb_batch", n=25)
        n1 = await tool_usage.rollup_once()
        check("T10 one statement is bounded", n1 == 10, f"folded={n1}")
    finally:
        settings.tool_usage.rollup_batch = original

    await _reset(pool)
    await close_pool()
    print(f"\n{PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
