"""Per-vault health aggregator.

Composes per-vault pending_stats() calls into a single response that
mirrors a subset of the global /health endpoint. The auth check
happens in the route handler; this module just composes.

Post-Phase-4 indexing is one stage (embed + sparse + vector-store
upsert), so the vector-store side carries the only backfill counter.
"""

from __future__ import annotations

import uuid

from app.services import delete_worker, embed_worker, metadata_worker


async def vault_health(vault_id: uuid.UUID) -> dict:
    """Return per-vault pending counts. Sequential awaits — the queries
    are sub-millisecond on indexed columns; gather() saves ~1-2ms but
    adds task-scheduling overhead."""
    backfill = await embed_worker.pending_stats(vault_id)
    metadata = await metadata_worker.pending_stats(vault_id)
    return {
        "metadata_backfill": metadata,
        "vector_store":      {"backfill": backfill},
    }
