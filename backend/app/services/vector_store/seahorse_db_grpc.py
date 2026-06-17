"""SeahorseDB (self-hosted) driver — gRPC variant.

Sibling of ``seahorse_db.py`` (the REST/JSONL driver shipped in 0.7.x).
The two drivers target the **same Coral coordinator** on the **same
port** — Coral merges axum (HTTP REST) and tonic (gRPC) onto a single
listener, so the user-facing endpoint config
(``seahorsedb_coordinator_url``) is shared and only the wire format
changes.

Why a second driver
-------------------
0.7.7's release notes called out two real client bugs the REST
driver hit because the wire happens to be JSON — i64 overflow on
the PK column (``arrow_json::Decoder`` rejects unsigned > 2^63 - 1
with a generic 500233) and double-IDF/double-saturation on BM25
sparse weights. Both were real bugs in our encoder, but typed
gRPC (protobuf int64 for the PK, Arrow IPC for the streaming
results, explicit ``DenseQueryVectors`` for the dense leg) makes
the type contract obvious at the wire boundary instead of letting
"any uint64 value" reach Coral as untyped JSON. The grpc driver
keeps using ``InsertJsonl`` (not ``InsertArrowIpc``) for the
insert path so the encoder is shared with the REST driver and
the 0.7.7 fixes (signed-i64 label, raw-mode BM25) apply
identically.

Wire mapping (Coral protos, ``coral/proto/coral/*/v1/*.proto``):

  - ``health.HealthService.Check``        — health probe
  - ``table.TableService.GetTable``       — exists check
  - ``table.TableService.CreateTable``    — schema provision (typed
                                            ``CreateTableSpec``, no
                                            Arrow IPC required for
                                            the schema itself)
  - ``ingest.IngestService.InsertJsonl``  — single-record upsert
                                            (same JSONL bytes the
                                            REST driver ships)
  - ``ingest.IngestService.DeleteTableData`` — delete by SQL WHERE
  - ``query.QueryService.HybridSearch``   — server streaming;
                                            chunks are Arrow IPC
                                            stream bytes that
                                            pyarrow decodes into
                                            VectorHit rows.

Status
------
Experimental — opt-in via ``vector_store_driver: seahorse-db-grpc``.
The REST driver (``seahorse-db``) remains the documented production
path. Local validation precedes any commit; CI does not gate this
driver yet.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

import grpc
import pyarrow as pa

# Generated protobuf stubs use absolute ``coral.foo.v1.foo_pb2``
# imports, so the ``_grpc/proto`` root must be on ``sys.path`` while
# they load. We add it, import the stubs, then remove it — Python
# caches the imported modules in ``sys.modules`` so subsequent uses
# don't re-hit the path. Cleaning up keeps the ``coral`` top-level
# package name out of the global resolution path for everything else
# in the process.
#
# This is a workaround for the generated-code import style. The
# production-grade fix is to re-emit the stubs with relative imports
# (e.g. via ``protoc --python_out=... -I=...`` rooted under
# ``app.services.vector_store._grpc`` and a small ``sed`` pass to
# rewrite ``from coral.x.v1`` to ``from .coral.x.v1``). Filed as a
# follow-up so the dogfood window doesn't block on it.
_PROTO_ROOT = str(Path(__file__).parent / "_grpc" / "proto")
_added_to_path = False
if _PROTO_ROOT not in sys.path:
    sys.path.insert(0, _PROTO_ROOT)
    _added_to_path = True
try:
    from coral.catalog.v1 import catalog_pb2  # noqa: E402
    from coral.health.v1 import health_pb2, health_pb2_grpc  # noqa: E402
    from coral.ingest.v1 import ingest_pb2, ingest_pb2_grpc  # noqa: E402
    from coral.query.v1 import query_pb2, query_pb2_grpc  # noqa: E402
    from coral.table.v1 import table_pb2, table_pb2_grpc  # noqa: E402
finally:
    if _added_to_path:
        try:
            sys.path.remove(_PROTO_ROOT)
        except ValueError:
            pass

from ._seahorse_common import (
    chunk_id_to_label as _chunk_id_to_label,
    encode_sparse_string as _encode_sparse_string,
    vault_filter_sql as _vault_filter_sql,
)
from .base import ChunkUpsert, VectorHit, VectorStoreUnavailable, has_dense


logger = logging.getLogger("akb.vector_store.seahorse_db_grpc")


# Coral's catalog uses SCREAMING_SNAKE enum names for scalar columns.
# These match the values accepted by the REST driver's
# ``_build_create_table_payload`` — one source of truth for the AKB
# schema, two transports.
# Coral's catalog enums prefix every name with the enum type (proto3
# convention). Keep a short alias map so the column builders read
# naturally — `INT64` instead of `SCALAR_TYPE_INT64`.
_SCALAR_NAME_TO_ENUM: dict[str, int] = {
    "INT64": catalog_pb2.ScalarType.Value("SCALAR_TYPE_INT64"),
    "STRING": catalog_pb2.ScalarType.Value("SCALAR_TYPE_STRING"),
    # extend here if AKB ever adds FLOAT64 / BOOL columns.
}


def _column_scalar(name: str, scalar: str, nullable: bool) -> catalog_pb2.Column:
    return catalog_pb2.Column(
        name=name,
        column_type=catalog_pb2.ColumnType(
            scalar=_SCALAR_NAME_TO_ENUM[scalar],
        ),
        nullable=nullable,
    )


def _column_dense(name: str, dim: int, nullable: bool) -> catalog_pb2.Column:
    return catalog_pb2.Column(
        name=name,
        column_type=catalog_pb2.ColumnType(
            dense_vector=catalog_pb2.DenseVectorType(
                element=catalog_pb2.VectorElement.Value("VECTOR_ELEMENT_FLOAT32"),
                dim=dim,
            ),
        ),
        nullable=nullable,
    )


def _column_sparse(name: str, nullable: bool) -> catalog_pb2.Column:
    return catalog_pb2.Column(
        name=name,
        column_type=catalog_pb2.ColumnType(
            sparse_vector=catalog_pb2.SparseVectorType(),
        ),
        nullable=nullable,
    )


class SeahorseDbGrpcStore:
    """VectorStore driver targeting Coral over gRPC.

    Construction is config-only. ``ensure_collection`` runs the
    GetTable + CreateTable round-trip on first use; subsequent calls
    cheap-cache.
    """

    # Stores vault_id and filters on it in hybrid_search (issue #189 Phase 2).
    # Capable, but does NOT expose vault_backfill_pending(): the generated gRPC
    # stubs here have no SqlService (no count primitive) and Coral has no scan
    # RPC, so the readiness worker keeps this driver gated on the safe source-id
    # path until a count is wired (recreate+reindex + a SQL/scan count, then it
    # auto-activates). vault_id storage + filtering are still correct meanwhile.
    vault_filter_supported = True

    def __init__(
        self,
        *,
        coordinator_url: str,
        table_name: str,
        dense_dim: int,
        distance: str = "ip",
        auto_create: bool = True,
        timeout: float = 30.0,
    ):
        # Coral's REST and gRPC share the same port. The user-facing
        # config takes a URL (because the REST driver uses it as one);
        # we strip down to ``host:port`` for ``insecure_channel`` —
        # any scheme, any trailing path. urlsplit has a footgun:
        # without ``//`` it treats ``host:port`` as ``scheme:path``,
        # so we pre-normalise by adding ``//`` whenever the input
        # doesn't already carry it.
        url = coordinator_url
        if "://" not in url:
            url = "//" + url
        parsed = urllib.parse.urlsplit(url)
        self._endpoint = parsed.netloc or parsed.path.split("/", 1)[0]
        self._table = table_name
        self._dense_dim = dense_dim
        self._distance = distance
        self._auto_create = auto_create
        self._timeout = timeout

        self._channel: grpc.aio.Channel | None = None
        self._ensured_collection = False
        self._ensure_lock = asyncio.Lock()

    async def _ch(self) -> grpc.aio.Channel:
        if self._channel is None:
            # Plaintext H2 — Coral's listener is the same TCP socket
            # axum uses, so TLS posture follows the cluster's reverse
            # proxy. Inside the cluster we go plaintext.
            self._channel = grpc.aio.insecure_channel(self._endpoint)
        return self._channel

    async def aclose(self) -> None:
        """Test-only: close the gRPC channel. Production path keeps it
        open for the process lifetime."""
        if self._channel is not None:
            await self._channel.close()
            self._channel = None

    # ── VectorStore Protocol ──────────────────────────────────────

    async def ensure_collection(self, *, conn=None) -> None:
        if self._ensured_collection:
            return
        async with self._ensure_lock:
            if self._ensured_collection:
                return
            ch = await self._ch()
            ts = table_pb2_grpc.TableServiceStub(ch)
            try:
                await ts.GetTable(
                    table_pb2.GetTableRequest(table_name=self._table),
                    timeout=self._timeout,
                )
                self._ensured_collection = True
                return
            except grpc.aio.AioRpcError as e:
                if e.code() != grpc.StatusCode.NOT_FOUND:
                    raise VectorStoreUnavailable(
                        f"Coral gRPC GetTable {self._table!r}: "
                        f"{e.code().name} {e.details()}"
                    ) from e

            if not self._auto_create:
                raise VectorStoreUnavailable(
                    f"table {self._table!r} absent on Coral and "
                    f"seahorsedb_auto_create=false; create it manually."
                )

            try:
                await ts.CreateTable(
                    self._build_create_request(),
                    timeout=self._timeout,
                )
            except grpc.aio.AioRpcError as e:
                # ALREADY_EXISTS is benign — peer beat us to it.
                if e.code() != grpc.StatusCode.ALREADY_EXISTS:
                    raise VectorStoreUnavailable(
                        f"Coral gRPC CreateTable {self._table!r}: "
                        f"{e.code().name} {e.details()}"
                    ) from e
            self._ensured_collection = True

    async def health(self) -> bool:
        try:
            ch = await self._ch()
            stub = health_pb2_grpc.HealthServiceStub(ch)
            await stub.Check(health_pb2.HealthCheckRequest(), timeout=5.0)
            return True
        except grpc.aio.AioRpcError as e:
            logger.warning(
                "Coral gRPC health: %s %s", e.code().name, e.details(),
            )
            return False
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
        vault_id: str,
    ) -> None:
        """Single-record insert. JSONL bytes carried over
        ``IngestService.InsertJsonl`` — identical to the REST
        driver's body, so the same ``_chunk_id_to_label(signed=True)``
        fix and the same raw-mode BM25 ``sparse_encoder`` output
        apply unchanged here.
        """
        if not has_dense(dense):
            raise VectorStoreUnavailable(
                "seahorse-db-grpc requires a dense embedding per row "
                "(Coral schema forbids NULL on vector columns)."
            )

        record: dict[str, Any] = {
            "id": _chunk_id_to_label(chunk_id),
            "chunk_id": chunk_id,
            "embedding": dense,
            "sparse": _encode_sparse_string(sparse_indices, sparse_values),
            "content": content,
            "section_path": section_path or "",
            "chunk_index": chunk_index,
            "source_type": source_type,
            "source_id": source_id,
            "vault_id": vault_id,
        }
        jsonl = (
            json.dumps(record, separators=(",", ":")) + "\n"
        ).encode("utf-8")

        ch = await self._ch()
        stub = ingest_pb2_grpc.IngestServiceStub(ch)
        try:
            await stub.InsertJsonl(
                ingest_pb2.InsertJsonlRequest(
                    table_name=self._table,
                    jsonl=jsonl,
                ),
                timeout=self._timeout,
            )
        except grpc.aio.AioRpcError as e:
            raise VectorStoreUnavailable(
                f"Coral gRPC InsertJsonl: {e.code().name} {e.details()}"
            ) from e

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
        # Defensive UUID check — same shape as REST driver.
        uuid.UUID(chunk_id)
        ch = await self._ch()
        stub = ingest_pb2_grpc.IngestServiceStub(ch)
        try:
            await stub.DeleteTableData(
                ingest_pb2.DeleteTableDataRequest(
                    table_name=self._table,
                    delete_condition=f"chunk_id = '{chunk_id}'",
                ),
                timeout=self._timeout,
            )
        except grpc.aio.AioRpcError as e:
            raise VectorStoreUnavailable(
                f"Coral gRPC DeleteTableData: {e.code().name} {e.details()}"
            ) from e

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
        """Server-streaming search. Each ``ResultStreamEvent`` carries
        one of ``header`` / ``chunk`` / ``result_set_boundary`` /
        ``footer``. Each ``chunk`` carries an Arrow IPC stream bytes
        payload that pyarrow decodes into a record batch; we project
        columns to ``VectorHit``.
        """
        if not has_dense(query_dense):
            raise VectorStoreUnavailable(
                "seahorse-db-grpc hybrid_search requires query_dense "
                "(Coral schema forbids NULL on the embedding column)."
            )

        # BM25 metadata — same convention as REST driver: ship our
        # corpus stats (N, avgdl) + the per-term df, so the index
        # applies the canonical scoring instead of falling back to
        # defaults the local stats don't match.
        from app.services import sparse_encoder
        bm25_stats = await sparse_encoder.load_stats()
        n_docs = int(bm25_stats.get("total_docs") or 0)
        avgdl = float(bm25_stats.get("avgdl") or 0.0)
        sparse_string = _encode_sparse_string(
            query_sparse_indices, query_sparse_values,
        )

        # DenseVectorSearchConfig.vectors is a `repeated FloatVector`
        # (not a wrapper message) and SparseVectorSearchConfig.vectors
        # is a `repeated string` — different shape from the VectorQuery
        # message in the same proto (which DOES wrap them under
        # DenseQueryVectors / SparseQueryVectors). The two contexts
        # diverge in the proto; the SearchConfig path takes the raw
        # vector list.
        dense_cfg = query_pb2.DenseVectorSearchConfig(
            column="embedding",
            vectors=[query_pb2.FloatVector(values=list(query_dense))],
            parameters=query_pb2.DenseSearchParameters(
                ef_search=max(prefetch_per_leg, limit * 2),
            ),
        )
        sparse_cfg = query_pb2.SparseVectorSearchConfig(
            column="sparse",
            vectors=[sparse_string or " "],
            parameters=query_pb2.SparseSearchParameters(
                k=float(bm25_stats.get("k1") or 1.5),
                b=float(bm25_stats.get("b") or 0.75),
            ),
        )
        if n_docs > 0 and avgdl > 0 and query_sparse_indices:
            df_map = await sparse_encoder.load_df_for_terms(query_sparse_indices)
            df_str = " ".join(
                f"{int(t)}:{int(df_map.get(int(t), 0))}"
                for t in query_sparse_indices
            )
            sparse_cfg.metadata.CopyFrom(query_pb2.SparseMetadata(
                n=n_docs, avgdl=avgdl, df=[df_str],
            ))

        request = query_pb2.HybridSearchRequest(
            table_name=self._table,
            top_k=limit,
            dense=dense_cfg,
            sparse=sparse_cfg,
            # RRF ``k`` pinned to 60 — same value the REST driver uses
            # (seahorse_db.py:464). Leaving it as the server default
            # silently shifts fused ordering vs the REST stream and
            # would create cross-driver drift on the same corpus.
            fusion=query_pb2.FusionConfig(
                type="rrf",
                parameters=query_pb2.FusionParameters(k=60),
            ),
            projection=(
                "chunk_id, source_type, source_id, section_path, content"
            ),
        )
        # ACL pre-filter (issue #189 Phase 2): exactly one of vault_ids /
        # source_ids; vault_filter_sql asserts that + UUID-validates each id.
        acl = _vault_filter_sql(vault_ids, source_ids)
        if acl is not None:
            request.filter = acl

        ch = await self._ch()
        stub = query_pb2_grpc.QueryServiceStub(ch)
        hits: list[VectorHit] = []
        try:
            stream = stub.HybridSearch(request, timeout=self._timeout)
            async for event in stream:
                kind = event.WhichOneof("event")
                if kind != "chunk":
                    # header, result_set_boundary, footer — nothing to
                    # decode. Header carries num_result_sets but we
                    # only ever issue one query vector.
                    continue
                hits.extend(_decode_chunk_to_hits(event.chunk.chunk))
        except grpc.aio.AioRpcError as e:
            raise VectorStoreUnavailable(
                f"Coral gRPC HybridSearch: {e.code().name} {e.details()}"
            ) from e

        return hits[:limit]

    # ── helpers ────────────────────────────────────────────────────

    def _build_create_request(self) -> table_pb2.CreateTableRequest:
        """Construct the typed CreateTable request. Columns and
        index/segmentation parameters mirror the REST driver's JSON
        payload — same AKB table schema, two transports."""
        spec = catalog_pb2.CreateTableSpec(
            table_name=self._table,
            columns=[
                _column_scalar("id", "INT64", nullable=False),
                _column_scalar("chunk_id", "STRING", nullable=False),
                _column_dense("embedding", self._dense_dim, nullable=False),
                _column_sparse("sparse", nullable=False),
                _column_scalar("content", "STRING", nullable=True),
                _column_scalar("section_path", "STRING", nullable=True),
                _column_scalar("chunk_index", "INT64", nullable=True),
                _column_scalar("source_type", "STRING", nullable=False),
                _column_scalar("source_id", "STRING", nullable=False),
                # vault_id (issue #189 Phase 2): per-vault ACL filter key.
                _column_scalar("vault_id", "STRING", nullable=True),
            ],
            segmentation=catalog_pb2.ExternalSegmentation(
                strategy=catalog_pb2.SegmentationStrategy.Value(
                    "SEGMENTATION_STRATEGY_HASH"
                ),
                columns=["id"],
                buckets=1,
                composition=catalog_pb2.SegmentationComposition.Value(
                    "SEGMENTATION_COMPOSITION_SINGLE"
                ),
            ),
            indexes=[
                catalog_pb2.ExternalIndex(
                    type="hnsw",
                    column="embedding",
                    params=catalog_pb2.IndexParams(
                        # space: ip | l2 — HNSW rejects cosine at
                        # segment build, see REST driver docstring.
                        space=self._distance,
                        ef_construction=64,
                        m=16,
                    ),
                ),
                catalog_pb2.ExternalIndex(
                    type="inverted",
                    column="sparse",
                    # Coral's hybrid_search rejects BM25 sparse scoring
                    # without `sparse_model=bm25` on the index; pin it
                    # explicitly so the search-time (k, b, N, avgdl, df)
                    # metadata applies.
                    params=catalog_pb2.IndexParams(sparse_model="bm25"),
                ),
            ],
        )
        return table_pb2.CreateTableRequest(table=spec)


def _decode_chunk_to_hits(arrow_ipc_bytes: bytes) -> list[VectorHit]:
    """Decode one streamed ResultStreamChunk's Arrow IPC bytes into
    ``VectorHit`` objects. Column set follows the ``projection`` we
    ship in the HybridSearch request; Coral appends a ``score``
    column for fused ranking results.
    """
    reader = pa.ipc.open_stream(pa.BufferReader(arrow_ipc_bytes))
    table = reader.read_all()
    cols = set(table.column_names)
    if "score" not in cols:
        # Coral *should* append a score column for hybrid-search
        # results; if it doesn't, ordering still works (iteration
        # order is the server's ranking) but ``hit.score`` becomes
        # silently 0.0 and any downstream caller that thresholds on
        # score will misbehave. Surface this loudly the first time so
        # operators notice schema drift.
        logger.warning(
            "Coral hybrid-search response has no `score` column "
            "(columns=%s); VectorHit.score will be 0.0",
            sorted(cols),
        )
    hits: list[VectorHit] = []
    for r in table.to_pylist():
        hits.append(VectorHit(
            chunk_id=str(r.get("chunk_id", "")),
            source_type=str(r.get("source_type", "")),
            source_id=str(r.get("source_id", "")),
            # `section_path` is nullable in the schema; the VectorHit
            # dataclass declares it as `str`, so coerce None to "".
            # Same shape as the REST driver — see seahorse_db.py.
            section_path=r.get("section_path") or "",
            content=r.get("content") or "",
            score=float(r.get("score") or 0.0),
        ))
    return hits
