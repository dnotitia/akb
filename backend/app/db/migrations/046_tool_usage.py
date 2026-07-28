"""Migration 046: MCP tool-usage tracking — `tool_calls` + `tool_usage_daily`.

AKB could not answer "which MCP tool is actually used". `events` records domain
verbs on successful writes only (at least 29 of the 43 tools have no
corresponding `kind` and can never appear) and is DELETEd once delivered to
Redis; `audit_log` observes the right chokepoint but writes a hash-chained JSONL
file for a SIEM, which cannot be grouped. This adds a third, queryable sink.

`tool_calls` holds one row per MCP tool call (reads, writes and failures) and is
pruned after `tool_usage.raw_retention_days`; `tool_usage_daily` holds the
aggregate and is kept indefinitely (~86 rows/day). Purge is predicated on a
day having been rolled up, so raw rows are never dropped before the aggregate
that replaces them exists.

Indexes are deliberately sparse — this is an append-heavy table:
  * BRIN on `occurred_at`: a few pages instead of a btree over millions of rows,
    and the access patterns (retention purge, rollup window, time-range reports)
    are all correlated ranges over insertion order.
  * partial btree on `(session_id, id)`: the agent-behaviour question ("what did
    this conversation call, in order") is a point lookup that BRIN cannot serve.
No index on `tool`/`actor` — those are answered from `tool_usage_daily`, and
every extra index taxes the insert path.

Design: `docs/design/proposal/2026-07-28-mcp-tool-usage-tracking/README.md`.

Idempotent: `CREATE TABLE/INDEX IF NOT EXISTS`, so re-running is a no-op.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db

logger = logging.getLogger("akb.migration.046")


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    async with conn.transaction():
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tool_calls (
                id           BIGSERIAL PRIMARY KEY,
                occurred_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                tool         TEXT        NOT NULL,
                actor_id     TEXT,
                actor        TEXT,
                session_id   TEXT,
                vault        TEXT,
                outcome      TEXT        NOT NULL,
                code         TEXT,
                duration_ms  INTEGER,
                is_write     BOOLEAN     NOT NULL DEFAULT FALSE,
                -- NULL until the row has been folded into `tool_usage_daily`.
                -- Per-row claim state rather than a sequence high-water mark:
                -- Postgres allocates ids BEFORE commit, so a watermark taken as
                -- MAX(id) can advance past a lower id whose transaction is still
                -- open, permanently skipping that row and then authorising its
                -- purge. Claiming each row in the same statement that aggregates
                -- it is exactly-once for any number of concurrent writers.
                rolled_at    TIMESTAMPTZ
            )
            """
        )
        # The claim scan only ever looks at unfolded rows, so a partial index
        # keeps it proportional to the backlog rather than to the table.
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tool_calls_unrolled
                ON tool_calls (id) WHERE rolled_at IS NULL
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tool_calls_occurred_brin
                ON tool_calls USING BRIN (occurred_at)
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tool_calls_session
                ON tool_calls (session_id, id)
             WHERE session_id IS NOT NULL
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tool_usage_daily (
                day               DATE   NOT NULL,
                tool              TEXT   NOT NULL,
                outcome           TEXT   NOT NULL,
                calls             BIGINT NOT NULL,
                total_duration_ms BIGINT NOT NULL DEFAULT 0,
                PRIMARY KEY (day, tool, outcome)
            )
            """
        )
        # No watermark table: claim state lives on the row itself
        # (`tool_calls.rolled_at`), which is what makes the rollup correct
        # without assuming a single inserter.

    logger.info(
        "Migration 046 created tool_calls + tool_usage_daily "
        "(inert until tool_usage.enabled=true)"
    )


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
