"""Computes the `/stats` snapshot on a timer and caches it.

**Requests never compute.** `/stats` reads whatever this module last stored;
it does not touch the database. The endpoint is reachable by a poller on a
fixed cadence, and a surface that recomputed per request would let a
misconfigured (or duplicated) poller turn a monitoring feature into a source
of `pg_database_size` scans and `COUNT(*)`s on the serving pool. The cost is
therefore fixed at one sample per `stats.sampler_interval_secs` regardless of
how often anyone asks.

Three sections, three different consistency stories, deliberately not unified:

* ``corpus`` is one REPEATABLE READ snapshot — the counts are consistent with
  each other, so ``distilled_doc_count <= doc_count`` cannot be violated by a
  write landing between two statements.
* ``storage`` carries its own ``observed_at`` watermark. Physical sizes are
  approximations maintained by the statistics collector and have no meaningful
  relationship to a transaction snapshot; pretending otherwise by stamping them
  with the corpus timestamp would be a lie about their freshness.
* ``activity`` is a **closed fact** about a finished UTC day, folded once and
  then persisted (``tenant_activity_daily``). See :mod:`app.db.migrations`
  entry 087 for why it must survive a restart unchanged.

**Absent is not zero.** Every numeric field is nullable, and a value that could
not be computed is omitted from the payload rather than defaulted. `0` is a
measurement; the whole point of this contract is that a consumer can tell the
difference between "no files" and "we could not measure the files".
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from app.config import settings
from app.db.postgres import get_pool
from app.services._backfill import BackfillRunner

logger = logging.getLogger("akb.stats_sampler")

# Bumped only for a breaking change to the payload shape. An additive optional
# field does not bump it and may ship producer-first; anything else has to
# reach consumers before it ships here, because an unrecognised version makes
# them reject the whole snapshot rather than read the fields they know.
SCHEMA_VERSION = 1

# The pgvector relations counted by `storage.vector_bytes`, named explicitly
# rather than discovered by scanning the schema: an operator who puts something
# else in that schema should not silently change what this number means.
# `pg_total_relation_size` covers each one's heap, its TOAST table and TOAST
# index, and every index on it.
#
# These live INSIDE the main database when the driver shares the main pool, so
# `vector_bytes` is a SUBSET of `db_bytes` — the two must never be added
# together. When the driver is anything else, or points at its own DSN, the
# bytes are not in this database and the field is reported as absent rather
# than as a number that would break that containment.
PGVECTOR_RELATIONS: tuple[str, ...] = ("chunks", "posting")

_snapshot: dict[str, Any] | None = None
_last_error: str | None = None
_samples = 0
_failures = 0


def snapshot() -> dict[str, Any] | None:
    """The last successfully computed payload, or None before the first one.

    A failed sample never replaces a good one: the payload is assembled into a
    local and published only on success, so a database blip degrades freshness
    (`computed_at` stops advancing) instead of blanking the surface.
    """
    return _snapshot


def stats() -> dict[str, Any]:
    """Sampler state, for logs and tests. Not part of the `/stats` payload."""
    return {
        "has_snapshot": _snapshot is not None,
        "samples": _samples,
        "failures": _failures,
        "last_error": _last_error,
        "interval_secs": settings.stats.sampler_interval_secs,
    }


def reset() -> None:
    """Drop the cached snapshot and counters (tests)."""
    global _snapshot, _last_error, _samples, _failures
    _snapshot = None
    _last_error = None
    _samples = 0
    _failures = 0


def _iso(moment: datetime) -> str:
    """RFC 3339 with a `Z` suffix and second precision.

    Second precision because the consumer treats these as observation
    timestamps on a multi-minute cadence, and a stable width keeps the golden
    fixture a fixed comparison rather than a regex.
    """
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _present(values: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is None.

    This is the encoding half of "absent is not zero": an unmeasurable field
    leaves the object entirely. A consumer decoding into a plain integer then
    has to opt in to a default, instead of receiving one silently.
    """
    return {key: value for key, value in values.items() if value is not None}


# ── activity window ──────────────────────────────────────────────────────


