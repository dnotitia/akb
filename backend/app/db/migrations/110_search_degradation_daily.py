"""Migration 110: search degradation accounting — `search_degradation_daily`
and `search_degradation_cause_daily`.

Nothing counted how often a search response came back `degraded`, or why.
`/health` reports a section per indexing queue and none of them is search; the
word `degraded` does appear there, but as the vector store's *reachability*
verdict, a different namespace from the search response field.
`tool_usage_daily` cannot answer it either — a degraded search is a successful
call and lands under `ok` beside every healthy one. The only trace was a log
line, which lives as long as the pod does, and deploys here are frequent.

Two tables because two different things are being counted, and folding them
into one would need a sentinel cause meaning "not degraded":

  * `search_degradation_daily` — one row per day holding the denominators:
    every response observed, how many were degraded, and how many of THOSE
    still carried results. That last number is the one the consumer question
    turns on (should a surface show partial results?) and the one no existing
    surface could ever answer.
  * `search_degradation_cause_daily` — one row per (day, cause) holding the
    breakdown. An aggregate that says only "3% degraded" cannot separate a
    write race from a retrieval outage, and those call for opposite responses.

`degraded` is NOT the sum of the cause rows. A response whose reason names
several hydration causes contributes to each of them, so the breakdown sums to
at least the degraded count — which is why the total is stored rather than
derived.

No index beyond the primary keys, and no purge. Both tables are written once
per flush tick per day, not once per response, and the whole corpus of causes
is a closed set of six plus a handful of retrieval-leg reasons — single-digit
rows per day, kept indefinitely like `tool_usage_daily`.

Idempotent: `CREATE TABLE IF NOT EXISTS`, so re-running is a no-op.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db

logger = logging.getLogger("akb.migration.110")

DDL = """
CREATE TABLE IF NOT EXISTS search_degradation_daily (
    day                   date   PRIMARY KEY,
    observed              bigint NOT NULL DEFAULT 0,
    degraded              bigint NOT NULL DEFAULT 0,
    degraded_with_results bigint NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS search_degradation_cause_daily (
    day       date   NOT NULL,
    cause     text   NOT NULL,
    responses bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (day, cause)
);
"""


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn) -> None:
    await conn.execute(DDL)
    logger.info(
        "Migration 110 created search_degradation_daily + "
        "search_degradation_cause_daily"
    )


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        await init_db()
        await migrate()
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
