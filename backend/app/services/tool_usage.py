"""MCP tool-usage tracking — per-call rows in PG, rolled up daily.

**Why a third stream.** AKB already emits two, and neither can answer "which
MCP tool is actually used":

* ``events`` (``events_repo.emit_event``) records *domain verbs* on successful
  writes for Redis fanout. It is not a chokepoint — 38 hand-written call sites
  covering 30 kinds — and has no ``publication.*`` / ``edge.*`` /
  ``vault.create|delete|archive`` / export / sql kinds at all, so at least 29 of
  the 43 MCP tools can never appear in it. Rows are also DELETEd once delivered.
* ``audit_log`` observes the right place (the dispatch chokepoint) but writes an
  append-only, hash-chained JSONL file for a SIEM. It cannot be grouped, and
  ``audit.log_reads=false`` drops read-classified tools — the coupling that hid
  ``akb_grep(replace=)`` from the audit trail until 0.9.x. Read calls are most
  of the usage signal, so that flag must not gate this sink.

``record_tool``'s own docstring already draws this line between ``events`` and
``audit`` ("different altitudes … do not try to unify the two"); this applies it
one step further. Design, schema and rejected alternatives:
``docs/design/proposal/2026-07-28-mcp-tool-usage-tracking/README.md``.

**Why the queue.** ``record()`` runs on the single event loop that serves every
request. This service's dominant outage class is loop stalls (503), so the
recording path may only append to a bounded in-memory deque; a background
``BackfillRunner`` does the batched INSERT off the request path. On overflow the
oldest entries are evicted and **counted** — this codebase has a habit of
narrowing results silently, and a usage table with an unreported hole is worse
than no table.

**Never raises.** Tracking a call must not be able to fail that call.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple

from app.config import settings
from app.db.postgres import get_pool
from app.services._backfill import BackfillRunner
from app.services.uri_service import vault_of

logger = logging.getLogger("akb.tool_usage")

_INSERT_SQL = """
    INSERT INTO tool_calls
        (occurred_at, tool, actor_id, actor, session_id, vault,
         outcome, code, duration_ms, is_write)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
"""

# Claim-and-fold, in ONE statement. Each row is stamped `rolled_at` by the same
# statement that counts it, so the aggregate and the claim commit or roll back
# together and a row is folded exactly once — for any number of concurrent
# writers or rollup runners.
#
# Why not a `MAX(id)` high-water mark (the previous design): Postgres allocates
# sequence values BEFORE commit, so a watermark can advance past a lower id
# whose transaction is still open. That row then sits permanently below the
# watermark — never aggregated — and the purge predicate happily deletes it.
# The single-flusher, single-process topology hides this, but nothing enforces
# that topology, so the invariant would have been true only by accident.
#
# `FOR UPDATE SKIP LOCKED` lets a second runner take a different slice instead
# of blocking, and `LIMIT` bounds the transaction so a long backlog is caught up
# in steady chunks rather than one statement-timeout-sized burst.
_ROLLUP_SQL = """
    WITH claimed AS (
        UPDATE tool_calls SET rolled_at = NOW()
         WHERE id IN (
             SELECT id FROM tool_calls
              WHERE rolled_at IS NULL
              ORDER BY id
              LIMIT $1
              FOR UPDATE SKIP LOCKED
         )
        RETURNING (occurred_at AT TIME ZONE 'UTC')::date AS day,
                  tool, outcome, duration_ms
    ), agg AS (
        SELECT day, tool, outcome,
               COUNT(*) AS calls, COALESCE(SUM(duration_ms), 0) AS total_ms
          FROM claimed GROUP BY day, tool, outcome
    ), ins AS (
        INSERT INTO tool_usage_daily (day, tool, outcome, calls, total_duration_ms)
        SELECT day, tool, outcome, calls, total_ms FROM agg
        ON CONFLICT (day, tool, outcome) DO UPDATE
           SET calls = tool_usage_daily.calls + EXCLUDED.calls,
               total_duration_ms = tool_usage_daily.total_duration_ms
                                 + EXCLUDED.total_duration_ms
        RETURNING 1
    )
    SELECT COUNT(*) FROM claimed