def target_activity_day(now: datetime, grace: timedelta) -> date:
    """The most recent UTC day that is closed AND past its settling grace.

    A day closes at midnight UTC, but writes that were in flight across that
    boundary reach `tool_calls` slightly afterwards (the usage sink hands off
    through an in-memory queue that a flusher drains on an interval). Folding
    the moment the clock ticks over would undercount them, and the fold is
    permanent — there is no second chance to correct it.

    So within `grace` of midnight the previous day is still considered
    unsettled and the day before it is the target. That day was folded a full
    24 hours ago, so this only ever re-selects an already-closed day; it never
    reaches backwards for one that was skipped.
    """
    today = now.astimezone(timezone.utc).date()
    midnight_today = datetime.combine(today, time.min, tzinfo=timezone.utc)
    if now < midnight_today + grace:
        return today - timedelta(days=2)
    return today - timedelta(days=1)


async def _activity_counts(conn, window_start: datetime, window_end: datetime):
    """Read/write call volume and distinct actors for one closed window.

    Returns ``(calls_read, calls_write, active_actors)``, any of which may be
    None.

    The read/write split is NOT re-derived here. `tool_calls.is_write` is
    written at the MCP dispatch chokepoint from the tool's required scope
    (``mcp_server/server.py``: ``_required_scope(name, arguments) ==
    _WRITE_SCOPE``), which is the same classification that decides whether the
    call is allowed to mutate anything. A second opinion — a list of tool names
    kept here — would drift from it silently the first time a tool is added.

    `active_actors` counts distinct non-NULL `actor_id`. Calls with no actor
    (unauthenticated public reads) are real traffic but not a distinct someone,
    and COUNT(DISTINCT …) skipping NULLs is exactly the wanted semantics.

    When usage tracking is off, `tool_calls` is empty for structural reasons
    and every count would come back 0 — a number that looks like "a quiet day"
    and would be frozen as one forever, since the fold is permanent. All three
    are reported unknown instead.
    """
    if not settings.tool_usage.enabled:
        return None, None, None
    row = await conn.fetchrow(
        """
        SELECT COUNT(*) FILTER (WHERE NOT is_write) AS calls_read,
               COUNT(*) FILTER (WHERE is_write)     AS calls_write,
               COUNT(DISTINCT actor_id)             AS active_actors
          FROM tool_calls
         WHERE occurred_at >= $1
           AND occurred_at <  $2
        """,
        window_start,
        window_end,
    )
    if row is None:
        return None, None, None
    return int(row["calls_read"]), int(row["calls_write"]), int(row["active_actors"])


async def _fold_activity_day(conn, now: datetime) -> None:
    """Fold the target day into `tenant_activity_daily` if it is not there yet.

    `ON CONFLICT (day) DO NOTHING` is the whole durability contract: whichever
    process closes a day first decides its value permanently, and every later
    sample — in this process or a restarted one — is a no-op. Without that, a
    restart would recompute the window against a `tool_calls` table that has
    since been purged or re-flushed and could publish a different number for a
    window the consumer already stored.

    The existence probe before computing is an optimisation, not the guard;
    the ON CONFLICT is.
    """
    grace = timedelta(minutes=settings.stats.activity_grace_minutes)
    day = target_activity_day(now, grace)
    window_start = datetime.combine(day, time.min, tzinfo=timezone.utc)
    window_end = window_start + timedelta(days=1)

    already = await conn.fetchval("SELECT 1 FROM tenant_activity_daily WHERE day = $1", day)
    if already:
        return

    calls_read, calls_write, active_actors = await _activity_counts(conn, window_start, window_end)
    await conn.execute(
        """
        INSERT INTO tenant_activity_daily
            (day, window_start, window_end, calls_read, calls_write, active_actors)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (day) DO NOTHING
        """,
        day,
        window_start,
        window_end,
        calls_read,
        calls_write,
        active_actors,
    )


