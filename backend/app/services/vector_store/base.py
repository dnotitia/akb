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
from typing import Protocol, runtime_checkable


@dataclass
class VectorHit:
    """A single search result. `score` is driver-internal (monotonic,
    higher is better) — do not compare across drivers.

    Mutable on purpose: the rerank service overwrites `score` in-place
    after cross-encoder rescoring."""
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
        on PG-backed drivers to participate in an outer transaction."""

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
        dense: list[float],
        sparse_indices: list[int],
        sparse_values: list[float],
        source_type: str,
        source_id: str,
    ) -> None:
        """Upsert one chunk. Driver stores dense + sparse + payload
        atomically. `dense` and `sparse_*` are pre-computed by callers.
        Pass `conn` to share an outer PG transaction (atomic with the
        caller's own writes)."""

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