"""

# Raw rows are the only copy until they are folded in, so the delete requires
# the claim stamp as well as age: an old but unfolded row survives. `LIMIT`
# keeps a large backlog from becoming one huge DELETE (WAL burst, statement
# timeout, bloat spike).
#
# Deliberately NOT ordered. `ORDER BY id` here makes Postgres materialise every
# eligible row and top-N sort it just to take `LIMIT` of them: measured at ~500k
# eligible rows, 755ms / 49,610 buffers per batch versus 10.7ms / 172 without,
# and the waste is quadratic across a full pass. Order buys nothing — the
# predicate already selects only eligible rows, and `ctid` lets the delete skip
# a second primary-key probe per row.
_PURGE_SQL = """
    WITH d AS (
        DELETE FROM tool_calls
         WHERE ctid IN (
             SELECT ctid FROM tool_calls
              WHERE occurred_at < $1
                AND rolled_at IS NOT NULL
              LIMIT $2
         )
        RETURNING 1
    )
    SELECT COUNT(*) FROM d
"""

class _Row(NamedTuple):
    """One queued call, already in `_INSERT_SQL` column order.

    A tuple rather than a dict: asyncpg wants positional parameters anyway, so
    the dict was a pure intermediate that cost ~2x the memory per queued row
    and forced the column order to be restated in a third place.
    """
    occurred_at: datetime
    tool: str
    actor_id: str | None
    actor: str | None
    session_id: str | None
    vault: str | None
    outcome: str
    code: str | None
    duration_ms: int | None
    is_write: bool


# The bound is kept alongside the deque so the hot path compares against a
# plain int instead of re-deriving `deque.maxlen` (typed `int | None`, which
# needs a guard that can never fire).
_maxlen: int = 1
_queue: deque[_Row] = deque(maxlen=_maxlen)
_dropped = 0


def reset() -> None:
    """(Re)build the queue at the configured bound and clear the drop count.

    Called at startup and by tests; the bound is read here rather than per
    append so the hot path stays a single ``append``.
    """
    global _queue, _dropped, _maxlen
    _maxlen = settings.tool_usage.queue_max
    _queue = deque(maxlen=_maxlen)
    _dropped = 0


reset()


def queue_depth() -> int:
    return len(_queue)


def dropped_count() -> int:
    """Records lost to overflow or to a failed flush that could not be
    requeued, since the last ``reset()``. Counted so a flood shows up as a
    reported degradation instead of a silent gap."""
    return _dropped


def drain(limit: int) -> list[_Row]:
    if len(_queue) <= limit:
        out = list(_queue)
        _queue.clear()
        return out
    return [_queue.popleft() for _ in range(limit)]


def record(
    name: str,
    args: dict | None,
    user: Any,
    result: Any,
    *,
    session_id: str | None = None,
    duration_ms: int | None = None,
    is_write: bool = False,
) -> None:
    """Enqueue one MCP tool call. Synchronous, allocation-only, never raises.

    Deliberately mirrors ``audit_log.record_tool``'s parameters so both can be
    wired from the same dispatch chokepoint without the two disagreeing about
    who called what.

    Raw ``args`` are NOT stored — they carry document bodies, search queries and
    SQL. Only ``vault`` is lifted out, the same "honest, lossy" choice
    ``audit_log`` made for its ``target``.
    """
    global _dropped
    try:
        if not settings.tool_usage.enabled:
            return

        outcome, code = "ok", None
        if isinstance(result, dict) and (result.get("error") is not None or result.get("code")):
            outcome = "error"
            code = result.get("code")

        if len(_queue) >= _maxlen:
            # deque evicts the oldest on append; count it so the loss is
            # reportable rather than invisible.
            _dropped += 1
            if _dropped == 1 or _dropped % 1000 == 0:
                logger.warning(
                    "tool_usage queue full (maxlen=%d) — dropped %d record(s); "
                    "the flusher is not keeping up",
                    _maxlen, _dropped,
                )
        _queue.append(_Row(
            occurred_at=datetime.now(timezone.utc),
            tool=name,
            actor_id=_str_or_none(getattr(user, "user_id", None)),
            actor=_str_or_none(getattr(user, "username", None)),
            session_id=session_id,
            vault=_vault_of(args),
            outcome=outcome,
            code=code,
            duration_ms=duration_ms,
            is_write=is_write,
        ))
    except Exception as e:  # noqa: BLE001 — tracking must never fail a tool call
        logger.debug("tool_usage.record skipped: %s", e)


def _str_or_none(v: Any) -> str | None:
    return None if v is None else str(v)


# Resource tools address their target by `akb://` URI rather than a separate
# vault argument — the canonical API dropped the redundant parameter — so 20 of
# the 43 tools (`akb_get`, `akb_update`, `akb_edit`, `akb_delete`, `akb_move`,
# `akb_link`, …) carry no `vault` key at all. Reading only `args["vault"]` would
# leave the dimension NULL on most rows.
#
# Mirrors the URI-bearing argument names in `mcp_server/tools.py`; the scheme
# itself is parsed by `uri_service`, which owns that grammar.
_URI_ARGS = ("uri", "parent", "source", "target")


def _vault_of(args: Any) -> str | None:
    """Vault name from an explicit argument, else the first `akb://` URI."""
    if not isinstance(args, dict):
        return None
    explicit = args.get("vault")
    if isinstance(explicit, str) and explicit:
        return explicit
    for key in _URI_ARGS:
        value = args.get(key)
        if isinstance(value, str):
            name = vault_of(value)
            if name:
                return name
    return None


async def flush_once() -> int:
    """Drain up to ``flush_batch`` records into PG in one round trip.

    On a DB error the batch is returned to the FRONT of the queue so a
    transient outage delays rows instead of losing them; the bound caps how
    much a sustained outage can retain, and whatever does not fit is counted.
    """
    batch = drain(settings.tool_usage.flush_batch)
    if not batch:
        return 0
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.executemany(_INSERT_SQL, batch)
    except Exception as e:  # noqa: BLE001
        kept = _requeue_front(batch)
        logger.warning(
            "tool_usage flush failed (%d requeued, %d dropped): %s",
            kept, len(batch) - kept, e,
        )
        return 0
    return len(batch)


def _requeue_front(batch: list[_Row]) -> int:
    """Put a failed batch back at the head. Returns how many actually fit.

    New calls may have taken the freed capacity while the flush was in flight,
    so this can only keep what the bound allows; the remainder is counted as
    dropped rather than reported as requeued.
    """
    global _dropped
    room = _maxlen - len(_queue)
    if room < len(batch):
        _dropped += len(batch) - room
        batch = batch[len(batch) - room:] if room > 0 else []
    _queue.extendleft(reversed(batch))
    return len(batch)


def _purge_cutoff(now: datetime | None = None) -> datetime:
    """Midnight UTC, ``raw_retention_days`` back — a whole-day boundary.

    Truncating to the day keeps a retention pass from splitting a day across
    runs, so "this day is gone" is never half-true for a reader joining raw
    rows against the aggregate. The ``now`` argument exists so the boundary can
    be asserted deterministically in tests.
    """
    now = now or datetime.now(timezone.utc)
    day = (now - timedelta(days=settings.tool_usage.raw_retention_days)).date()
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


async def rollup_once() -> int:
    """Claim and fold one bounded batch of unaggregated rows.

    Returns how many raw rows were folded. A non-zero return makes the runner
    loop again immediately, so a backlog is caught up in steady chunks instead
    of one oversized transaction. Rows already purged were claimed first, so
    aggregates outlive their sources.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval(_ROLLUP_SQL, settings.tool_usage.maintenance_batch)
    return int(n or 0)


