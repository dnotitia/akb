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
import hashlib
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple

from app.config import settings
from asyncpg.exceptions import DataError

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
# Consecutive failed flushes. Not used to drop anything — only to make a
# permanently failing environment distinguishable from an idle queue on /health.
_flush_failures = 0


def reset() -> None:
    """(Re)build the queue at the configured bound and clear the drop count.

    Called at startup and by tests; the bound is read here rather than per
    append so the hot path stays a single ``append``.
    """
    global _queue, _dropped, _maxlen, _flush_failures
    _maxlen = settings.tool_usage.queue_max
    _queue = deque(maxlen=_maxlen)
    _dropped = 0
    _flush_failures = 0


reset()


def queue_depth() -> int:
    return len(_queue)


def dropped_count() -> int:
    """Records lost to overflow or to a failed flush that could not be
    requeued, since the last ``reset()``. Counted so a flood shows up as a
    reported degradation instead of a silent gap."""
    return _dropped


def stats() -> dict:
    """Operational snapshot for `/health`.

    Counting losses is only half the contract — `audit_log.stats()` is on
    `/health` for exactly this reason. Without an endpoint the numbers live
    only in a rate-limited log line, so an overflow or a systematic recording
    failure is invisible to anyone watching a dashboard.
    """
    if not settings.tool_usage.enabled:
        # The maintenance runner keeps folding and pruning while collection is
        # off, so its failures have to stay visible here too.
        return {
            "enabled": False,
            "queued": len(_queue),
            "lost": _dropped,
            "maintenance_failures": dict(_leg_failures),
            "abandoned_workers": _abandoned(),
        }
    return {
        "enabled": True,
        "queued": len(_queue),
        "queue_max": _maxlen,
        "lost": _dropped,
        "consecutive_flush_failures": _flush_failures,
        "maintenance_failures": dict(_leg_failures),
        "retention_days": settings.tool_usage.raw_retention_days,
    }


def _abandoned() -> dict[str, int]:
    """Workers left dead by an iteration that ignored cancellation.

    `BackfillRunner.start()` refuses to run a second loop over a queue an old
    iteration may still be writing to, which is right — but it means one bad
    shutdown silently ends that worker for the process lifetime. Anything
    non-zero here is a restart-required condition.
    """
    return {
        name: n for name, r in (("flusher", _flusher), ("maintenance", _maintainer))
        if (n := r.abandoned())
    }


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
            tool=_clip(name) or "",
            actor_id=_clip(getattr(user, "user_id", None)),
            actor=_clip(getattr(user, "username", None)),
            session_id=_clip(session_id),
            vault=_clip(_vault_of(name, args)),
            outcome=outcome,
            code=_clip(code),
            duration_ms=duration_ms,
            is_write=is_write,
        ))
    except Exception as e:  # noqa: BLE001 — tracking must never fail a tool call
        # Never raising is right; being silent about WHY nothing was recorded
        # is not. A regression that makes this raise for every call would
        # otherwise present as an empty table with `dropped=0` and `depth=0` —
        # every signal reading "healthy, no traffic".
        _dropped += 1
        if _dropped == 1 or _dropped % 1000 == 0:
            logger.warning("tool_usage.record failed (%d lost so far): %s", _dropped, e)


# Every string column here is caller-influenced: `tool` is the raw JSON-RPC
# method name (the SDK forwards unknown names too), `vault` and `session_id`
# come straight off the wire, and `code` comes from a handler's error envelope.
# They land in a table on the volume that also holds the source-of-truth
# Postgres, so none of them may be unbounded — the sibling audit sink clips its
# lifted argument at `_TARGET_MAX` for the same reason.
#
# `tool` in particular becomes part of `tool_usage_daily`'s btree PRIMARY KEY,
# where a value past ~2704 bytes raises "index row size exceeds btree maximum".
# The claim and the aggregate are one statement, so that rolls the entire
# rollup back and the offending row is re-selected every tick forever: folding
# stops, and because purge requires the fold stamp, retention stops with it.
_STR_MAX = 256
# Hex chars of digest kept on truncation. 16 = 64 bits: an 8-char (32-bit)
# suffix is fine against accident but cheap to collide on purpose, and `tool`
# is part of `tool_usage_daily`'s PRIMARY KEY.
_DIGEST = 16
# Ceiling on how much of an oversized value is encoded and hashed. Both scan
# linearly and both run on the request path — measured 0.6ms/1MB, 58ms/100MB,
# which is the event-loop stall class this service dies of. Sampling a bounded
# prefix keeps the cost flat regardless of what a caller sends.
_SCAN_MAX = 4096


