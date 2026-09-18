"""Search degradation accounting — how often a search response is degraded,
why, and whether the caller still got results (akb#612).

**Why it did not exist.** Three surfaces looked like they should answer this
and none of them can. `/health` reports a section per indexing queue and search
is not a queue; the word `degraded` does appear there, but as the vector
store's *reachability* verdict — a different namespace from the search response
field. `tool_usage_daily` records `(day, tool, outcome, calls, duration)`, and a
degraded search is a successful call: it lands under `ok` beside every healthy
one, so the table cannot tell them apart. The only remaining trace is a log
line, which lives as long as the pod does.

**Why those three numbers.** They are the ones a consumer's behaviour should be
chosen from, and it is being chosen without them today: one surface renders the
results with a warning, another withholds them entirely. Which is right depends
on how often the flag fires and whether results survive when it does, so
`degraded_with_results` is the field that matters most — and the one no
existing surface could ever have produced. The per-cause breakdown is the
second: a write race and a retrieval outage call for opposite responses, and an
aggregate that says only "3% degraded" separates neither.

**Shape.** Counts, never a verdict — the neighbouring `/health` sections report
numbers and let the reader judge, and this one has no business inventing a
second opinion about whether the service is healthy. It is deliberately absent
from `_aggregate_status`: a search degraded by a write race is not queue work
left undone.

**Cost.** `record()` runs on the single event loop that serves every request,
so it may only touch memory: one dict lookup and a handful of integer adds, no
allocation in the common (undegraded) case beyond the day key. The database
write is a per-day DELTA flushed on a timer off the request path — one small
UPSERT per tick, not one per response — which is what keeps this off a latency
budget that already spans a wide range.

**Durability.** In-process counters reset on every deploy and deploys here are
frequent, so the aggregate is a table. What is NOT durable is the delta between
flushes: a pod killed without running `stop()` loses up to one flush interval
of counts. That is stated rather than hidden — `pending_flush` in the snapshot
is exactly how much is at risk right now — and `stop()` drains before exit so
an ordinary rolling deploy loses nothing.

**Never raises.** Counting a response must not be able to fail that response.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from app.db.postgres import get_pool
from app.models.document import SearchResponse
from app.services._backfill import BackfillRunner
from app.services.search_service import degradation_causes

logger = logging.getLogger("akb.search_degradation")

# How often the in-memory delta is folded into the tables. Short enough that an
# ungraceful kill loses little, long enough that a busy deployment writes two
# rows a minute rather than two a second.
FLUSH_INTERVAL_SECS = 30

# Distinct cause names kept per day before the rest are folded into `other`.
# The causes are a closed internal set of well under ten, so this is never
# reached in practice — it exists so that a future cause derived from
# caller-influenced text cannot turn a counter into unbounded key growth.
# Overflow is FOLDED, not dropped: this codebase has a habit of narrowing
# results silently, and a counter with an unreported hole is worse than none.
MAX_CAUSES_PER_DAY = 64
OVERFLOW_CAUSE = "other"

# Whole shutdown budget, split evenly between quiescing the flusher and the
# final drain. Deliberately well inside the 15s the all-in-one supervisor
# grants, because this is one component among a dozen being stopped together.
SHUTDOWN_BUDGET_SECS = 6.0

_TOTALS_SQL = """
    INSERT INTO search_degradation_daily AS t
        (day, observed, degraded, degraded_with_results)
    VALUES ($1, $2, $3, $4)
    ON CONFLICT (day) DO UPDATE
       SET observed              = t.observed + EXCLUDED.observed,
           degraded              = t.degraded + EXCLUDED.degraded,
           degraded_with_results = t.degraded_with_results
                                 + EXCLUDED.degraded_with_results
"""

_CAUSES_SQL = """
    INSERT INTO search_degradation_cause_daily AS t (day, cause, responses)
    VALUES ($1, $2, $3)
    ON CONFLICT (day, cause) DO UPDATE
       SET responses = t.responses + EXCLUDED.responses
"""


@dataclass
class _Delta:
    """One day's un-flushed counts. Every field is a delta, never a total."""

    observed: int = 0
    degraded: int = 0
    degraded_with_results: int = 0
    by_cause: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def merge(self, other: "_Delta") -> None:
        self.observed += other.observed
        self.degraded += other.degraded
        self.degraded_with_results += other.degraded_with_results
        for cause, count in other.by_cause.items():
            self.by_cause[cause] += count


_pending: dict[date, _Delta] = {}
_flush_failures = 0


def _today() -> date:
    return datetime.now(timezone.utc).date()


def record(response: SearchResponse) -> None:
    """Count one search response. In-memory only, and never raises.

    Every response counts toward `observed`, including an empty one: the
    denominator is "searches that happened", and excluding the quiet ones would
    inflate every ratio built on it.
    """
    try:
        delta = _pending.setdefault(_today(), _Delta())
        delta.observed += 1
        if not response.degraded:
            return
        delta.degraded += 1
        if response.results:
            delta.degraded_with_results += 1
        for cause in degradation_causes(response.degradation_reason):
            if cause not in delta.by_cause and len(delta.by_cause) >= MAX_CAUSES_PER_DAY:
                cause = OVERFLOW_CAUSE
            delta.by_cause[cause] += 1
    except Exception:  # noqa: BLE001 — accounting must not fail a search
        logger.warning("search degradation accounting failed", exc_info=True)


