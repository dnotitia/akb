"""Hybrid (dense + BM25 sparse) vector store, driver-pluggable.

Public API:
    VectorStore              — Protocol all drivers implement
    VectorHit                — search result dataclass
    VectorStoreUnavailable   — driver-side transient failure
    VectorSearchDegraded     — partial search hits with an explicit reason
    get_vector_store()       — factory; selects driver from settings
    decide_sparse_shape_for_settings()
                             — settles `auto` for this database before the
                               store is built (pgvector only)

Drivers (in sibling modules):
    qdrant.QdrantStore       — native RRF via Query API
    pgvector.PgvectorStore   — (Phase 3) PG + pgvector ext, app-side RRF

Source-of-truth split: main PG holds chunk text + metadata; the driver
holds the derived index (dense embedding + corpus-side BM25 sparse).
A full vector-store loss is recoverable by setting
`chunks.vector_indexed_at = NULL` and letting the indexer worker
re-upsert from PG.
"""

from .base import VectorHit, VectorSearchDegraded, VectorStore, VectorStoreUnavailable
from .factory import decide_sparse_shape_for_settings, get_vector_store, reset_singleton_for_tests

__all__ = [
    "VectorStore",
    "VectorHit",
    "VectorStoreUnavailable",
    "VectorSearchDegraded",
    "get_vector_store",
    "decide_sparse_shape_for_settings",
    "reset_singleton_for_tests",
]
