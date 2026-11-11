"""Per-vault health aggregator.

Composes per-vault pending_stats() calls into a single response that
mirrors a subset of the global /health endpoint. The auth check
happens in the route handler; this module composes, and
`cached_vault_health` reuses one composition per vault for a short TTL.

Post-Phase-4 indexing is one stage (embed + sparse + vector-store
upsert), so the vector-store side carries the only backfill counter.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import math
import os
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable

from app.config import VAULT_HEALTH_CACHE_TTL_MAX_SECS, settings
from app.services import (
    embed_worker,
    metadata_worker,
    native_derived_worker,
    native_file_projection,
    search_update_status,
)

logger = logging.getLogger("akb.health")

VAULT_HEALTH_CACHE_TTL_ENV = "AKB_VAULT_HEALTH_CACHE_TTL_SECS"
# Distinct vaults whose result one process keeps. Expired entries are dropped
# as new ones arrive, so this only binds when this many vaults are polled
# within one TTL. A dropped entry costs one recomputation.
VAULT_HEALTH_CACHE_MAX_ENTRIES = 1024


async def vault_health(vault_id: uuid.UUID) -> dict:
    """Return per-vault pending counts. Sequential awaits — the queries
    are sub-millisecond on indexed columns; gather() saves ~1-2ms but
    adds task-scheduling overhead.

    `native_derived` is the vault-scoped answer to the question the global
    counters can only raise: it reports the derived-index intents this vault
    gave up on, so an operator who sees a non-zero `abandoned` on `/health`
    can find where the loss is without reading an internal queue table."""
    backfill = await embed_worker.pending_stats(vault_id)
    metadata = await metadata_worker.pending_stats(vault_id)
    projection = await native_file_projection.pending_stats(vault_id)
    derived = await native_derived_worker.pending_stats(vault_id)
    result = {
        "metadata_backfill": metadata,
        "vector_store":      {"backfill": backfill},
        "native_file_projection": projection,
        "native_derived": derived,
    }
    try:
        current_heads = await native_derived_worker.current_head_stats(vault_id)
    except Exception as exc:  # noqa: BLE001
        # Never substitute historical outcomes while claiming current-Head scope.
        logger.warning("Current search preparation observation unavailable (%s)", type(exc).__name__)
        current_heads = None
    try:
        result["search_update_status"] = search_update_status.build(
            backfill, metadata, projection, current_heads,
        )
    except Exception as exc:  # noqa: BLE001
        # This additive interpretation must not break successful legacy reads.
        logger.warning("Search update interpretation unavailable (%s)", type(exc).__name__)
    return result


def configured_vault_health_cache_ttl() -> float:
    """Seconds one vault's health result is reused: env > app.yaml > default.

    AKB_VAULT_HEALTH_CACHE_TTL_SECS is a deployment-layer knob in the same
    class as AKB_PG_POOL_MIN_SIZE/MAX_SIZE: an operator or control plane can
    retune or disable (0) the cache per deployment without re-rendering
    config files. A malformed value fails loudly instead of falling back,
    because a silently ignored override looks applied and is not.
    """
    raw = os.getenv(VAULT_HEALTH_CACHE_TTL_ENV, "").strip()
    if not raw:
        return float(settings.vault_health_cache_ttl_secs)
    try:
        ttl = float(raw)
    except ValueError:
        raise RuntimeError(
            f"{VAULT_HEALTH_CACHE_TTL_ENV} must be a number of seconds; got {raw!r}"
        ) from None
    if not (math.isfinite(ttl) and 0.0 <= ttl <= VAULT_HEALTH_CACHE_TTL_MAX_SECS):
        raise RuntimeError(
            f"{VAULT_HEALTH_CACHE_TTL_ENV} must be between 0 and "
            f"{VAULT_HEALTH_CACHE_TTL_MAX_SECS:g} seconds; got {raw!r}"
        )
    return ttl


class VaultHealthCache:
    """Reuse one `vault_health()` result per vault for a short TTL, per process.

    `vault_health()` is a handful of aggregates over the vault's whole chunk
    and queue population, so it costs more the larger the vault is, while the
    web console polls it for each of the user's vaults on a timer, from every
    open tab. The counts feed a progress display: being a few seconds old is
    invisible, recomputing them for every poll is not.

    - Keyed by vault id, never by name: a vault deleted and re-created under
      the same name is a different key.
    - Authorization is not part of this cache. The caller checks the
      requester's role on every request before asking; the value is a
      vault-level aggregate, the same for every authorized reader.
    - Single-flight: concurrent misses for one vault await one computation.
      It runs as its own task, so a caller that goes away (a client
      disconnect, a timeout) does not cancel it for the callers still
      waiting.
    - Only results are stored. A failure reaches the callers waiting on that
      computation, and the next request computes afresh.
    - Each caller gets its own copy, as it did before the cache existed, so a
      caller that edits its response cannot change anyone else's.
    - Bounded. Every entry gets the same TTL, so insertion order is expiry
      order: each store drops the expired entries from the front, then the
      ones closest to expiry beyond `max_entries`.
    - `ttl_secs <= 0` disables it: every call computes, nothing is kept.
    """

    def __init__(
        self,
        ttl_secs: float,
        *,
        max_entries: int = VAULT_HEALTH_CACHE_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._ttl = ttl_secs
        self._max_entries = max_entries
        self._clock = clock
        # vault id -> (expires_at on `clock`, result)
        self._entries: OrderedDict[uuid.UUID, tuple[float, dict]] = OrderedDict()
        self._inflight: dict[uuid.UUID, asyncio.Task[dict]] = {}

    def __len__(self) -> int:
        return len(self._entries)

    async def get(
        self, vault_id: uuid.UUID, compute: Callable[[uuid.UUID], Awaitable[dict]],
    ) -> dict:
        if self._ttl <= 0:
            return await compute(vault_id)
        entry = self._entries.get(vault_id)
        if entry is not None and self._clock() < entry[0]:
            return copy.deepcopy(entry[1])
        loop = asyncio.get_running_loop()
        task = self._inflight.get(vault_id)
        # A computation started on another event loop cannot be awaited from
        # this one. One process serves on one loop; this only guards reuse of
        # the instance across loops, as a test suite does.
        if task is None or task.get_loop() is not loop:
            task = loop.create_task(self._compute_and_store(vault_id, compute))
            self._inflight[vault_id] = task
        return copy.deepcopy(await asyncio.shield(task))

    async def _compute_and_store(
        self, vault_id: uuid.UUID, compute: Callable[[uuid.UUID], Awaitable[dict]],
    ) -> dict:
        try:
            value = await compute(vault_id)
            self._store(vault_id, value)
            return value
        finally:
            if self._inflight.get(vault_id) is asyncio.current_task():
                del self._inflight[vault_id]

    def _store(self, vault_id: uuid.UUID, value: dict) -> None:
        # The TTL counts from when the result is ready, not from when it was
        # asked for: under a slow database a computation that outlived the TTL
        # would otherwise be stored already stale and recomputed at once.
        now = self._clock()
        self._entries.pop(vault_id, None)
        self._entries[vault_id] = (now + self._ttl, value)
        while self._entries:
            oldest_expires_at, _ = next(iter(self._entries.values()))
            if len(self._entries) <= self._max_entries and oldest_expires_at > now:
                break
            self._entries.popitem(last=False)


_vault_health_cache = VaultHealthCache(configured_vault_health_cache_ttl())


async def cached_vault_health(vault_id: uuid.UUID) -> dict:
    """`vault_health()` through this process's per-vault cache.

    The caller must have authorized the requester for this vault first; the
    cache is shared by every reader of the vault and checks nothing itself.
    """
    return await _vault_health_cache.get(vault_id, vault_health)
