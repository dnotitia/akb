"""Vector store driver Protocol + shared types.

`VectorStore` is an interface; concrete drivers live in sibling
modules (`qdrant.py`, `pgvector.py`). Service-layer code depends on
the Protocol only — zero driver-specific imports leak past this
package boundary.

Sparse encoding is the *caller's* responsibility:
- `embed_worker` runs `sparse_encoder.encode_document` before upsert.
- `search_service` runs `sparse_encoder.encode_query` before search.

This keeps BM25 logic in one place (vocab + stats live in main PG)
and lets a future non-BM25 driver (SPLADE etc.) accept and ignore the
inputs without restructuring callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeGuard, runtime_checkable


@dataclass
class VectorHit:
    """A single search result. `score` is driver-internal (monotonic,
    higher is better) — do not compare across drivers.

    Mutable on purpose: search_service may overwrite `score` in-place
    with a post-rerank fused score."""
    chunk_id: str
    source_type: str
    source_id: str
    section_path: str
    content: str
    score: float


class VectorStoreUnavailable(Exception):
    """Driver-side transient failure. Worker paths catch and back off;
    read paths let it propagate so `search` returns empty instead of
    serving stale or partial results."""


def has_dense(dense: list[float] | None) -> TypeGuard[list[float]]:
    """Single source of truth for "this point has a dense vector".

    Callers (workers, drivers) pass ``None`` for "the embed API was
    unavailable for this row" and ``list[float]`` for "here's the
    embedding". The TypeGuard return lets mypy narrow ``dense`` to
    ``list[float]`` inside an ``if has_dense(dense):`` body so call
    sites don't need a redundant ``assert``.

    Drivers should branch on this helper instead of inlining
    ``if dense and len(dense) > 0:`` or ``if dense is not None:``.
    Three copies that disagreed about whether ``[]`` is "dense" is
    exactly the contract drift this helper exists to prevent.
    """
    return dense is not None and len(dense) > 0


@runtime_checkable
class VectorStore(Protocol):
    """Hybrid (dense + sparse) vector store.

    All write methods accept an optional `conn` (asyncpg.Connection).
    When the caller is already inside a Postgres transaction (e.g.
    `embed_worker` wrapping `upsert + mark` atomically), pgvector-style
    drivers join that transaction by reusing the conn — eliminating
    the dual-write race where the vector store commits but the caller
    rolls back. Drivers backed by external services (Qdrant) ignore
    `conn`; their writes are non-transactional from PG's point of
    view, and recovery is via re-upsert on the next worker cycle
    (idempotent by chunk_id).
    """

    async def ensure_collection(self, *, conn=None) -> None:
        """Idempotent: create the underlying storage (Qdrant collection,
        PG schema/tables, etc.) if it doesn't exist. May reuse `conn`
        on PG-backed drivers to participate in an outer transaction.

        **Race-safety is the driver's responsibility.** This method is
        called from lifespan startup *and* lazily from the first request
        that touches the store, possibly from multiple workers, possibly
        with the in-flight call being cancelled (e.g. nginx timeout
        propagating asyncio.CancelledError into a long-running CREATE
        INDEX). The driver MUST guarantee:

          - Concurrent callers serialize, in-process AND cross-process
            (multiple uvicorn workers / pods sharing the same backend).
          - A cancelled or crashed call cannot strand whatever
            serialization primitive was used (no orphaned advisory
            locks / phantom collection-creation flags).
          - Any schema migration step is atomic from the search side's
            point of view — there must never be a window where a
            previously-functional index/collection is absent.

        The 0.6.4 prod incident (see backend/CHANGELOG.md) is the
        canonical anti-example: pre-0.6.4 ``PgvectorStore`` had only
        an in-process ``asyncio.Lock`` + an ``_ensured`` bool, both of
        which a cancellation reset. The drop-then-create migration was
        non-atomic. Cross-process serialization (PG advisory lock) +
        atomic index swap (``CREATE _new`` → ``DROP legacy`` → ``RENAME``)
        was the fix, and that pattern's pgvector-specific shape is fine
        — but every other driver author has to solve the same problem
        with whatever primitives their backend exposes.

        Today's drivers (audit before swapping production):
          - ``PgvectorStore``: ✓ pg_advisory_xact_lock + atomic swap.
          - ``QdrantStore``: ⚠ only the ``_ensured_collection`` bool +
            ``collection_exists()`` check. Probably fine because Qdrant
            ``create_collection`` is idempotent server-side, but no
            explicit cross-process guard.
          - ``SeahorseStore``: ⚠ BFF ``_bff_get_table()`` + optional
            auto-create; same race-window shape as Qdrant.
        Both ⚠ drivers should be audited before they go to prod at
        scale; the asymmetry exists because only pgvector has ever
        actually shipped a schema migration step here.
        """

    async def health(self) -> bool:
        """Light reachability check — fast, no side effects. Used by
        `/readyz` and `/health`."""

    async def upsert_one(
        self,
        *,
        conn=None,
        chunk_id: str,
        content: str,
        section_path: str | None,
        chunk_index: int,
        dense: list[float] | None,
        sparse_indices: list[int],
        sparse_values: list[float],
        source_type: str,
        source_id: str,
    ) -> None:
        """Upsert one chunk into the driver. `dense` and `sparse_*` are
        pre-computed by the caller (`embed_worker` for indexing).

        ``dense=None`` is the BM25-only fallback the worker uses when the
        embedding API is unavailable. Drivers must store the point with
        only sparse vectors — pgvector writes NULL into the `dense`
        column (excluded from the partial HNSW index by its WHERE
        clause), Qdrant omits the dense entry from its named-vectors
        dict, Seahorse omits the dense column. `hybrid_search` has the
        symmetric `query_dense=None` path so dense-less points only
        contribute to the sparse leg.

        Atomicity guarantees are driver-specific. pgvector joins the
        caller's PG transaction when `conn` is provided (the upsert
        commits with the caller's own writes). Qdrant and Seahorse ignore
        `conn` — their writes are external and non-transactional from
        PG's perspective, so recovery for half-completed batches relies
        on `chunk_id` idempotence (the worker can safely re-upsert)."""

    async def delete_point(self, chunk_id: str, *, conn=None) -> None:
        """Remove a single chunk by id. Idempotent."""

    async def hybrid_search(
        self,
        *,
        query_text: str,
        query_dense: list[float] | None,
        query_sparse_indices: list[int],
        query_sparse_values: list[float],
        source_ids: list[str] | None,
        limit: int,
        prefetch_per_leg: int,
    ) -> list[VectorHit]:
        """Dense + sparse search, RRF-fused.

        - `query_text` is for driver-side debug/logging only.
        - `query_dense` may be None (embedding API unavailable) — driver
          falls back to sparse-only.
        - Empty `query_sparse_indices` means OOV query — driver falls
          back to dense-only.
        - `source_ids` is the post-filter (resolved at the caller from
          vault/collection/doc_type/tags/ACL); drivers translate to
          their own filter primitive.
        - `prefetch_per_leg` caps each leg's candidate pool before
          fusion. Caller decides — typically `max(limit * 3, 50)`.
        """
