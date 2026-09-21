"""Shared PostgreSQL coordination primitives for BM25 maintenance.

The BM25 stats refresher and the offline vector backfill are separate
processes, but they operate on the same corpus and must agree on the advisory
lock namespace. Keep the constants here so the application and the CLI do not
each invent a key (or accidentally drift to a different hash function).

The helpers in this module are deliberately read-only observations. The
exclusive recompute lock remains owned by ``sparse_encoder.recompute_stats``;
the active-run query below looks only at PostgreSQL's granted lock table and
does not infer activity from the durable ``bm25_recompute_run`` row or from
backend query text.
"""

from __future__ import annotations

import hashlib


# This value predates the shared maintenance namespace. It is intentionally
# frozen: rolling deployments may still have a refresher using this key.
BM25_RECOMPUTE_LOCK_KEY = 987654321


def advisory_lock_key(namespace: str) -> int:
    """Return a deterministic signed PostgreSQL bigint for ``namespace``.

    ``hashtextextended`` would be stable inside PostgreSQL, but deriving the
    constants in Python keeps the offline CLI and the application independent
    of a database round trip. BLAKE2b's fixed eight-byte digest maps exactly
    to PostgreSQL's signed ``bigint`` advisory-lock argument.
    """

    if not isinstance(namespace, str) or not namespace:
        raise ValueError("advisory-lock namespace must be a non-empty string")
    digest = hashlib.blake2b(namespace.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


# These names are corpus-wide on purpose. Do not add a schema or tenant
# suffix: the bulk CLI and every serving replica must exclude one another.
BM25_BULK_OWNER_LOCK_KEY = advisory_lock_key(
    "akb:bm25-maintenance:bulk:owner"
)
BM25_BULK_CONTROL_LOCK_KEY = advisory_lock_key(
    "akb:bm25-maintenance:bulk:control"
)


BM25_RECOMPUTE_SKIP_REASON_LOCK_HELD = "recompute_lock_held"
BM25_RECOMPUTE_SKIP_REASON_VECTOR_QUEUE = "vector_upsert_queue_nonempty"
BM25_RECOMPUTE_SKIP_REASONS = frozenset(
    {
        BM25_RECOMPUTE_SKIP_REASON_LOCK_HELD,
        BM25_RECOMPUTE_SKIP_REASON_VECTOR_QUEUE,
    }
)


async def vector_upsert_queue_nonempty(conn) -> bool:
    """Return whether any retryable chunk remains in the vector upsert queue.

    This is intentionally the queue predicate used by ``embed_worker``'s
    global pending stats, expressed locally to avoid importing that worker
    from ``sparse_encoder`` and creating a module cycle. Abandoned rows are
    terminal outcomes, not queue work, so they do not defer a stats scan.
    """

    return bool(
        await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                  FROM chunks
                 WHERE vector_indexed_at IS NULL
                   AND vector_abandoned_at IS NULL
            )
            """
        )
    )


async def active_bm25_recompute(conn) -> bool:
    """Return whether this database grants the legacy recompute lock.

    PostgreSQL exposes a bigint advisory lock as the high and low 32-bit
    portions in ``pg_locks.classid`` and ``pg_locks.objid``. Restricting the
    observation to this database, ``ExclusiveLock``, and ``granted`` avoids
    reporting a waiter, a different advisory key, or a lock from another
    database. The query does not inspect ``pg_stat_activity`` or query text.
    """

    return bool(
        await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                  FROM pg_locks
                 WHERE locktype = 'advisory'
                   AND database = (
                       SELECT oid
                         FROM pg_database
                        WHERE datname = current_database()
                   )
                   AND classid = (($1::bigint >> 32) & 4294967295)::oid
                   AND objid = ($1::bigint & 4294967295)::oid
                   AND objsubid = 1
                   AND granted
                   AND mode = 'ExclusiveLock'
            )
            """,
            BM25_RECOMPUTE_LOCK_KEY,
        )
    )


# A descriptive alias reads better at call sites that use the helper as a
# predicate, while keeping one implementation and one SQL query to review.
is_bm25_recompute_active = active_bm25_recompute


__all__ = [
    "BM25_RECOMPUTE_LOCK_KEY",
    "BM25_BULK_OWNER_LOCK_KEY",
    "BM25_BULK_CONTROL_LOCK_KEY",
    "BM25_RECOMPUTE_SKIP_REASON_LOCK_HELD",
    "BM25_RECOMPUTE_SKIP_REASON_VECTOR_QUEUE",
    "BM25_RECOMPUTE_SKIP_REASONS",
    "advisory_lock_key",
    "vector_upsert_queue_nonempty",
    "active_bm25_recompute",
    "is_bm25_recompute_active",
]