def _clip(v: Any) -> str | None:
    """Coerce to a value PostgreSQL TEXT can store, bounded at `_STR_MAX`.

    Two shapes are unstorable, not merely oversized: NUL, and lone surrogates
    (which survive a Python str but cannot be encoded as UTF-8). Either one
    fails the batched INSERT, and a failed batch returns to the head of the
    deque — so one such value would stall every row behind it.

    Truncation carries a digest of the original rather than a bare ellipsis:
    `tool` is part of `tool_usage_daily`'s PRIMARY KEY, so two different
    overlong values sharing a prefix would otherwise merge into one aggregate
    row and silently sum unrelated traffic.
    """
    if v is None:
        return None
    s = v if isinstance(v, str) else str(v)
    oversized = len(s) > _STR_MAX
    # Slice BEFORE encoding/hashing so neither scan is attacker-scaled.
    sample = s[:_SCAN_MAX] if len(s) > _SCAN_MAX else s
    sample = sample.encode("utf-8", "replace").decode("utf-8").replace("\x00", "")
    if not oversized:
        return sample
    digest = hashlib.sha256(sample.encode("utf-8")).hexdigest()[:_DIGEST]
    return sample[: _STR_MAX - _DIGEST - 1] + "\u2026" + digest


# Resource tools address their target by `akb://` URI rather than a separate
# vault argument — the canonical API dropped the redundant parameter — so 20 of
# the 43 tools (`akb_get`, `akb_update`, `akb_edit`, `akb_delete`, `akb_move`,
# `akb_link`, …) carry no `vault` key at all. Reading only `args["vault"]` would
# leave the dimension NULL on most rows.
#
# Mirrors the URI-bearing argument names in `mcp_server/tools.py`; the scheme
# itself is parsed by `uri_service`, which owns that grammar.
_URI_ARGS = ("uri", "parent", "source", "target")
# A real `akb://` URI is short. Parsing is regex work on the request path, so
# an argument past this is rejected without parsing — four 1 MB strings measured
# ~45ms of event-loop time, which is the stall class this service dies of.
_URI_ARG_MAX = 2048


def _scalar_vault(args: dict) -> str | None:
    v = args.get("vault")
    return v if isinstance(v, str) and v else None


def _vault_of(name: str, args: Any) -> str | None:
    """The vault the handler will actually operate on.

    Attribution has to follow each tool's own precedence, not one global rule,
    or the row names a vault the call never touched:

    * `akb_sql` reads `vaults or [vault]` (`server.py`), so the array wins. A
      genuinely multi-vault statement has no single target — recording its
      first element would present one arbitrarily-ordered vault as *the*
      vault, so that case is left NULL.
    * `akb_publish(resource_type="table_query")` takes the scalar `vault` and
      ignores any `uri`, even though the schema accepts both.
    * Everything else addresses its target by URI; `_handle_browse`,
      `_handle_graph` and `_resolve_parent` all ignore a `vault` passed
      alongside one.
    """
    if not isinstance(args, dict):
        return None

    if name == "akb_sql":
        many = [v for v in (args.get("vaults") or []) if isinstance(v, str) and v]
        if len(many) == 1:
            return many[0]
        if many:
            return None
        return _scalar_vault(args)

    if name == "akb_publish" and args.get("resource_type") == "table_query":
        return _scalar_vault(args)

    if name == "akb_unpublish" and args.get("slug"):
        # `_handle_unpublish` resolves the vault from the publication row when
        # a slug is given and never looks at `uri`, though the schema accepts
        # both. The vault is only knowable by a DB lookup, which this path must
        # not do — NULL is the honest answer.
        return None

    for key in _URI_ARGS:
        value = args.get(key)
        if isinstance(value, str) and len(value) <= _URI_ARG_MAX:
            found = vault_of(value)
            if found:
                return found
    return _scalar_vault(args)


async def flush_once() -> int:
    """Drain up to ``flush_batch`` records into PG in one round trip.

    A transient failure returns the batch to the FRONT of the queue so an
    outage delays rows instead of losing them; the queue bound caps how much a
    sustained outage can retain, and whatever does not fit is counted.
    """
    global _dropped, _flush_failures
    batch = drain(settings.tool_usage.flush_batch)
    if not batch:
        return 0
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            # `DataError` is only meaningful for the INSERT. Raised by
            # `get_pool()` or `acquire()` it would say nothing about the rows,
            # so the classification lives strictly around the statement.
            written, lost = await _insert_isolating_poison(conn, batch)
    except asyncio.CancelledError:
        # `CancelledError` is a BaseException, so the handler below never sees
        # it. The shutdown deadline firing mid-INSERT would otherwise leave
        # these rows nowhere at all — already popped by `drain()`, never
        # inserted, never requeued, never counted — and `stop()`'s
        # queue-depth check would report a clean shutdown.
        _requeue_front(batch)
        raise
    except Exception as e:  # noqa: BLE001
        # Connection reset, pool exhausted, failover, a missing table, a
        # missing grant — none of these are a property of the rows, and all of
        # them become correct again once the environment does. An earlier
        # version dropped after three consecutive failures of ANY kind, which
        # turned an ordinary 10-15s PostgreSQL restart into deterministic loss.
        # A permanent environment fault therefore retries indefinitely; it
        # degrades rather than wedges, because the queue is bounded, every
        # eviction is counted, and `_flush_failures` makes the difference
        # between "retrying" and "idle" visible on /health.
        _flush_failures += 1
        kept = _requeue_front(batch)
        if _flush_failures == 1 or _flush_failures % 60 == 0:
            logger.warning(
                "tool_usage flush failing (%d consecutive; %d requeued, %d dropped): %s",
                _flush_failures, kept, len(batch) - kept, e,
            )
        return 0
    _flush_failures = 0
    if lost:
        _dropped += lost
        logger.error("tool_usage dropped %d unstorable record(s)", lost)
    return written


