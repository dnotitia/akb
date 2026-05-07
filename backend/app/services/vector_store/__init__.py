"""Hybrid (dense + BM25 sparse) vector store, driver-pluggable.

Public API:
    VectorStore              — Protocol all drivers implement
    VectorHit                — search result dataclass
    VectorStoreUnavailable   — driver-side transient failure
    get_vector_store()       — factory; selects driver from settings

Drivers (in sibling modules):
    qdrant.QdrantStore       — native RRF via Query API
    pgvector.PgvectorStore   — (Phase 3) PG + pgvector ext, app-side RRF

Source-of-truth split: main PG holds chunk text + metadata; the driver
holds the derived index (dense embedding + corpus-side BM25 sparse).
A full vector-store loss is recoverable by setting
`chunks.vector_indexed_at = NULL` and letting the indexer worker
re-upsert from PG.
"""

from .base import VectorHit, VectorStore, VectorStoreUnavailable
from .factory import get_vector_store, reset_singleton_for_tests

__all__ = [
    "VectorStore",
    "VectorHit",
    "VectorStoreUnavailable",
    "get_vector_store",
    "reset_singleton_for_tests",
]
