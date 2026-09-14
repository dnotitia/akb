"""Automatic `vault_id` backfill (issue #189 Phase 2).

Increment A added a `vault_id` column to the pgvector index and writes it for
NEW points; points indexed before the upgrade have `vault_id = NULL`. The vault
filter (`vault_filter_enabled`) is only correct once every LIVE-source point has
its `vault_id`, otherwise a user could miss their own un-backfilled docs.

Rather than make every operator run `scripts/backfill_vault_id.py` and flip a
flag by hand, this background worker backfills the column automatically on
startup (non-blocking, in bounded batches), and search self-activates the vault
path only once it reports ready — until then it transparently uses the existing
source-id path (no behavior change, no under-fetch). Zero-touch upgrade.

Auto-fill scope: pgvector in same-instance mode (the vector index lives in the
main DB, so the backfill is a server-side join). Every OTHER vault-filter-capable
driver (qdrant, seahorse, separate-instance pgvector) can't be reached by that
join, so this worker doesn't auto-fill them — it only GATES readiness on the
driver's own `vault_backfill_pending()` reaching 0 (the operator backfills:
qdrant via `scripts/backfill_vault_id.py`, seahorse via recreate + reindex). A
driver whose `vault_filter_supported` is False never takes the vault path, so the
worker latches ready immediately for it.

Readiness = "no LIVE-source point is missing vault_id". Orphan points (whose
source row was deleted) keep `vault_id` NULL forever but are excluded by BOTH the
vault and source paths, so they do NOT block readiness.
"""
from __future__ import annotations

import logging
import time

from app.config import settings
from app.db.postgres import get_pool
from app.services._backfill import BackfillRunner
from app.services.vector_store import get_vector_store
from app.services.vector_store.base import supports_vault_filter

logger = logging.getLogger("akb.vault_backfill")

_BATCH = 5000
# Process-local fast path for the search hot path. Set once THIS process has
# established readiness (see is_ready_async); never cleared afterwards.
# Increment A guarantees no NEW nulls appear, so once True it stays True for
# the life of the process. It is deliberately NOT shared across processes —
# cross-process visibility comes from the cached DB check in is_ready_async.
_ready = False
# Cached outcome of the last cross-process readiness check: (established_at,
# value). A True value is latched into `_ready` above, so only False outcomes
# are ever re-checked — and only after the TTL below.
_last_check: tuple[float, bool] | None = None
# How long a negative readiness outcome is trusted before re-checking the
# store (akb#526). The check is one indexed COUNT(*) on the vector schema;
# 30s keeps the hot path cheap while opening the vault path within half a
# minute of the backfill completing anywhere (any process, any replica).
_NEGATIVE_TTL_SECS = 30.0


def is_ready() -> bool:
    """Process-local fast path: True once THIS process has established
    readiness. Kept for the worker loop (`_process_once` short-circuit) and
    for tests. Search must call `is_ready_async()` instead — on a split
    api/worker deployment the worker flips its own copy of this latch while
    the serving process never runs the backfill runner, so a bare memory
    read here is permanently False where search actually runs (akb#526)."""
    return _ready


async def is_ready_async() -> bool:
    """Cross-process readiness for the search hot path (akb#526).

    Returns the process-local latch when set (zero cost). Otherwise asks the
    store for its NULL-`vault_id` count — state every tier can see — and
    caches a negative outcome for `_NEGATIVE_TTL_SECS`. A True outcome
    latches process-locally and is never re-checked.
    """
    global _ready, _last_check
    if _ready:
        return True
    now = time.monotonic()
    if _last_check is not None:
        checked_at, value = _last_check
        if not value and now - checked_at < _NEGATIVE_TTL_SECS:
            return False
    store = get_vector_store()
    if not supports_vault_filter(store):
        _ready = True
        _last_check = None
        return True
    fn = getattr(store, "vault_backfill_pending", None)
    if fn is None:
        # A capable driver that exposes no counter can't prove its existing
        # points carry vault_id: stay gated (matches the worker branch below).
        _last_check = (now, False)
        return False
    try:
        pending = await fn()
    except Exception:  # noqa: BLE001 — a counter failure must not open the path
        _last_check = (now, False)
        return False
    if pending == 0:
        _ready = True
        _last_check = None
        return True
    _last_check = (now, False)
    return False


def _is_pgvector() -> bool:
    return settings.vector_store_driver == "pgvector"


def _same_instance() -> bool:
    # The auto-join backfill needs the index to share the main DB
    # (same-instance: vector_store_dsn blank).
    return not settings.vector_store_dsn.strip()