# Statements one flush may spend isolating unstorable rows. A clean batch costs
# 1 and a single bad row about 17, but a wholly unstorable batch would cost
# 2n-1 — 999 round trips for 500 rows, seconds of a held connection, on every
# tick for as long as the cause persists. Past the budget the remainder is
# dropped wholesale: precision where it is cheap, a bound where it is not.
_BISECT_BUDGET = 64


async def _insert_isolating_poison(
    conn, batch: list[_Row], budget: list[int] | None = None,
) -> tuple[int, int]:
    """Insert `batch`, bisecting around rows PostgreSQL will never accept.

    Returns ``(written, dropped)``. A class-22 failure (invalid byte sequence,
    value too long, …) belongs to ONE row, but `executemany` is atomic, so
    failing the whole call would discard up to `flush_batch` perfectly valid
    siblings. Halving isolates the offender on the error path only.
    """
    if budget is None:
        budget = [_BISECT_BUDGET]
    if budget[0] <= 0:
        return 0, len(batch)
    budget[0] -= 1
    try:
        await conn.executemany(_INSERT_SQL, batch)
        return len(batch), 0
    except DataError:
        if len(batch) == 1:
            return 0, 1
    mid = len(batch) // 2
    left = await _insert_isolating_poison(conn, batch[:mid], budget)
    right = await _insert_isolating_poison(conn, batch[mid:], budget)
    return left[0] + right[0], left[1] + right[1]


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

    Each leg is attempted and accounted for independently. Sequenced without
    isolation, a purge error after a successful 5,000-row fold would discard
    the rollup's count as well: `BackfillRunner` turns the exception into
    `done = 0` and then sleeps the whole idle interval — an hour — so
    throughput collapses to one batch per hour while the other leg keeps
    failing. The mirror case is worse: a rollup error would stop purge running
    at all, restoring the starvation this function exists to prevent.
    """
    return await _leg(rollup_once, "rollup") + await _leg(purge_once, "purge")


_leg_failures: dict[str, int] = {"rollup": 0, "purge": 0}


async def _leg(fn, name: str) -> int:
    """Run one maintenance leg; its failure must not veto the other.

    Rate-limited with a traceback, and counted. Logging the bare message every
    tick turns a permanently broken leg into a warning storm — and because the
    surviving leg keeps reporting work, the runner re-runs immediately rather
    than waiting out the hourly idle interval, so "every tick" is fast. The
    count is surfaced on `/health` so a leg that is quietly always failing is
    visible without grepping.
    """
    try:
        n = await fn()
    except Exception as e:  # noqa: BLE001 — one leg must not veto the other
        _leg_failures[name] += 1
        count = _leg_failures[name]
        if count == 1 or count % 100 == 0:
            logger.warning(
                "tool_usage %s failed (%d consecutive): %s", name, count, e,
                exc_info=True,
            )
        return 0
    _leg_failures[name] = 0
    return n


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
    # NOTHING here may propagate. `lifecycle.stop_workers()` awaits each
    # worker's `stop()` in sequence, so one exception skips every worker after
    # it — observed live when a config field this function reads was missing:
    # `AttributeError` escaped and events_publisher, metadata_worker,
    # embed_worker and the rest were never stopped. The phases below are each
    # guarded; this outer guard covers everything around them, including
    # reading the budget itself.
    try:
        budget = settings.tool_usage.shutdown_deadline_secs
        # Each phase is bounded and each is *reached*. Wrapping the whole
        # sequence in one `wait_for` meant an expiry during the drain skipped
        # `_maintainer.stop()` entirely — its stop event never set, its task
        # never awaited — leaving a shielded rollup running against a pool that
        # `close_pool()` was about to tear down.
        await _phase(_flusher.stop(timeout=budget / 2), "flusher stop")
        await _phase(_drain_all(), "final drain", timeout=budget / 4)
        await _phase(_maintainer.stop(timeout=budget / 4), "maintenance stop")
        if queue_depth():
            logger.warning(
                "tool_usage lost %d queued record(s) at shutdown (drain incomplete)",
                queue_depth(),
            )
    except Exception as e:  # noqa: BLE001 — must not abort the shutdown chain
        logger.warning("tool_usage shutdown aborted: %s", e)


async def _drain_all() -> None:
    while await flush_once():
        pass


async def _phase(coro, what: str, timeout: float | None = None) -> None:
    """Run one shutdown phase; never raise, never let it run past its bound."""
    try:
        await (asyncio.wait_for(coro, timeout=timeout) if timeout else coro)
    except asyncio.TimeoutError:
        logger.warning("tool_usage %s exceeded its shutdown budget", what)
    except Exception as e:  # noqa: BLE001 — shutdown must not raise
        logger.warning("tool_usage %s failed: %s", what, e)
