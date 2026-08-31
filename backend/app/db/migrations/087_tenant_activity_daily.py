"""Migration 087: finalized daily activity windows for the `/stats` surface.

`/stats` reports call volume for the **previous complete UTC day**. That value
is a *closed fact*: once a day is folded it must never be recomputed, because
the consumer (the control plane's loader) keeps the first value it observes for
a window and a differing second answer would silently contradict a series it
has already stored. A snapshot held only in process memory cannot promise that
— a restart would recompute the same window from whatever `tool_calls` still
holds and could easily produce a different number (retention purge, a late
flush of the in-memory queue, tracking toggled off).

So the fold is persisted, keyed on the day, and inserted with
`ON CONFLICT (day) DO NOTHING`. The first writer to close a day wins forever;
every later process serves that row verbatim.

The counts are **nullable on purpose**. A day whose volume could not be
computed — MCP usage tracking was off, so `tool_calls` holds nothing for it —
is recorded as a closed window with unknown counts, which is a different fact
from a closed window with zero calls. Defaulting those to 0 would publish a
fabricated number that can never be corrected, since the row is final.

One row per day (~365/year); no retention worker.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("akb.migration.087")


async def migrate(conn) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tenant_activity_daily (
            -- The UTC calendar day the window covers. PRIMARY KEY is what
            -- makes "fold a day exactly once, forever" enforceable by the
            -- database rather than by whichever process happened to run.
            day            DATE        PRIMARY KEY,
            -- Stored rather than derived from `day` so the served window is
            -- exactly the window that was counted, even if a future change
            -- alters how the boundary is chosen.
            window_start   TIMESTAMPTZ NOT NULL,
            window_end     TIMESTAMPTZ NOT NULL,
            -- NULL = the window closed but its volume was not computable.
            -- Not 0: see the module docstring.
            calls_read     BIGINT,
            calls_write    BIGINT,
            active_actors  BIGINT,
            computed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    logger.info("Migration 087 created tenant_activity_daily")