async def purge_once() -> int:
    """Drop raw rows past retention that carry a claim stamp.

    An unfolded row is never deleted however old it is, because the aggregate
    that would replace it does not exist yet. Unlike the ``events`` outbox —
    whose purge lives inside the publisher and stops the moment its transport
    is unconfigured, letting the table grow forever — this runs independently
    of ``enabled`` so disabling collection still prunes.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            _PURGE_SQL, _purge_cutoff(), settings.tool_usage.maintenance_batch
        )
    n = int(n or 0)
    if n:
        logger.info("tool_usage purge: removed %d raw row(s)", n)
    return n


async def maintenance_once() -> int:
    """`BackfillRunner` callback: fold a batch, then prune a batch.

    Both run every tick. An earlier version returned early whenever the rollup
    had folded anything, on the theory that purge should wait for a globally
    quiet moment — but sustained traffic never produces one, so purge could be
    starved indefinitely while already-aggregated rows piled up. The wait was
    never needed: `rolled_at IS NOT NULL` makes each row individually safe to
    delete regardless of what else is still unfolded.
    """
    folded = await rollup_once()
    purged = await purge_once()
    return folded + purged


# ── Workers ─────────────────────────────────────────────────────

_flusher = BackfillRunner(
    "tool_usage_flusher", flush_once,
    idle_secs=settings.tool_usage.flush_interval_secs,
    # This one has work on every tick the service has traffic; the runner's
    # per-tick progress line would be thousands of INFO lines a day. Overflow
    # and flush failures still log — those are the ones worth seeing.
    log_progress=False,
)
# Deliberately a SEPARATE runner from the flusher, and started regardless of
# `enabled`: the `events` outbox puts its purge inside the publisher, so
# clearing `redis_url` stops publishing AND pruning and the table grows without
# bound. Turning tracking off here must still drain and prune what was already
# collected.
_maintainer = BackfillRunner(
    "tool_usage_maintenance", maintenance_once,
    idle_secs=settings.tool_usage.rollup_interval_secs,
)


def start() -> None:
    reset()
    if settings.tool_usage.enabled:
        _flusher.start()
    _maintainer.start()


async def stop() -> None:
    """Quiesce the flusher, drain the remainder, then stop maintenance.

    Order matters in both directions. Draining *first* races the live flusher:
    it may already own a batch, so the final drain sees an empty queue, and if
    that in-flight INSERT then fails it requeues after we have stopped looking
    — the tail is lost with no warning. Stopping first removes the other
    claimant, so whatever remains in the deque is ours alone.

    Everything is under one deadline. `BackfillRunner.stop()` defaults to
    waiting 120s for an in-flight iteration, but Kubernetes grants 30s and the
    all-in-one supervisor 15s, so an unbounded shutdown is simply SIGKILLed
    mid-drain. Anything still queued when the budget runs out is logged rather
    than dropped in silence.
    """
    budget = settings.tool_usage.shutdown_deadline_secs
    try:
        await asyncio.wait_for(_shutdown_sequence(budget), timeout=budget)
    except asyncio.TimeoutError:
        logger.warning("tool_usage shutdown exceeded %.1fs budget", budget)
    except Exception as e:  # noqa: BLE001 — shutdown must not raise
        logger.warning("tool_usage shutdown failed: %s", e)
    if queue_depth():
        logger.warning(
            "tool_usage lost %d queued record(s) at shutdown (drain incomplete)",
            queue_depth(),
        )


async def _shutdown_sequence(budget: float) -> None:
    # Half the budget for the flusher to finish its iteration; the rest for the
    # drain and the maintenance stop.
    await _flusher.stop(timeout=budget / 2)
    while await flush_once():
        pass
    await _maintainer.stop(timeout=budget / 4)