def _applicable() -> bool:
    # True only where this worker can AUTO-backfill via the server-side
    # `UPDATE … JOIN documents/vault_tables/vault_files` (see _process_once).
    # That needs BOTH: the index shares the main DB (_same_instance) AND the
    # driver's points are joinable to the main catalog by source_id — which
    # `_is_pgvector` stands in for (it's the only such driver; qdrant/seahorse
    # store points out-of-band). A capable driver that isn't applicable is still
    # gated for readiness below, just not auto-filled (operator backfills).
    return _is_pgvector() and _same_instance()


async def _process_once() -> int:
    """One backfill step for the BackfillRunner. Returns rows updated; 0 makes
    the runner idle. Flips `_ready` when the vault path is safe to use."""
    global _ready
    if _ready:
        return 0

    store = get_vector_store()

    # A driver without the vault filter never takes the vault path
    # (`vault_path_eligible` is False for it), so readiness is moot. Latch ready
    # so the worker stops looping instead of spinning forever.
    if not supports_vault_filter(store):
        _ready = True
        return 0

    # Capable driver this worker CANNOT auto-backfill — qdrant, seahorse, or a
    # SEPARATE-instance pgvector: none is reachable by the same-DB server-side
    # join below. We DON'T auto-fill; the operator backfills (qdrant: the script;
    # seahorse: recreate + reindex; separate pgvector: scripts/backfill_vault_id).
    # We still gate: readiness = the driver reports 0 NULL vault_id (the manual
    # script's "0 before flip" contract). Until then search keeps the source-id
    # path — no under-fetch. A capable driver MUST expose vault_backfill_pending();
    # if one somehow doesn't, stay gated forever rather than activate blind.
    if not _applicable():
        fn = getattr(store, "vault_backfill_pending", None)
        if fn is None:
            return 0
        if await fn() == 0:
            _ready = True
            logger.info(
                "vault_id backfill complete (%s) — vault filter path is now active",
                settings.vector_store_driver,
            )
        return 0

    schema = settings.vector_store_schema
    pool = await get_pool()
    async with pool.acquire() as c:
        # Cheap EXISTS (stops at the first hit): is any LIVE-source point still
        # missing vault_id? Orphans (no live source) are ignored — they never
        # match and are excluded by both search paths anyway.
        more = await c.fetchval(
            f"""
            SELECT EXISTS(
              SELECT 1 FROM "{schema}".chunks vi
              WHERE vi.vault_id IS NULL AND (
                EXISTS(SELECT 1 FROM documents d   WHERE d.id = vi.source_id)
                OR EXISTS(SELECT 1 FROM vault_tables t WHERE t.id = vi.source_id)
                OR EXISTS(SELECT 1 FROM vault_files  f WHERE f.id = vi.source_id)))
            """
        )
        if not more:
            _ready = True
            logger.info("vault_id backfill complete — vault filter path is now active")
            return 0

        async with c.transaction():
            res = await c.execute(
                f"""
                WITH batch AS (
                  SELECT vi.chunk_id, src.vault_id AS vid
                  FROM "{schema}".chunks vi
                  JOIN (
                    SELECT id, vault_id FROM documents
                    UNION ALL SELECT id, vault_id FROM vault_tables
                    UNION ALL SELECT id, vault_id FROM vault_files
                  ) src ON src.id = vi.source_id
                  WHERE vi.vault_id IS NULL
                  LIMIT {_BATCH}
                  FOR UPDATE OF vi SKIP LOCKED
                )
                UPDATE "{schema}".chunks vi
                SET vault_id = batch.vid
                FROM batch
                WHERE vi.chunk_id = batch.chunk_id
                """
            )
    return int(res.split()[-1]) if res.startswith("UPDATE") else 0


# Idle cadence is generous: during the one-time backfill `_process_once` returns
# >0 and the runner loops immediately (drains fast); once ready it returns 0 and
# the runner sleeps. concurrency=1 — the batch UPDATE is idempotent on NULL, so a
# rolling-deploy overlap is harmless.
_runner = BackfillRunner("vault_backfill", _process_once, idle_secs=60)
start = _runner.start
stop = _runner.stop


async def pending_stats() -> dict:
    """For /health: readiness + remaining NULL count (the safe-to-activate
    signal). null_remaining includes orphans; readiness ignores them.
    `applicable` = this worker auto-fills here (pgvector same-instance);
    `vault_filter_supported` = the driver is capable at all (so an operator on
    qdrant/seahorse, where applicable=False, still sees the gate is live)."""
    store = get_vector_store()
    out: dict = {
        "ready": is_ready(),
        "applicable": _applicable(),
        "vault_filter_supported": supports_vault_filter(store),
    }
    fn = getattr(store, "vault_backfill_pending", None)
    if fn is not None:
        try:
            out["null_remaining"] = await fn()
        except Exception as e:  # noqa: BLE001
            out["null_remaining_error"] = str(e)
    return out