def pending_depth() -> int:
    """Responses counted but not yet written. Zero right after a flush."""
    return sum(delta.observed for delta in _pending.values())


async def flush_once() -> int:
    """Fold every pending day into the tables. `BackfillRunner` callback.

    The pending map is taken whole and replaced with a fresh one, so a response
    recorded while the write is in flight lands in the next batch rather than
    being folded twice. On failure the claimed delta is merged BACK — losing a
    count to a transient database blip would put a silent hole in exactly the
    counter that exists to make holes visible.
    """
    global _flush_failures
    if not _pending:
        return 0
    claimed = dict(_pending)
    _pending.clear()
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                for day, delta in claimed.items():
                    await conn.execute(
                        _TOTALS_SQL, day, delta.observed, delta.degraded,
                        delta.degraded_with_results,
                    )
                    if delta.by_cause:
                        await conn.executemany(
                            _CAUSES_SQL,
                            [(day, c, n) for c, n in sorted(delta.by_cause.items())],
                        )
    except Exception:
        for day, delta in claimed.items():
            _pending.setdefault(day, _Delta()).merge(delta)
        _flush_failures += 1
        raise
    _flush_failures = 0
    return sum(delta.observed for delta in claimed.values())


async def snapshot() -> dict:
    """Today's counts for `/health`, durable rows plus the un-flushed delta.

    Folding the delta in is what makes the section answer a question asked
    seconds after a search instead of up to a flush interval later. In a
    multi-pod deployment the stored half already covers every pod; the delta is
    this one's, which is why `pending_flush` is reported beside the counts
    rather than silently baked into them.
    """
    day = _today()
    pool = await get_pool()
    async with pool.acquire() as conn:
        totals = await conn.fetchrow(
            "SELECT observed, degraded, degraded_with_results "
            "  FROM search_degradation_daily WHERE day = $1",
            day,
        )
        cause_rows = await conn.fetch(
            "SELECT cause, responses FROM search_degradation_cause_daily "
            " WHERE day = $1 ORDER BY cause",
            day,
        )
    observed = int(totals["observed"]) if totals else 0
    degraded = int(totals["degraded"]) if totals else 0
    with_results = int(totals["degraded_with_results"]) if totals else 0
    by_cause: dict[str, int] = {r["cause"]: int(r["responses"]) for r in cause_rows}

    delta = _pending.get(day)
    if delta is not None:
        observed += delta.observed
        degraded += delta.degraded
        with_results += delta.degraded_with_results
        for cause, count in delta.by_cause.items():
            by_cause[cause] = by_cause.get(cause, 0) + count

    return {
        "day": day.isoformat(),
        "observed": observed,
        "degraded": degraded,
        "degraded_with_results": with_results,
        # Sums to at least `degraded`: a response whose reason names several
        # hydration causes is counted under each of them.
        "by_cause": dict(sorted(by_cause.items())),
        # Counted here but not yet in the tables — the window an ungraceful
        # kill would lose.
        "pending_flush": pending_depth(),
        "consecutive_flush_failures": _flush_failures,
    }


# ── Worker ──────────────────────────────────────────────────────

_flusher = BackfillRunner(
    "search_degradation_flusher", flush_once,
    idle_secs=FLUSH_INTERVAL_SECS,
    # Every tick of a deployment serving searches has work; a per-tick progress
    # line would be thousands of INFO lines a day and would bury the flush
    # failures, which are the ones worth seeing.
    log_progress=False,
)


def reset() -> None:
    """Drop every un-flushed count. Startup and tests only."""
    global _flush_failures
    _pending.clear()
    _flush_failures = 0


def start() -> None:
    _flusher.start()


async def stop() -> None:
    """Quiesce the flusher, then drain what is left.

    Order matters: draining first races the live flusher, which may already own
    the batch, so the final drain would see an empty map and a failure in that
    in-flight write would requeue into a map nobody looks at again. Stopping
    first makes the remainder ours alone.

    Both phases are bounded and both are REACHED. Kubernetes grants 30s and the
    all-in-one supervisor 15s, so an unbounded stop is simply SIGKILLed
    mid-drain; and one `wait_for` around the pair would let an expiry during the
    first phase skip the drain entirely, which is the phase this exists for.

    Nothing here may propagate. `lifecycle.stop_workers()` gathers these, and an
    escaping exception from one component has historically skipped every worker
    after it.
    """
    budget = SHUTDOWN_BUDGET_SECS / 2
    try:
        await _flusher.stop(timeout=budget)
    except Exception:  # noqa: BLE001
        logger.warning("search degradation flusher stop failed", exc_info=True)
    try:
        if _pending:
            await asyncio.wait_for(flush_once(), timeout=budget)
    except Exception:  # noqa: BLE001
        logger.warning(
            "search degradation final flush failed; %d response(s) not recorded",
            pending_depth(), exc_info=True,
        )
