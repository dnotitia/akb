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

from .base import ChunkUpsert, VectorHit, VectorStoreUnavailable, has_dense

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
PAYLOAD_VAULT_ID = "vault_id"


class QdrantStore:
    """VectorStore impl over Qdrant. Uses native RRF fusion."""

    # Stores vault_id on each point's payload, filters on it in hybrid_search,
    # exposes vault_backfill_pending() — vault filter capable (issue #189 P2).
    vault_filter_supported = True

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
            # qdrant-client 1.13+ stubs narrowed `timeout` to int|None
            # even though the runtime still accepts float. Integer
            # seconds is what we want anyway at this scale.
            url=self._url, api_key=self._api_key, timeout=30,
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
        # vault_id is the per-vault ACL filter key (issue #189 Phase 2). Indexed
        # for the same reason as source_id. Created OUTSIDE the `if not exists`
        # block (idempotent server-side) so an ALREADY-deployed collection
        # — created before vault_id existed — also gets the index on first ensure.
        try:
            await client.create_payload_index(
                collection_name=self._collection,
                field_name=PAYLOAD_VAULT_ID,
                field_schema=qm.PayloadSchemaType.KEYWORD,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("vault_id payload index ensure failed (non-fatal): %s", e)
        self._ensured_collection = True

    async def health(self) -> bool:
        try:
            client = self._get_client()
            await client.get_collections()
            return True
        except Exception:  # noqa: BLE001
            return False

    async def vault_backfill_pending(self) -> int:
        """Points whose `vault_id` payload is missing/null (issue #189 Phase 2) —
        the backfill-readiness signal the worker gates on (qdrant has no server-
        side join to the main DB, so the operator backfills by reindexing). The
        qdrant analogue of pgvector's `WHERE vault_id IS NULL`.

        Uses `IsEmptyCondition`, NOT `IsNullCondition`: verified against qdrant
        1.18.2, IsNull matches only an EXPLICIT null and MISSES a point that
        never had the key — which is exactly the pre-upgrade point set we must
        count. IsEmpty matches missing OR null. (A wrong choice here under-counts
        → the gate flips ready early → users miss their un-backfilled docs.)
        `exact=True` is mandatory: the default approximate count could read 0
        prematurely for the same reason."""
        await self.ensure_collection()
        client = self._get_client()
        try:
            res = await client.count(
                collection_name=self._collection,
                count_filter=qm.Filter(
                    must=[qm.IsEmptyCondition(is_empty=qm.PayloadField(key=PAYLOAD_VAULT_ID))],
                ),
                exact=True,
            )
            return int(res.count)
        except Exception as e:  # noqa: BLE001
            raise VectorStoreUnavailable(f"vault_backfill_pending failed: {e}") from e

    # ── Upsert ───────────────────────────────────────────────────

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
        vault_id: str,
    ) -> None:
        del conn  # external service; can't share PG transaction
        await self.ensure_collection()
        client = self._get_client()

        # Qdrant named-vectors collections accept points that carry only a
        # subset of the declared vectors — leave dense out when the embed
        # API was unavailable; the dense leg of hybrid_search will then
        # have nothing to score against for this point.
        vectors: dict[str, Any] = {}
        if has_dense(dense):
            vectors[DENSE_VECTOR_NAME] = dense
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
                PAYLOAD_VAULT_ID: str(vault_id),
            },
        )
        await client.upsert(collection_name=self._collection, points=[point])

    # ── Delete ───────────────────────────────────────────────────

    async def upsert_batch(
        self,
        chunks: list[ChunkUpsert],
        *,
        conn=None,
    ) -> None:
        """Fallback batch path — N calls of ``upsert_one``. No native
        batch shape on this driver yet; the loop preserves the
        Protocol contract while keeping per-call atomicity unchanged."""
        from .base import loop_upsert_batch
        await loop_upsert_batch(self, chunks, conn=conn)

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
        vault_ids: list[str] | None = None,
    ) -> list[VectorHit]:
        await self.ensure_collection()
        client = self._get_client()

        # Exactly one ACL filter (issue #189 Phase 2): the caller sends vault_ids
        # (vault path) OR source_ids (resource path), never both. Mirrors pgvector.
        assert not (vault_ids and source_ids), \
            "hybrid_search got both vault_ids and source_ids; expected exactly one"

        has_dense = query_dense is not None and len(query_dense) > 0
        has_sparse = len(query_sparse_indices) > 0
        if not has_dense and not has_sparse:
            return []

        flt = _acl_filter(vault_ids=vault_ids, source_ids=source_ids)

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


def _acl_filter(*, vault_ids: list[str] | None, source_ids: list[str] | None):
    """The ACL pre-filter: vault_ids (per-vault, issue #189 Phase 2) wins when
    present, else source_ids (per-resource), else no filter. The qdrant analogue
    of pgvector's `WHERE {vault_id|source_id} = ANY(...)`. vault_id is stored as
    UUID text (like source_id), so MatchAny over the keyword index is exact."""
    if vault_ids:
        key, vals = PAYLOAD_VAULT_ID, vault_ids
    elif source_ids:
        key, vals = PAYLOAD_SOURCE_ID, source_ids
    else:
        return None
    return qm.Filter(
        must=[qm.FieldCondition(key=key, match=qm.MatchAny(any=[str(v) for v in vals]))],
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
