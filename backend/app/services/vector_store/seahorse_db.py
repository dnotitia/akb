"""SeahorseDB (self-hosted) driver for the VectorStore Protocol.

Talks to a Coral coordinator (Rust HTTP API), which fronts a SeahorseDB
Writer/Reader cluster + Redis + Kafka. Coral is the single entry point
— this driver never speaks to Writer/Reader/Redis directly.

Endpoint map (Coral HTTP API, see Coral's ``src/interface/http/routes.rs``):

  - ``GET  /health``                                — health
  - ``POST /catalog/tables``                        — create
  - ``GET  /catalog/tables/{name}``                 — exists
  - ``POST /catalog/tables/{name}/data``            — insert (Kafka async)
  - ``POST /catalog/tables/{name}/data/delete``     — delete
  - ``POST /catalog/tables/{name}/data/hybrid-search``  — fused dense+sparse

Sparse encoding contract: the AKB ``embed_worker`` + ``sparse_encoder``
emits two parallel arrays ``sparse_indices: list[int]`` +
``sparse_values: list[float]``. Coral takes the equivalent shape as a
list of ``[term_id, weight]`` pairs — we zip at the driver boundary so
the rest of AKB never sees the Coral-specific shape.

Label mapping: SeahorseDB identifies records by a ``u64`` label. AKB
uses ``chunk_id`` (UUID). We map UUID → u64 by taking the first 8
bytes (big-endian) of the UUID's raw 16-byte form. This is a one-way
hash from AKB's perspective; collisions inside one table would only
happen at ~2^32 chunks (birthday paradox) — well above any realistic
single-vault scale and within the eventual-rebuild cost budget if we
ever hit it.

Async / eventual-consistency caveat: inserts at Coral go through
Kafka before they reach Writer + Reader. ``POST /data`` returns once
the message is in Kafka, NOT once the row is searchable. Callers that
need "the chunk I just upserted shows up in the next search" (e.g.
some AKB integration tests) need to poll Coral's
``/catalog/tables/{name}/data/scan`` or accept ~10-30s eventual
visibility. ``embed_worker`` is fine — it marks `vector_indexed_at`
on Kafka-accept, and the next search-time gap is the same gap any
async indexing pipeline has.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import httpx

from .base import VectorHit, VectorStoreUnavailable, has_dense


logger = logging.getLogger("akb.vector_store.seahorse_db")


def _chunk_id_to_label(chunk_id: str) -> int:
    """UUID -> SeahorseDB u64 label.

    First 8 bytes of the UUID's binary form, interpreted big-endian.
    Stable, dependency-free, no DB round-trip. Collisions are
    birthday-paradox bounded — ~2^32 chunks per table before a 50%
    chance of any pair colliding, far beyond any realistic vault."""
    raw = uuid.UUID(chunk_id).bytes
    return int.from_bytes(raw[:8], "big", signed=False)


class SeahorseDbStore:
    """VectorStore driver targeting a self-hosted SeahorseDB cluster
    via its Coral coordinator HTTP API.

    Construction is config-only — no network calls. The lazy
    ``ensure_collection`` does the catalog setup on first use (and
    is called from lifespan startup so the cost lands at boot, not
    on the first search request)."""

    def __init__(
        self,
        *,
        coordinator_url: str,
        table_name: str,
        dense_dim: int,
        distance: str = "cosine",
        auto_create: bool = True,
        timeout: float = 30.0,
    ):
        self._url = coordinator_url.rstrip("/")
        self._table = table_name
        self._dense_dim = dense_dim
        self._distance = distance
        self._auto_create = auto_create
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._ensured_collection = False
        self._ensure_lock = asyncio.Lock()

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._url, timeout=self._timeout,
            )
        return self._client

    async def aclose(self) -> None:
        """Test-only: close the underlying HTTP client. Production
        path keeps it open for the process lifetime."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── VectorStore Protocol ──────────────────────────────────────

    async def ensure_collection(self, *, conn=None) -> None:
        """Create the AKB-shaped table on Coral if missing. No-op when
        the table is already there or ``auto_create=False`` (operator
        manages schema externally)."""
        if self._ensured_collection:
            return
        async with self._ensure_lock:
            if self._ensured_collection:
                return
            http = await self._http()
            try:
                resp = await http.get(f"/catalog/tables/{self._table}")
            except httpx.HTTPError as e:
                raise VectorStoreUnavailable(
                    f"Coral unreachable at {self._url}: {e}"
                ) from e

            if resp.status_code == 200:
                self._ensured_collection = True
                return
            if resp.status_code != 404:
                raise VectorStoreUnavailable(
                    f"Coral GET /catalog/tables/{self._table} → "
                    f"{resp.status_code}: {resp.text[:200]}"
                )

            if not self._auto_create:
                raise VectorStoreUnavailable(
                    f"table {self._table!r} absent on Coral and "
                    f"seahorsedb_auto_create=false; create it manually "
                    f"or flip the flag in app.yaml."
                )

            create_payload = self._build_create_table_payload()
            try:
                resp = await http.post(
                    "/catalog/tables", json=create_payload,
                )
            except httpx.HTTPError as e:
                raise VectorStoreUnavailable(
                    f"Coral POST /catalog/tables: {e}"
                ) from e
            if resp.status_code not in (200, 201, 409):
                # 409 = raced with a peer that already created it; OK.
                raise VectorStoreUnavailable(
                    f"Coral POST /catalog/tables → "
                    f"{resp.status_code}: {resp.text[:200]}"
                )
            self._ensured_collection = True

    async def health(self) -> bool:
        try:
            http = await self._http()
            resp = await http.get("/health", timeout=5.0)
            return resp.status_code == 200
        except Exception:  # noqa: BLE001
            return False

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
        record: dict[str, Any] = {
            "id": _chunk_id_to_label(chunk_id),
            "chunk_id": chunk_id,
            "content": content,
            "section_path": section_path or "",
            "chunk_index": chunk_index,
            "source_type": source_type,
            "source_id": source_id,
        }
        if has_dense(dense):
            record["embedding"] = dense
        if sparse_indices:
            # Coral consumes sparse as a list of [term_id, weight] pairs.
            record["sparse"] = [
                [int(t), float(w)]
                for t, w in zip(sparse_indices, sparse_values)
            ]

        http = await self._http()
        try:
            resp = await http.post(
                f"/catalog/tables/{self._table}/data",
                json={"records": [record]},
            )
        except httpx.HTTPError as e:
            raise VectorStoreUnavailable(
                f"Coral POST /data: {e}"
            ) from e
        if resp.status_code not in (200, 202):
            raise VectorStoreUnavailable(
                f"Coral POST /data → {resp.status_code}: {resp.text[:200]}"
            )

    async def delete_point(self, chunk_id: str, *, conn=None) -> None:
        label = _chunk_id_to_label(chunk_id)
        http = await self._http()
        try:
            resp = await http.post(
                f"/catalog/tables/{self._table}/data/delete",
                json={"labels": [label]},
            )
        except httpx.HTTPError as e:
            raise VectorStoreUnavailable(
                f"Coral POST /data/delete: {e}"
            ) from e
        # 200 OK or 404 (already gone — idempotent).
        if resp.status_code not in (200, 202, 404):
            raise VectorStoreUnavailable(
                f"Coral POST /data/delete → "
                f"{resp.status_code}: {resp.text[:200]}"
            )

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
        payload: dict[str, Any] = {
            "k": limit,
            "ef": max(prefetch_per_leg, limit * 2),
        }
        if has_dense(query_dense):
            payload["query_vector"] = query_dense
        if query_sparse_indices:
            payload["sparse_query"] = [
                [int(t), float(w)]
                for t, w in zip(query_sparse_indices, query_sparse_values)
            ]
        if source_ids:
            payload["filter"] = {
                "Keyword": {"field": "source_id", "values": source_ids},
            }

        http = await self._http()
        try:
            resp = await http.post(
                f"/catalog/tables/{self._table}/data/hybrid-search",
                json=payload,
            )
        except httpx.HTTPError as e:
            raise VectorStoreUnavailable(
                f"Coral POST /hybrid-search: {e}"
            ) from e
        if resp.status_code != 200:
            raise VectorStoreUnavailable(
                f"Coral POST /hybrid-search → "
                f"{resp.status_code}: {resp.text[:200]}"
            )

        body = resp.json()
        hits: list[VectorHit] = []
        for row in body.get("hits", []):
            hits.append(VectorHit(
                chunk_id=row.get("chunk_id") or "",
                source_type=row.get("source_type") or "",
                source_id=row.get("source_id") or "",
                section_path=row.get("section_path") or "",
                content=row.get("content") or "",
                score=float(row.get("score") or 0.0),
            ))
        return hits

    # ── internals ─────────────────────────────────────────────────

    def _build_create_table_payload(self) -> dict[str, Any]:
        """AKB-shaped table: id (label) + chunk_id (UUID string) +
        content + section_path + chunk_index + source_type + source_id
        + embedding (dense vector) + sparse (hybrid index).
        Mirrors what `_handle_get` / search_service expect to round-
        trip back."""
        return {
            "name": self._table,
            "dimension": self._dense_dim,
            "distance_space": _distance_to_coral(self._distance),
            "schema": {
                "columns": [
                    {"name": "id", "column_type": "Int64", "primary_key": True},
                    {"name": "chunk_id", "column_type": "String"},
                    {"name": "embedding",
                     "column_type": {"Vector": self._dense_dim}},
                    {"name": "sparse", "column_type": "SparseVector"},
                    {"name": "content", "column_type": "String"},
                    {"name": "section_path", "column_type": "String"},
                    {"name": "chunk_index", "column_type": "Int32"},
                    {"name": "source_type", "column_type": "String"},
                    {"name": "source_id", "column_type": "String"},
                ],
            },
        }


def _distance_to_coral(d: str) -> str:
    """AKB config uses lowercase; Coral's enum is PascalCase."""
    return {"cosine": "Cosine", "l2": "L2", "ip": "InnerProduct"}[d]