async def _read_activity(conn) -> dict[str, Any] | None:
    """The newest folded window, or None when no day has been folded yet.

    A fresh deployment has nothing here until the first sample after the grace
    period, and the section is omitted rather than filled with a window whose
    counts are all unknown — "we have not measured a day yet" is a different
    state from "this day is unmeasurable".
    """
    row = await conn.fetchrow(
        """
        SELECT day, window_start, window_end, calls_read, calls_write, active_actors
          FROM tenant_activity_daily
         ORDER BY day DESC
         LIMIT 1
        """
    )
    if row is None:
        return None
    return _present(
        {
            "window_start": _iso(row["window_start"]),
            "window_end": _iso(row["window_end"]),
            "calls_read": _int_or_none(row["calls_read"]),
            "calls_write": _int_or_none(row["calls_write"]),
            "active_actors": _int_or_none(row["active_actors"]),
        }
    )


def _int_or_none(value: Any) -> int | None:
    return None if value is None else int(value)


# ── corpus ───────────────────────────────────────────────────────────────


async def _distilled_doc_count(conn) -> int | None:
    """Documents produced by distillation — currently NOT DETERMINED.

    The cross-repo design leaves "what counts as distilled" open and assigns
    the question to this repository, which has no marker to answer it with:
    nothing in the schema, the document metadata, or the write path records
    that a document was produced by distillation. The only adjacent signal is
    `vault_write_policy.managed_by`, a free-text owner label whose values
    ("gardener:distill", "collector:acme-jira") are set by whoever provisions
    the policy — a naming convention, not a fact the database enforces.

    So this reports unknown. It is a nullable field precisely so that an
    undecided definition can be represented honestly; returning `COUNT(*)` of a
    guessed predicate would publish a number nobody can defend, and the
    dashboard would render it as measured.

    **This is a stub awaiting a per-document marker.** Counting the documents
    of vaults whose `managed_by` label starts with `gardener:` was considered
    and rejected: it counts everything in such a vault, including what a person
    wrote there by hand, and re-pointing a vault at a different owner would
    silently reclassify its whole history. The accepted direction is an
    explicit marker written by the distillation path itself, which is a fact
    about the document and cannot move under it. That spans this repository and
    the gardener, so it is deliberately not in this change; when it lands, this
    function is the only thing here that changes.
    """
    return None


async def _read_corpus(conn) -> dict[str, Any]:
    """Inventory counts from ONE repeatable-read snapshot.

    Live only: deletion removes the rows, so a plain COUNT is the live count.
    Archived vaults and archived documents are counted — archived is a
    lifecycle state of an object that still exists and still occupies storage,
    which is the opposite of deleted.

    The explicit REPEATABLE READ transaction is doing real work even though the
    counts currently fit in one statement. It makes the "one snapshot"
    guarantee structural rather than incidental, so that splitting this into
    several statements later (which `distilled_doc_count` will require) cannot
    quietly reintroduce a torn read where a subset count exceeds its superset.
    """
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        row = await conn.fetchrow(
            """
            SELECT (SELECT COUNT(*) FROM vaults)      AS vault_count,
                   (SELECT COUNT(*) FROM collections) AS collection_count,
                   (SELECT COUNT(*) FROM documents)   AS doc_count,
                   -- Chunks that are actually present in the vector index.
                   -- `chunks` is the source of truth and always holds more
                   -- during indexing; the difference is the backlog, which is
                   -- already reported by /health rather than duplicated here.
                   (SELECT COUNT(*) FROM chunks
                     WHERE vector_indexed_at IS NOT NULL) AS vector_chunk_count
            """
        )
        distilled = await _distilled_doc_count(conn)

    return _present(
        {
            "vault_count": int(row["vault_count"]),
            "collection_count": int(row["collection_count"]),
            "doc_count": int(row["doc_count"]),
            "distilled_doc_count": distilled,
            "vector_chunk_count": int(row["vector_chunk_count"]),
        }
    )


# ── storage ──────────────────────────────────────────────────────────────


