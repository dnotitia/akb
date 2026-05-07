"""Seahorse Cloud driver for VectorStore.

Talks to Seahorse Cloud's two-plane API:

- **Management (BFF)** at `vector_store_management_url` for table
  lifecycle (list / get / create / delete).
- **Per-table data plane host** for upsert / search / delete-row /
  schema. The host is discovered from the management response (each
  table gets its own subdomain) and cached on `ensure_collection`.

A single `SeahorseStore` instance maps to a single Seahorse table.
Multiple AKB sources (different `source_id`) live in the same table
and are separated by SQL `WHERE` filters at search time.

Auth: `Authorization: Bearer <shsk_...>`. Despite the spec advertising
an `apiKeyAuth` scheme via the `api-key` header, only the bearer
scheme is accepted by the live gateway as of 2026-05-07.

Reference: see akb product vault, doc `d-f25fb2f7`
("Seahorse Cloud — Hybrid Vector Table API 사용법") for the full
API reverse-engineering writeup.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from .base import VectorHit, VectorStoreUnavailable

logger = logging.getLogger("akb.vector_store.seahorse")


# Seahorse PK is enforced as `<prefix><RS><uint64>` — record separator
# is ASCII 30 (\x1e). We compose it from `source_id` + `chunk_index`
# so chunks of the same source share a prefix.
_RS = "\x1e"

# Column names this driver assumes in the table schema. ensure_collection
# verifies them; auto_create lays them down.
COL_ID = "id"                       # PK (Seahorse-format)
COL_EXTERNAL_CHUNK_ID = "external_chunk_id"
COL_SOURCE_TYPE = "source_type"
COL_SOURCE_ID = "source_id"
COL_SECTION_PATH = "section_path"
COL_CONTENT = "content"
COL_CHUNK_INDEX = "chunk_index"
COL_DENSE = "dense_vector"
COL_SPARSE = "sparse_vector"

# RRF k constant matches what other drivers use, so VectorHit.score
# stays comparable across drivers in spirit (still not numerically
# comparable per the Protocol contract).
RRF_K = 60


def _seahorse_pk(source_id: str, chunk_index: int) -> str:
    """Build the Seahorse-format PK: `<source_id><RS><chunk_index>`."""
    return f"{source_id}{_RS}{int(chunk_index)}"


def _sql_quote(value: str) -> str:
    """Escape single quotes for an SQL WHERE literal."""
    return value.replace("'", "''")


def _sparse_to_str(indices: list[int], values: list[float]) -> str:
    """`(indices, values)` → `"i1:v1 i2:v2 ..."` (Seahorse sparse format)."""
    return " ".join(f"{int(i)}:{float(v)}" for i, v in zip(indices, values))


class SeahorseStore:
    """VectorStore impl over Seahorse Cloud's TABLE_V2 + BFF API."""

    def __init__(
        self,
        *,
        management_url: str,
        token: str,
        tenant_uuid: str,
        table_name: str | None = None,
        table_uuid: str | None = None,
        dense_dim: int = 1024,
        auto_create: bool = False,
        request_timeout: float = 30.0,
    ):
        if not (table_name or table_uuid):
            raise ValueError(
                "SeahorseStore requires either table_name or table_uuid "
                "(blank both)."
            )
        if not token:
            raise ValueError("SeahorseStore requires a bearer token.")
        if not tenant_uuid:
            raise ValueError("SeahorseStore requires tenant_uuid.")
        self._mgmt_url = management_url.rstrip("/")
        self._token = token
        self._tenant_uuid = tenant_uuid
        self._table_name = table_name
        self._table_uuid = table_uuid
        self._dense_dim = dense_dim
        self._auto_create = auto_create
        self._timeout = request_timeout
        self._table_host: str | None = None
        self._client: httpx.AsyncClient | None = None
        self._ensured = False

    # ── HTTP plumbing ─────────────────────────────────────────────

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                headers={"Authorization": f"Bearer {self._token}"},
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _bff_get_table(self) -> dict | None:
        """Look up the configured table via BFF. Returns the table
        descriptor dict (with `table_uuid`, `host_name`, `schema`, ...)
        or None when not found."""
        client = await self._http()
        if self._table_uuid:
            url = f"{self._mgmt_url}/tenants/{self._tenant_uuid}/tables/{self._table_uuid}"
            r = await client.get(url)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json().get("data") or {}
        # Lookup by name: list + filter (BFF doesn't expose by-name endpoint).
        url = f"{self._mgmt_url}/tenants/{self._tenant_uuid}/tables"
        r = await client.get(url)
        r.raise_for_status()
        for tbl in r.json().get("data") or []:
            if tbl.get("table_name") == self._table_name:
                return tbl
        return None

    # ── Lifecycle ─────────────────────────────────────────────────

    async def ensure_collection(self, *, conn=None) -> None:
        del conn  # external service; can't share PG transaction
        if self._ensured:
            return
        try:
            tbl = await self._bff_get_table()
            if tbl is None:
                if not self._auto_create:
                    raise VectorStoreUnavailable(
                        f"Seahorse table not found "
                        f"(tenant={self._tenant_uuid}, "
                        f"name={self._table_name!r}, "
                        f"uuid={self._table_uuid!r}). "
                        "Create it in the console first or set "
                        "vector_store_seahorse_auto_create=true."
                    )
                tbl = await self._create_table()
            self._validate_schema(tbl)
            self._table_uuid = tbl.get("table_uuid") or self._table_uuid
            host = tbl.get("host_name")
            if not host:
                raise VectorStoreUnavailable(
                    "Seahorse table descriptor missing host_name"
                )
            self._table_host = host.rstrip("/")
        except VectorStoreUnavailable:
            raise
        except httpx.HTTPError as e:
            raise VectorStoreUnavailable(f"Seahorse BFF unreachable: {e}") from e
        self._ensured = True

    async def _create_table(self) -> dict:
        """Auto-provision the AKB-shaped table. Idempotent only in the
        sense that re-calling after success raises (the existence
        check above takes precedence). One-shot path."""
        if not self._table_name:
            raise VectorStoreUnavailable(
                "Cannot auto-create Seahorse table without "
                "vector_store_seahorse_table_name (uuid alone won't do "
                "— it doesn't exist yet)."
            )
        body = {
            "table_name": self._table_name,
            "primary_key": [COL_ID],
            "columns": [
                {"name": COL_ID, "nullable": False, "type": "STRING"},
                {"name": COL_EXTERNAL_CHUNK_ID, "nullable": False, "type": "STRING"},
                {"name": COL_SOURCE_TYPE, "nullable": False, "type": "STRING"},
                {"name": COL_SOURCE_ID, "nullable": False, "type": "STRING"},
                {"name": COL_SECTION_PATH, "nullable": True, "type": "STRING"},
                {"name": COL_CONTENT, "nullable": False, "type": "STRING"},
                {"name": COL_CHUNK_INDEX, "nullable": False, "type": "INT64"},
                {"name": COL_DENSE, "nullable": False,
                 "type": {"name": "VECTOR", "element": "FLOAT32", "dim": self._dense_dim}},
                {"name": COL_SPARSE, "nullable": False,
                 "type": {"name": "SPARSE_VECTOR"}},
            ],
            "indexes": [
                {"type": "DISKBASED", "column": COL_DENSE,
                 "params": {"M": 16, "ef_construction": 128, "space": "ip"}},
                {"type": "INVERTED", "column": COL_SPARSE,
                 "params": {"sparse_model": "bm25"}},
            ],
            "segmentation": {"strategy": "hash", "columns": [COL_ID],
                             "buckets": 1, "composition": "single"},
            "configurations": {"active_set_size_limit": 10000, "max_threads": 8},
        }
        client = await self._http()
        url = f"{self._mgmt_url}/tenants/{self._tenant_uuid}/tables"
        r = await client.post(url, json=body)
        r.raise_for_status()
        logger.info(
            "Seahorse table auto-created: name=%s tenant=%s",
            self._table_name, self._tenant_uuid,
        )
        return r.json().get("data") or {}

    def _validate_schema(self, tbl: dict) -> None:
        """Sanity-check the AKB-required columns + dense dim. Loose —
        warns instead of failing on extra columns; only fails on
        missing required columns or dim mismatch (both would corrupt
        upsert/search at runtime)."""
        cols = {c["name"]: c for c in (tbl.get("columns") or [])}
        required = {COL_ID, COL_EXTERNAL_CHUNK_ID, COL_SOURCE_ID,
                    COL_CONTENT, COL_DENSE, COL_SPARSE}
        missing = required - cols.keys()
        if missing:
            raise VectorStoreUnavailable(
                f"Seahorse table {tbl.get('table_name')!r} is missing "
                f"required columns: {sorted(missing)}. The driver expects "
                f"the AKB schema (see vector_store/seahorse.py docstring)."
            )
        dense_col = cols.get(COL_DENSE) or {}
        dense_type = dense_col.get("type") or {}
        if isinstance(dense_type, dict):
            actual_dim = dense_type.get("dim")
            if actual_dim and int(actual_dim) != int(self._dense_dim):
                raise VectorStoreUnavailable(
                    f"Seahorse {COL_DENSE} dim={actual_dim} != configured "
                    f"embed_dimensions={self._dense_dim}. Recreate the "
                    f"table or change the embedding model."
                )

    async def health(self) -> bool:
        try:
            if not self._ensured:
                await self.ensure_collection()
            client = await self._http()
            r = await client.get(f"{self._table_host}/v2/data/schema")
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    # ── Upsert ────────────────────────────────────────────────────

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
        del conn
        await self.ensure_collection()
        row = {
            COL_ID: _seahorse_pk(str(source_id), int(chunk_index)),
            COL_EXTERNAL_CHUNK_ID: str(chunk_id),
            COL_SOURCE_TYPE: source_type,
            COL_SOURCE_ID: str(source_id),
            COL_SECTION_PATH: section_path or "",
            COL_CONTENT: content,
            COL_CHUNK_INDEX: int(chunk_index),
            COL_DENSE: list(dense),
            COL_SPARSE: _sparse_to_str(sparse_indices, sparse_values),
        }
        # Insert is JSONL with text/plain — `application/json` is rejected.
        body = json.dumps(row, ensure_ascii=False)
        try:
            client = await self._http()
            r = await client.post(
                f"{self._table_host}/v2/data",
                content=body.encode("utf-8"),
                headers={"Content-Type": "text/plain"},
            )
            if r.status_code >= 400:
                raise VectorStoreUnavailable(
                    f"Seahorse upsert failed: HTTP {r.status_code} {r.text[:500]}"
                )
        except httpx.HTTPError as e:
            raise VectorStoreUnavailable(f"Seahorse upsert failed: {e}") from e

    # ── Delete ────────────────────────────────────────────────────

    async def delete_point(self, chunk_id: str, *, conn=None) -> None:
        del conn
        await self.ensure_collection()
        # We delete by external_chunk_id (AKB UUID) rather than the
        # Seahorse PK, because the caller passes us their own chunk
        # identifier, not a Seahorse PK.
        filter_sql = f"{COL_EXTERNAL_CHUNK_ID} = '{_sql_quote(str(chunk_id))}'"
        try:
            client = await self._http()
            r = await client.post(
                f"{self._table_host}/v2/data/delete",
                json={"filter": filter_sql},
            )
            if r.status_code >= 400:
                raise VectorStoreUnavailable(
                    f"Seahorse delete failed: HTTP {r.status_code} {r.text[:500]}"
                )
        except httpx.HTTPError as e:
            raise VectorStoreUnavailable(f"Seahorse delete failed: {e}") from e

    # ── Search ────────────────────────────────────────────────────

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
        # Seahorse runs its own RRF prefetch internally; the caller's
        # prefetch_per_leg hint isn't surfaced as an API knob.
        del query_text, prefetch_per_leg
        await self.ensure_collection()

        has_dense = query_dense is not None and len(query_dense) > 0
        has_sparse = len(query_sparse_indices) > 0
        if not has_dense and not has_sparse:
            return []

        if has_dense and has_sparse:
            mode = "hybrid"
        elif has_dense:
            mode = "dense"
        else:
            mode = "sparse"

        body: dict[str, Any] = {
            "search_mode": mode,
            "top_k": int(limit),
            "projection": (
                f"{COL_EXTERNAL_CHUNK_ID}, {COL_SOURCE_TYPE}, "
                f"{COL_SOURCE_ID}, {COL_SECTION_PATH}, {COL_CONTENT}"
            ),
        }
        if has_dense:
            body["dense"] = {
                "column": COL_DENSE,
                "vector": [list(query_dense)],
            }
        if has_sparse:
            # We deliberately omit `parameters` (k/b). Sending them
            # forces the server to also require BM25 corpus metadata
            # (N, avgdl, df) on every query — and that metadata lives
            # in main PG for AKB's own BM25 encoder, not in the form
            # Seahorse expects. Without `parameters`, the server uses
            # its own defaults (k=1.2, b=0.75) which are close enough
            # — AKB's caller-side encoder already produced the
            # weighted sparse vector, so the server's BM25 scoring
            # only re-weights it; the relative ranking is dominated
            # by our IDF math, not the server's k1/b.
            body["sparse"] = {
                "column": COL_SPARSE,
                "vector": [_sparse_to_str(query_sparse_indices, query_sparse_values)],
            }
        if has_dense and has_sparse:
            body["fusion"] = {"type": "rrf", "parameters": {"k": RRF_K}}
        if source_ids:
            quoted = ",".join(f"'{_sql_quote(str(s))}'" for s in source_ids)
            body["filter"] = f"{COL_SOURCE_ID} IN ({quoted})"

        try:
            client = await self._http()
            r = await client.post(
                f"{self._table_host}/v2/data/search",
                json=body,
            )
            if r.status_code >= 400:
                # raise_for_status alone hides the body; surface it so
                # the operator sees `Invalid argument: ...` instead of
                # a bare HTTP code.
                raise VectorStoreUnavailable(
                    f"Seahorse search failed: HTTP {r.status_code} {r.text[:500]}"
                )
            payload = r.json()
        except httpx.HTTPError as e:
            raise VectorStoreUnavailable(f"Seahorse search failed: {e}") from e

        # Response shape: data.data is a list of per-query hit lists
        # (batched-query model). Single-query → outer length 1.
        outer = (payload.get("data") or {}).get("data") or []
        hits = outer[0] if outer and isinstance(outer[0], list) else outer
        # Seahorse doesn't return a fusion score in the projection;
        # synthesise a monotonic score from the rank so VectorHit.score
        # remains comparable in spirit (rerank stage will overwrite
        # this anyway with cross-encoder scores).
        return [
            VectorHit(
                chunk_id=row.get(COL_EXTERNAL_CHUNK_ID, "") or "",
                source_type=row.get(COL_SOURCE_TYPE, "document") or "document",
                source_id=row.get(COL_SOURCE_ID, "") or "",
                section_path=row.get(COL_SECTION_PATH, "") or "",
                content=row.get(COL_CONTENT, "") or "",
                score=1.0 / (RRF_K + rank),
            )
            for rank, row in enumerate(hits, start=1)
        ]
