"""SeahorseDB (self-hosted) driver for the VectorStore Protocol.

Talks to a Coral coordinator (Rust HTTP API), which fronts a SeahorseDB
Writer/Reader cluster + Redis + Kafka. Coral is the single entry point
— this driver never speaks to Writer/Reader/Redis directly.

Endpoint map (Coral HTTP API, see Coral's ``src/interface/http/routes.rs``):

  - ``GET  /health``                                          — health (general_routes, no /v2)
  - ``POST /v2/tables``                               — create
  - ``GET  /v2/tables/{name}``                        — get/exists
  - ``POST /v2/tables/{name}/data``                   — insert (Kafka async)
  - ``POST /v2/tables/{name}/data/delete``            — delete
  - ``POST /v2/tables/{name}/data/hybrid-search``     — fused dense+sparse

The catalog/data surface lives under ``/v2`` (the only stable Coral
HTTP namespace). Unmatched paths fall through to tonic's gRPC
fallback on the same port and return 200 + ``content-type:
application/grpc`` + ``grpc-status: 12`` — easy to misread as
success. **Always sanity-check `content-type`, not just status
code**, if Coral evolves the prefix again. 0.7.1 shipped without
the ``/v2`` prefix and the smoke E2E falsely PASSed against gRPC
fallback — that miss is what 0.7.2 fixes.

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
``/v2/tables/{name}/data/scan`` or accept ~10-30s eventual
visibility. ``embed_worker`` is fine — it marks `vector_indexed_at`
on Kafka-accept, and the next search-time gap is the same gap any
async indexing pipeline has.

**BM25-only fallback is NOT supported by this driver.** pgvector
ships ``dense IS NULL`` rows + a partial HNSW index when the
embedding API is unavailable, so AKB's ``embed_base_url`` can be left
unset and the BM25 leg still serves results. Coral rejects that path
at the catalog level: ``POST /v2/tables`` with ``embedding`` column
``nullable: true`` returns ``HTTP 400 error_code 400101 "Vector
column 'embedding' must not be nullable"`` (see
``coral-models/src/api/schema.rs`` — the field's own docstring warns
"server may reject it at validation time even if nullable=true").
Both ``upsert_one(dense=None)`` and ``hybrid_search(query_dense=None)``
therefore raise ``VectorStoreUnavailable`` immediately. Operators
who need BM25-only resilience should run pgvector or qdrant; an
``embed_base_url`` outage will fail the upsert path here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

import httpx

from .base import VectorHit, VectorStoreUnavailable, has_dense


logger = logging.getLogger("akb.vector_store.seahorse_db")


def _validate_uuid_for_sql(s: str) -> str:
    """Reject anything that isn't a UUID before interpolating into a
    SQL WHERE clause. AKB source_ids are always UUIDs; this is purely
    defense in depth against any caller mistake."""
    uuid.UUID(s)
    return s


def _encode_sparse_string(
    indices: list[int], values: list[float],
) -> str:
    """Encode AKB's parallel sparse arrays into Coral's
    ``"term_id:weight term_id:weight"`` string format (space-separated,
    one pair per token). Verified against the live Coral hybrid
    search request handler — sparse vectors arrive as a single string
    on this column, not as a list of pairs or as a JSON sub-object."""
    if not indices:
        return ""
    return " ".join(f"{int(t)}:{float(w):.6g}" for t, w in zip(indices, values))


def _chunk_id_to_label(chunk_id: str) -> int:
    """UUID -> SeahorseDB i64 label.

    First 8 bytes of the UUID's binary form, interpreted big-endian as
    **signed** i64. The signedness matters: Coral's JSONL ingest
    parses INT64 columns through Arrow, which rejects unsigned values
    > 2^63 - 1 with ``ComponentError::Arrow`` and surfaces as HTTP 500
    ``error_code 500233 "Internal error"`` with no row context. About
    half of all random UUIDs have a high bit set in their first 8
    bytes, so an unsigned variant of this function reliably 500s on
    roughly that fraction of inserts under sustained load — exactly the
    pattern we filed as SeahorseDB#433. ``signed=True`` keeps the full
    64-bit space addressable on the i64 side and removes that failure
    mode.

    Collisions are birthday-paradox bounded — ~2^32 chunks per table
    before a 50% chance of any pair colliding, far beyond any realistic
    vault. Signedness has no effect on collision probability.
    """
    raw = uuid.UUID(chunk_id).bytes
    return int.from_bytes(raw[:8], "big", signed=True)


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

    @staticmethod
    def _is_grpc_fallback(resp: httpx.Response) -> bool:
        """True when Coral's gRPC fallback served this — REST path was
        unmatched. Surface looks like ``HTTP/1.1 200 OK`` so a naive
        ``status_code == 200`` check passes; the only honest signal is
        the ``content-type`` header (`application/grpc*`)."""
        ct = resp.headers.get("content-type", "")
        return ct.startswith("application/grpc")

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
                resp = await http.get(f"/v2/tables/{self._table}")
            except httpx.HTTPError as e:
                raise VectorStoreUnavailable(
                    f"Coral unreachable at {self._url}: {e}"
                ) from e

            if self._is_grpc_fallback(resp):
                raise VectorStoreUnavailable(
                    f"Coral GET /v2/tables/{self._table} fell "
                    f"through to the gRPC fallback (HTTP REST route "
                    f"unmatched). Check the Coral version + the /v2 "
                    f"prefix used by this driver."
                )
            if resp.status_code == 200:
                self._ensured_collection = True
                return
            if resp.status_code != 404:
                raise VectorStoreUnavailable(
                    f"Coral GET /v2/tables/{self._table} → "
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
                    "/v2/tables", json=create_payload,
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
        """Insert a single record into the AKB-shaped Coral table.

        Coral's ``/data`` insert handler accepts JSONL (one JSON object
        per line) with ``Content-Type: application/x-ndjson`` — plain
        ``application/json`` is rejected with HTTP 400 / error_code
        400102 ``Unsupported Content-Type``. (Arrow IPC stream is also
        accepted but we have no Arrow producer on the AKB side.)

        Sparse vectors are encoded as a single string of
        ``"term_id:weight term_id:weight"`` (space-separated pairs)
        — the format Coral's BM25 sparse index consumes (see
        ``coral-models/src/api/vector.rs`` ``QueryVectors::Sparse``).
        Empty sparse_indices means OOV/empty BM25 — we still need a
        non-null value to satisfy the column's ``nullable=false``, so
        emit an empty string.
        """
        record: dict[str, Any] = {
            "id": _chunk_id_to_label(chunk_id),
            "chunk_id": chunk_id,
            "content": content,
            "section_path": section_path or "",
            "chunk_index": chunk_index,
            "source_type": source_type,
            "source_id": source_id,
            "sparse": _encode_sparse_string(sparse_indices, sparse_values),
        }
        if has_dense(dense):
            record["embedding"] = dense
        else:
            # Coral's CreateTableRequest rejects ``nullable: true`` on
            # vector columns at validation time (HTTP 400 error_code
            # 400101 "Vector column 'embedding' must not be nullable",
            # see module docstring). There's no honest way to upsert
            # a sparse-only row into this schema, so we fail loud and
            # let the worker's per-row failure path catch it instead
            # of Coral's whole-batch rejection with a less specific
            # error. Operators who need BM25-only resilience under an
            # embed-API outage should run pgvector or qdrant.
            raise VectorStoreUnavailable(
                "seahorse-db requires a dense embedding per row "
                "(Coral schema forbids NULL on vector columns). "
                "BM25-only fallback is structurally unsupported by "
                "this driver — use pgvector / qdrant if "
                "embed_base_url may be unset or unreachable."
            )

        body = json.dumps(record, separators=(",", ":"))
        http = await self._http()
        try:
            resp = await http.post(
                f"/v2/tables/{self._table}/data",
                content=body.encode("utf-8"),
                headers={"Content-Type": "application/x-ndjson"},
            )
        except httpx.HTTPError as e:
            raise VectorStoreUnavailable(
                f"Coral POST /v2/tables/{self._table}/data: {e}"
            ) from e
        if self._is_grpc_fallback(resp):
            raise VectorStoreUnavailable(
                f"Coral POST /v2/tables/{self._table}/data fell through "
                f"to the gRPC fallback (HTTP REST route unmatched)."
            )
        if resp.status_code not in (200, 202):
            raise VectorStoreUnavailable(
                f"Coral POST /v2/tables/{self._table}/data → "
                f"{resp.status_code}: {resp.text[:200]}"
            )

    async def delete_point(self, chunk_id: str, *, conn=None) -> None:
        """Coral's data-delete is a SQL WHERE clause string under
        ``delete_condition`` (coral-models/src/api/data.rs
        ``DataDeleteRequest``), NOT a list of primary-key values.

        We use the ``chunk_id`` column (the round-tripped UUID
        string) as the filter — it's stable, exact, and matches how
        AKB tracks the row. SQL string-quoting is single-quote;
        chunk_id values are UUIDs so no escaping required.
        Idempotent: Coral returns 200 even when the row was
        already gone (verified)."""
        # Defensive guard: AKB only ever calls delete_point with a
        # well-formed UUID, but a stray apostrophe would corrupt the
        # WHERE clause. Reject anything that doesn't parse as a UUID.
        uuid.UUID(chunk_id)
        http = await self._http()
        try:
            resp = await http.post(
                f"/v2/tables/{self._table}/data/delete",
                json={"delete_condition": f"chunk_id = '{chunk_id}'"},
            )
        except httpx.HTTPError as e:
            raise VectorStoreUnavailable(
                f"Coral POST /v2/tables/{self._table}/data/delete: {e}"
            ) from e
        if self._is_grpc_fallback(resp):
            raise VectorStoreUnavailable(
                f"Coral POST /v2/tables/{self._table}/data/delete fell "
                f"through to the gRPC fallback (HTTP REST route unmatched)."
            )
        if resp.status_code not in (200, 202):
            raise VectorStoreUnavailable(
                f"Coral POST /v2/tables/{self._table}/data/delete → "
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
        """Coral's hybrid search takes:

        - ``top_k``: int                              (final fused result count)
        - ``dense``: ``DenseVectorSearchConfig``
            - ``column``: name of the dense vector column ("embedding")
            - ``vectors``: list[list[float]]          (batch; we send one)
            - ``parameters.ef_search``: HNSW ef       (optional)
        - ``sparse``: ``SparseVectorSearchConfig``
            - ``column``: "sparse"
            - ``vectors``: list[str]                  ("term_id:weight ..." each)
            - ``parameters``/``metadata``: BM25       (we omit — server
              defaults work without per-query metadata; the alternative
              is shipping `N`, `avgdl`, `df` from `bm25_stats` which is
              wasteful per request)
        - ``fusion``: ``{type: "rrf", parameters: {k}}``
        - ``projection``: SQL projection string       ("col1, col2")
        - ``filter``: SQL WHERE clause                ("source_id IN ('a','b')")

        Response shape (verified):
            ``{"data": {"data": [[ {chunk_id, ..., score}, ... ]]}}``
        — the outer ``data`` wraps the response envelope, the inner
        ``data`` is a list of resultsets (one per query vector). We
        always send one query vector so we read ``[0]``.
        """
        # Dense-only and sparse-only modes both need a non-empty
        # ``vectors`` payload on the empty-side config or Coral
        # returns 400. The single-leg vector-search endpoints
        # (``/v2/tables/{name}/data/indexes/{index}/vector-search``)
        # exist, but the corresponding upsert path is structurally
        # blocked by Coral's NOT NULL on the embedding column (see
        # module docstring), so we can't reach a state where this
        # branch would be useful for AKB — the table can never
        # contain a sparse-only row to retrieve. Failing loud here
        # is consistent with `upsert_one`'s dense=None refusal.
        if not has_dense(query_dense):
            raise VectorStoreUnavailable(
                "seahorse-db hybrid_search requires query_dense "
                "(Coral schema forbids NULL on the embedding column, "
                "so a sparse-only row can never exist to retrieve). "
                "Use pgvector / qdrant for BM25-only search."
            )

        sparse_string = _encode_sparse_string(
            query_sparse_indices, query_sparse_values,
        )

        # BM25 metadata + parameters: Coral defaults gave noticeably
        # worse recall on the 25-scenario hybrid e2e (BM25-en /
        # BM25-ko / cross-vault-B all missed). Ship our corpus stats
        # (`N`, `avgdl`) + the per-term df for the query's vocabulary.
        # Cheap when the vocab is small (typical Kiwi-tokenised
        # Korean queries are 3-8 tokens), one extra PG fetch per
        # search. AKB's stats are already cached in sparse_encoder
        # with a TTL.
        from app.services import sparse_encoder
        bm25_stats = await sparse_encoder.load_stats()
        n_docs = int(bm25_stats.get("total_docs") or 0)
        avgdl = float(bm25_stats.get("avgdl") or 0.0)
        sparse_params: dict[str, Any] = {
            "k": float(bm25_stats.get("k1") or 1.5),
            "b": float(bm25_stats.get("b") or 0.75),
        }
        sparse_metadata: dict[str, Any] | None = None
        if n_docs > 0 and avgdl > 0 and query_sparse_indices:
            df_map = await sparse_encoder.load_df_for_terms(query_sparse_indices)
            # Coral's SparseMetadata.df is `Vec<String>` — one entry per
            # query vector, each string is "term_id:df term_id:df ...".
            df_str = " ".join(
                f"{int(t)}:{int(df_map.get(int(t), 0))}"
                for t in query_sparse_indices
            )
            sparse_metadata = {
                "N": n_docs,
                "avgdl": avgdl,
                "df": [df_str],
            }

        payload: dict[str, Any] = {
            "top_k": limit,
            "dense": {
                "column": "embedding",
                "vectors": [list(query_dense)],
                "parameters": {
                    "ef_search": max(prefetch_per_leg, limit * 2),
                },
            },
            "sparse": {
                "column": "sparse",
                "vectors": [sparse_string or " "],
                "parameters": sparse_params,
            },
            "fusion": {"type": "rrf", "parameters": {"k": 60}},
            "projection": (
                "chunk_id, source_type, source_id, section_path, content"
            ),
        }
        if sparse_metadata is not None:
            payload["sparse"]["metadata"] = sparse_metadata
        if source_ids:
            # SQL WHERE clause — Coral parses this directly. Each
            # `source_id` is a UUID, so the IN list is safe to
            # build by interpolation. Defensive check + quote.
            quoted = ", ".join(f"'{_validate_uuid_for_sql(s)}'" for s in source_ids)
            payload["filter"] = f"source_id IN ({quoted})"

        http = await self._http()
        try:
            resp = await http.post(
                f"/v2/tables/{self._table}/data/hybrid-search",
                json=payload,
            )
        except httpx.HTTPError as e:
            raise VectorStoreUnavailable(
                f"Coral POST /v2/tables/{self._table}/data/hybrid-search: {e}"
            ) from e
        if self._is_grpc_fallback(resp):
            raise VectorStoreUnavailable(
                "Coral POST hybrid-search fell through to gRPC fallback."
            )
        if resp.status_code != 200:
            raise VectorStoreUnavailable(
                f"Coral POST /v2/tables/{self._table}/data/hybrid-search → "
                f"{resp.status_code}: {resp.text[:200]}"
            )

        body = resp.json()
        # Envelope: top-level `data` wraps the response; inner `data`
        # is a list-of-resultsets (one per query vector). One query.
        outer = body.get("data") or {}
        resultsets = outer.get("data") or []
        rows = resultsets[0] if resultsets else []

        hits: list[VectorHit] = []
        for row in rows:
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
        """AKB-shaped table — wire format matches Coral's
        ``CreateTableRequest`` (coral-models/src/api/schema.rs).

        Shape (verified against the live Coral that ships with
        SeahorseDB SDDEV-244/monorepo-coral-sparse):

            table_name:  str
            columns:     list[Column]            top-level (not nested)
                Column = {name, type, nullable, max_length?}
                type:
                  scalar -> "INT64" | "FLOAT64" | "BOOL" | "STRING"
                            (SCREAMING_SNAKE_CASE)
                  dense  -> {"name": "DENSE_VECTOR",
                             "element": "FLOAT32",
                             "dim": int}
                  sparse -> {"name": "SPARSE_VECTOR"}
            primary_key: list[str]               separate from per-column flag
            indexes:     list[ExternalIndex]
                ExternalIndex = {type, column, params?}
                  type: "HNSW" | "INVERTED" | "DISKBASED"
                  params: {space?, ef_construction?, M?, sparse_model?}
                    space: "cosine" | "l2" | "ip"
        """
        return {
            "table_name": self._table,
            # Coral only accepts STRING primary keys (verified — POST /v2/tables
            # returns "Primary key column 'X' must be of type STRING" on
            # any other PK type). So `chunk_id` (UUID string) IS the PK,
            # and there's no separate u64 label column — deletes and PK
            # lookups reference chunk_id directly via the SQL
            # delete_condition payload.
            # `id` (INT64) is the per-row label hashed from chunk_id;
            # `chunk_id` (STRING) round-trips the UUID so hybrid_search
            # responses can carry it back to AKB unchanged. Coral
            # implicitly treats segmentation.columns as the primary
            # key when `primary_key` is omitted — matching the e2e
            # `cloud-functional-test::hybrid` reference shape.
            "columns": [
                {"name": "id", "type": "INT64", "nullable": False},
                {"name": "chunk_id", "type": "STRING", "nullable": False},
                {"name": "embedding",
                 "type": {"name": "DENSE_VECTOR", "element": "FLOAT32",
                          "dim": self._dense_dim},
                 "nullable": False},
                {"name": "sparse",
                 "type": {"name": "SPARSE_VECTOR"},
                 "nullable": False},
                {"name": "content", "type": "STRING", "nullable": True},
                {"name": "section_path", "type": "STRING", "nullable": True},
                {"name": "chunk_index", "type": "INT64", "nullable": True},
                {"name": "source_type", "type": "STRING", "nullable": False},
                {"name": "source_id", "type": "STRING", "nullable": False},
            ],
            "segmentation": {
                "strategy": "hash",
                "columns": ["id"],
                "buckets": 1,
                "composition": "single",
            },
            "indexes": [
                {
                    # Lowercase index type strings match the reference
                    # e2e and the round-trip GET response shape; the
                    # Rust enum is case-insensitive but `inverted` /
                    # `hnsw` is the canonical form.
                    "type": "hnsw",
                    "column": "embedding",
                    "params": {
                        "space": self._distance,  # ip | l2 — HNSW
                                                  # rejects cosine
                                                  # at segment build.
                        "ef_construction": 64,
                        "M": 16,
                    },
                },
                {
                    "type": "inverted",
                    "column": "sparse",
                    # Coral rejects hybrid-search with
                    # `BM25 sparse scoring requires sparse_model=bm25 index`
                    # when this is omitted, even though the index is
                    # created either way. Pin to bm25 explicitly so the
                    # search-time path can apply our (k, b, N, avgdl, df)
                    # metadata.
                    "params": {"sparse_model": "bm25"},
                },
            ],
        }
