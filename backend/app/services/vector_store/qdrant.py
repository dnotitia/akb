"""Qdrant driver for VectorStore.

Native RRF fusion via Qdrant's Query API when both legs are available
— this is the fastest hybrid path. Falls back to dense-only or
sparse-only when one signal is missing.

The collection layout matches the existing internal cluster's
`chunks` collection (named-vector `dense` + sparse `bm25`, payload
keyword index on `source_id`). Behavior-preserving extraction from
the legacy `vector_store.py`.
"""

from __future__ import annotations

import logging
from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qm

from .base import VectorHit, VectorStoreUnavailable

logger = logging.getLogger("akb.vector_store.qdrant")


# Named vectors inside the collection.
DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "bm25"

# Per-chunk payload keys.
PAYLOAD_CHUNK_INDEX = "chunk_index"
PAYLOAD_SECTION_PATH = "section_path"
PAYLOAD_CONTENT = "content"
PAYLOAD_SOURCE_TYPE = "source_type"
PAYLOAD_SOURCE_ID = "source_id"


class QdrantStore:
    """VectorStore impl over Qdrant. Uses native RRF fusion."""

    def __init__(
        self,
        *,
        url: str,
        api_key: str | None,
        collection: str,
        dense_dim: int,
    ):
        self._url = url
        self._api_key = api_key or None
        self._collection = collection
        self._dense_dim = dense_dim
        self._client: AsyncQdrantClient | None = None
        self._ensured_collection = False

    def _get_client(self) -> AsyncQdrantClient:
        if self._client is not None:
            return self._client
        # 30s — must absorb HNSW search latency on large collections,
        # especially right after bulk re-index when undeleted-segment
        # overhead lingers until the optimizer's vacuum sweep catches up.
        self._client = AsyncQdrantClient(
            url=self._url, api_key=self._api_key, timeout=30.0
        )
        return self._client

    async def ensure_collection(self, *, conn=None) -> None:
        del conn  # Qdrant is external; PG transaction conn isn't reused.
        if self._ensured_collection:
            return
        client = self._get_client()
        try:
            exists = await client.collection_exists(self._collection)
        except Exception as e:  # noqa: BLE001
            raise VectorStoreUnavailable(f"ping failed: {e}") from e

        if not exists:
            await client.create_collection(
                collection_name=self._collection,
                vectors_config={
                    DENSE_VECTOR_NAME: qm.VectorParams(
                        size=self._dense_dim,
                        distance=qm.Distance.COSINE,
                    ),
                },
                sparse_vectors_config={
                    SPARSE_VECTOR_NAME: qm.SparseVectorParams(),
                },
            )
            logger.info(
                "Vector collection %r created (dense_dim=%d)",
                self._collection, self._dense_dim,
            )
            # source_id is the primary filter key on every search.
            # Without this payload index the store falls back to a
            # full scan per query — measurable latency regression.
            await client.create_payload_index(
                collection_name=self._collection,
                field_name=PAYLOAD_SOURCE_ID,
                field_schema=qm.PayloadSchemaType.KEYWORD,
            )
        self._ensured_collection = True

    async def health(self) -> bool:
        try:
            client = self._get_client()
            await client.get_collections()
            return True
        except Exception:  # noqa: BLE001
            return False

    # ── Upsert ───────────────────────────────────────────────────

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
        del conn  # external service; can't share PG transaction
        await self.ensure_collection()
        client = self._get_client()

        vectors: dict[str, Any] = {DENSE_VECTOR_NAME: dense}
        if sparse_indices:
            vectors[SPARSE_VECTOR_NAME] = qm.SparseVector(
                indices=sparse_indices, values=sparse_values,
            )

        point = qm.PointStruct(
            id=str(chunk_id),
            vector=vectors,
            payload={
                PAYLOAD_CHUNK_INDEX: int(chunk_index),
                PAYLOAD_SECTION_PATH: section_path or "",
                PAYLOAD_CONTENT: content,
                PAYLOAD_SOURCE_TYPE: source_type,
                PAYLOAD_SOURCE_ID: str(source_id),
            },
        )
        await client.upsert(collection_name=self._collection, points=[point])

    # ── Delete ───────────────────────────────────────────────────

    async def delete_point(self, chunk_id: str, *, conn=None) -> None:
        del conn  # external service; can't share PG transaction
        await self.ensure_collection()
        client = self._get_client()
        await client.delete(
            collection_name=self._collection,
            points_selector=[str(chunk_id)],
        )

    # ── Search ───────────────────────────────────────────────────

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
        await self.ensure_collection()
        client = self._get_client()

        has_dense = query_dense is not None and len(query_dense) > 0
        has_sparse = len(query_sparse_indices) > 0
        if not has_dense and not has_sparse:
            return []

        flt = _source_filter(source_ids)

        if has_dense and has_sparse:
            prefetch = [
                qm.Prefetch(
                    query=query_dense,
                    using=DENSE_VECTOR_NAME,
                    limit=prefetch_per_leg,
                    filter=flt,
                ),
                qm.Prefetch(
                    query=qm.SparseVector(
                        indices=query_sparse_indices,
                        values=query_sparse_values,
                    ),
                    using=SPARSE_VECTOR_NAME,
                    limit=prefetch_per_leg,
                    filter=flt,
                ),
            ]
            result = await client.query_points(
                collection_name=self._collection,
                prefetch=prefetch,
                query=qm.FusionQuery(fusion=qm.Fusion.RRF),
                limit=limit,
                with_payload=True,
            )
            return [_to_hit(p) for p in result.points]

        if has_dense:
            # Dense-only: query has no known vocab term. Cross-encoder
            # rerank downstream filters semantically-weak matches.
            result = await client.query_points(
                collection_name=self._collection,
                query=query_dense,
                using=DENSE_VECTOR_NAME,
                limit=limit,
                query_filter=flt,
                with_payload=True,
            )
            return [_to_hit(p) for p in result.points]

        # Sparse-only: embedding API unavailable.
        result = await client.query_points(
            collection_name=self._collection,
            query=qm.SparseVector(
                indices=query_sparse_indices,
                values=query_sparse_values,
            ),
            using=SPARSE_VECTOR_NAME,
            limit=limit,
            query_filter=flt,
            with_payload=True,
        )
        return [_to_hit(p) for p in result.points]


def _source_filter(source_ids: list[str] | None):
    if not source_ids:
        return None
    return qm.Filter(
        must=[
            qm.FieldCondition(
                key=PAYLOAD_SOURCE_ID,
                match=qm.MatchAny(any=[str(d) for d in source_ids]),
            ),
        ],
    )


def _to_hit(point) -> VectorHit:
    payload = point.payload or {}
    return VectorHit(
        chunk_id=str(point.id),
        source_type=payload.get(PAYLOAD_SOURCE_TYPE) or "document",
        source_id=payload.get(PAYLOAD_SOURCE_ID) or "",
        section_path=payload.get(PAYLOAD_SECTION_PATH) or "",
        content=payload.get(PAYLOAD_CONTENT) or "",
        score=float(point.score) if point.score is not None else 0.0,
    )