async def _vector_bytes(conn) -> int | None:
    """Physical bytes held by the pgvector derived index, or None.

    None whenever the index is not inside this database — a different driver
    (qdrant, seahorse) or a pgvector driver pointed at its own DSN. Reporting a
    number there would break the documented containment (`vector_bytes` is a
    subset of `db_bytes`) and invite a consumer to add the two.
    """
    if settings.vector_store_driver != "pgvector":
        return None
    if settings.vector_store_dsn:
        return None

    total = 0
    found = False
    for relation in PGVECTOR_RELATIONS:
        size = await conn.fetchval(
            """
            SELECT pg_total_relation_size(c.oid)
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = $1
               AND c.relname = $2
            """,
            settings.vector_store_schema,
            relation,
        )
        if size is not None:
            found = True
            total += int(size)
    # The schema is created lazily on first use. Before that no relation
    # exists, and "the index has not been created" is unknown-shaped, not
    # zero-shaped.
    return total if found else None


async def _read_storage(conn) -> dict[str, Any]:
    """Physical + logical storage, stamped with its own observation time."""
    observed_at = datetime.now(timezone.utc)
    db_bytes = await conn.fetchval("SELECT pg_database_size(current_database())")
    vector_bytes = await _vector_bytes(conn)

    # Live logical file bytes. Only `confirmed` rows are counted: a pending row
    # is a reserved upload whose bytes may not exist in the object store yet.
    # Both kinds count — a `file` is a standalone vault file and an
    # `attachment` is an editor image, and both are objects the tenant is
    # storing.
    files = await conn.fetchrow(
        """
        SELECT COUNT(*)                                   AS file_count,
               COUNT(*) FILTER (WHERE size_bytes IS NULL) AS unsized,
               COALESCE(SUM(size_bytes), 0)               AS file_bytes
          FROM vault_files
         WHERE upload_state = 'confirmed'
        """
    )
    file_count = int(files["file_count"])
    # A sum over rows of unknown size is a floor, not a total, and it looks
    # exactly like a total to whoever charts it. Legacy rows predating size
    # recording are the realistic source of this. Report the count (which is
    # exact) and withhold the bytes.
    file_bytes = None if int(files["unsized"]) else int(files["file_bytes"])

    return _present(
        {
            "db_bytes": _int_or_none(db_bytes),
            "vector_bytes": vector_bytes,
            "file_bytes": file_bytes,
            "file_count": file_count,
            "observed_at": _iso(observed_at),
        }
    )


# ── sampling ─────────────────────────────────────────────────────────────


async def compute() -> dict[str, Any]:
    """Build one payload. Raises on failure; the caller decides what to keep."""
    now = datetime.now(timezone.utc)
    pool = await get_pool()
    async with pool.acquire() as conn:
        storage = await _read_storage(conn)
        corpus = await _read_corpus(conn)
        await _fold_activity_day(conn, now)
        activity = await _read_activity(conn)

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "computed_at": _iso(now),
        "storage": storage,
        "corpus": corpus,
    }
    if activity is not None:
        payload["activity"] = activity
    return payload


async def sample_once() -> int:
    """One sampler tick. Always returns 0 so the runner sleeps a full interval.

    The `BackfillRunner` cadence is "drain aggressively while there is work,
    otherwise sleep `idle_secs`". A sampler has no queue to drain, so it always
    reports no work and the idle sleep becomes the sampling interval.
    """
    global _snapshot, _last_error, _samples, _failures
    try:
        payload = await compute()
    except Exception as exc:  # noqa: BLE001 — a sample must never kill the loop
        _failures += 1
        _last_error = repr(exc)
        # Keep the traceback: this runs unattended, and the last snapshot is
        # still being served, so a persistent failure is otherwise visible only
        # as a `computed_at` that quietly stops moving.
        logger.exception("stats sample failed (%d consecutive-capable failures)", _failures)
        return 0
    _snapshot = payload
    _samples += 1
    _last_error = None
    return 0


_runner = BackfillRunner(
    "stats_sampler",
    sample_once,
    idle_secs=settings.stats.sampler_interval_secs,
    log_progress=False,
)
stop = _runner.stop


def start() -> None:
    """Start sampling at the configured cadence.

    `configure_idle_secs` is re-applied here rather than only at construction
    so that a test (or a reload) that changes the setting takes effect on the
    next start instead of keeping whatever the value was at import time.
    """
    _runner.configure_idle_secs(settings.stats.sampler_interval_secs)
    _runner.start()
