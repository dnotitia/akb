"""Unit tests for the ``seahorse-db-grpc`` driver.

These tests exercise the driver's wire-format choices without
needing a live Coral on the network. Every gRPC stub is mocked, so:

  - CI can run them — no port 53286 dependency.
  - A wire mistake (wrong column name, missing RRF k, mis-coerced
    label sign) trips a focused failure here instead of waiting
    for the e2e or — worse — a recall regression in production.

End-to-end behaviour against a real Coral is covered separately by
``backend/tests/test_hybrid_search_e2e.sh`` (the same 25-scenario
harness the REST driver passes). That harness needs a backend pod;
the unit tests here run against pure Python.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Skip cleanly (not a collection ERROR) where the gRPC stack is unavailable:
# grpc/pyarrow missing, OR the generated protobuf stubs reject the installed
# protobuf runtime (the local `runtime_version` ImportError — see
# [[feedback_local_skip_ci_driver_tests]]). CI installs the pinned deps and runs
# this in full.
grpc = pytest.importorskip("grpc")
pa = pytest.importorskip("pyarrow")

# Stub-loading side-effect: importing the driver inserts the proto root onto
# sys.path inside a try/finally. Guard it so a protobuf-version mismatch skips
# the module instead of erroring at collection.
try:
    from app.services.vector_store import seahorse_db_grpc as sdgrpc
except ImportError as _e:  # pragma: no cover - env-dependent
    pytest.skip(f"coral gRPC stubs unimportable: {_e}", allow_module_level=True)

from app.services.vector_store.base import VectorHit


# Re-import the generated proto modules locally for assertions —
# they were already loaded by the driver's import above, so this
# is a cheap lookup, not a second sys.path mutation.
_PROTO_ROOT = str(Path(sdgrpc.__file__).parent / "_grpc" / "proto")
if _PROTO_ROOT not in sys.path:
    sys.path.insert(0, _PROTO_ROOT)
from coral.catalog.v1 import catalog_pb2  # noqa: E402
from coral.ingest.v1 import ingest_pb2  # noqa: E402
from coral.query.v1 import query_pb2  # noqa: E402
from coral.table.v1 import table_pb2  # noqa: E402


# ── helpers (column builders) ───────────────────────────────────


def test_column_scalar_int64() -> None:
    col = sdgrpc._column_scalar("id", "INT64", nullable=False)
    assert col.name == "id"
    assert col.nullable is False
    assert col.column_type.scalar == catalog_pb2.ScalarType.Value("SCALAR_TYPE_INT64")


def test_column_scalar_string_nullable() -> None:
    col = sdgrpc._column_scalar("content", "STRING", nullable=True)
    assert col.nullable is True
    assert col.column_type.scalar == catalog_pb2.ScalarType.Value("SCALAR_TYPE_STRING")


def test_column_dense_float32() -> None:
    col = sdgrpc._column_dense("embedding", dim=1024, nullable=False)
    assert col.nullable is False
    dv = col.column_type.dense_vector
    assert dv.dim == 1024
    assert dv.element == catalog_pb2.VectorElement.Value("VECTOR_ELEMENT_FLOAT32")


def test_column_sparse() -> None:
    col = sdgrpc._column_sparse("sparse", nullable=False)
    assert col.nullable is False
    # SparseVectorType has no fields; presence is the assertion.
    assert col.column_type.HasField("sparse_vector")


def test_unknown_scalar_raises() -> None:
    with pytest.raises(KeyError):
        sdgrpc._column_scalar("x", "FLOAT64", nullable=False)


# ── endpoint URL parsing ────────────────────────────────────────


@pytest.mark.parametrize(
    "url, expected",
    [
        ("http://coral.ns.svc:3003", "coral.ns.svc:3003"),
        ("https://coral.ns.svc:443", "coral.ns.svc:443"),
        ("grpc://x.y.z:3003", "x.y.z:3003"),
        ("grpcs://x.y.z:443", "x.y.z:443"),
        ("coral:3003", "coral:3003"),
        ("host.docker.internal:53286", "host.docker.internal:53286"),
        ("localhost:53286", "localhost:53286"),
        ("http://coral:3003/v2", "coral:3003"),
        ("http://coral:3003/", "coral:3003"),
    ],
)
def test_endpoint_url_parsing(url: str, expected: str) -> None:
    s = sdgrpc.SeahorseDbGrpcStore(
        coordinator_url=url, table_name="t", dense_dim=1024,
    )
    assert s._endpoint == expected


# ── CreateTable schema parity with REST driver ─────────────────


def test_vault_filter_capability_flag() -> None:
    # Stores vault_id + filters on it (issue #189 Phase 2); no count primitive
    # here (no SqlService stub), so the readiness worker keeps it gated.
    assert sdgrpc.SeahorseDbGrpcStore.vault_filter_supported is True


def test_create_table_request_shape() -> None:
    s = sdgrpc.SeahorseDbGrpcStore(
        coordinator_url="localhost:53286",
        table_name="akb_chunks",
        dense_dim=1024,
        distance="ip",
        auto_create=True,
    )
    req = s._build_create_request()
    assert isinstance(req, table_pb2.CreateTableRequest)
    spec = req.table
    assert spec.table_name == "akb_chunks"

    cols = {c.name: c for c in spec.columns}
    assert set(cols) == {
        "id", "chunk_id", "embedding", "sparse",
        "content", "section_path", "chunk_index",
        "source_type", "source_id", "vault_id",
    }
    # Nullability matches the REST driver's payload.
    assert cols["id"].nullable is False
    assert cols["chunk_id"].nullable is False
    assert cols["embedding"].nullable is False
    assert cols["sparse"].nullable is False
    assert cols["content"].nullable is True
    assert cols["section_path"].nullable is True
    assert cols["chunk_index"].nullable is True
    assert cols["source_type"].nullable is False
    assert cols["source_id"].nullable is False
    # vault_id (issue #189 Phase 2): nullable so recreate+reindex can land it.
    assert cols["vault_id"].nullable is True
    # Dense vector dim flows through from constructor.
    assert cols["embedding"].column_type.dense_vector.dim == 1024

    seg = spec.segmentation
    assert seg.strategy == catalog_pb2.SegmentationStrategy.Value(
        "SEGMENTATION_STRATEGY_HASH",
    )
    assert list(seg.columns) == ["id"]
    assert seg.buckets == 1
    assert seg.composition == catalog_pb2.SegmentationComposition.Value(
        "SEGMENTATION_COMPOSITION_SINGLE",
    )

    idxs = {(i.type, i.column): i for i in spec.indexes}
    # HNSW on embedding with the requested distance space.
    hnsw = idxs[("hnsw", "embedding")]
    assert hnsw.params.space == "ip"
    assert hnsw.params.ef_construction == 64
    assert hnsw.params.m == 16
    # Inverted index on sparse with sparse_model=bm25.
    inv = idxs[("inverted", "sparse")]
    assert inv.params.sparse_model == "bm25"


def test_create_table_request_distance_l2() -> None:
    s = sdgrpc.SeahorseDbGrpcStore(
        coordinator_url="localhost:53286",
        table_name="t",
        dense_dim=512,
        distance="l2",
    )
    req = s._build_create_request()
    hnsw = next(i for i in req.table.indexes if i.type == "hnsw")
    assert hnsw.params.space == "l2"


# ── _decode_chunk_to_hits — Arrow IPC round-trip ────────────────


def _encode_record_batch(rows: list[dict], with_score: bool = True) -> bytes:
    """Build an Arrow IPC stream from a list of result rows.

    Mirrors the shape Coral emits in a ``ResultStreamChunk`` —
    columns chosen to match the projection the driver sends.
    """
    if with_score:
        schema = pa.schema([
            pa.field("chunk_id", pa.string()),
            pa.field("source_type", pa.string()),
            pa.field("source_id", pa.string()),
            pa.field("section_path", pa.string()),
            pa.field("content", pa.string()),
            pa.field("score", pa.float64()),
        ])
    else:
        schema = pa.schema([
            pa.field("chunk_id", pa.string()),
            pa.field("source_type", pa.string()),
            pa.field("source_id", pa.string()),
            pa.field("section_path", pa.string()),
            pa.field("content", pa.string()),
        ])
    arrays = [pa.array([r.get(f.name) for r in rows], type=f.type)
              for f in schema]
    batch = pa.RecordBatch.from_arrays(arrays, schema=schema)
    sink = io.BytesIO()
    with pa.ipc.new_stream(sink, schema) as writer:
        writer.write_batch(batch)
    return sink.getvalue()


def test_decode_chunk_simple() -> None:
    rows = [
        {
            "chunk_id": "aaaaaaaa-bbbb-cccc-dddd-000000000010",
            "source_type": "document",
            "source_id": "11111111-2222-3333-4444-555555555555",
            "section_path": "intro",
            "content": "Hello",
            "score": 0.85,
        },
    ]
    hits = sdgrpc._decode_chunk_to_hits(_encode_record_batch(rows))
    assert len(hits) == 1
    h = hits[0]
    assert isinstance(h, VectorHit)
    assert h.chunk_id == rows[0]["chunk_id"]
    assert h.source_type == "document"
    assert h.section_path == "intro"
    assert h.content == "Hello"
    assert h.score == pytest.approx(0.85)


def test_decode_chunk_section_path_null() -> None:
    """Regression for the section_path=None foot-gun: nullable in the
    schema, str-typed on VectorHit, so the decoder must coerce."""
    rows = [{
        "chunk_id": "aaaaaaaa-bbbb-cccc-dddd-000000000020",
        "source_type": "document",
        "source_id": "11111111-2222-3333-4444-555555555555",
        "section_path": None,
        "content": None,
        "score": 0.1,
    }]
    hits = sdgrpc._decode_chunk_to_hits(_encode_record_batch(rows))
    assert hits[0].section_path == ""
    assert hits[0].content == ""


def test_decode_chunk_missing_score_warns(caplog: pytest.LogCaptureFixture) -> None:
    rows = [{
        "chunk_id": "aaaaaaaa-bbbb-cccc-dddd-000000000030",
        "source_type": "document",
        "source_id": "11111111-2222-3333-4444-555555555555",
        "section_path": "",
        "content": "x",
    }]
    caplog.set_level("WARNING", logger="akb.vector_store.seahorse_db_grpc")
    hits = sdgrpc._decode_chunk_to_hits(_encode_record_batch(rows, with_score=False))
    assert hits[0].score == 0.0
    assert any("no `score` column" in r.message for r in caplog.records)


# ── 5 Protocol methods — wire assertions via mocked stubs ────────


@pytest.fixture
def store() -> sdgrpc.SeahorseDbGrpcStore:
    """Driver under test. ``_ch()`` is short-circuited so the stubs
    constructed inside each Protocol method receive a mock channel —
    the stubs themselves are then independently swapped out per test
    via ``monkeypatch``."""
    return sdgrpc.SeahorseDbGrpcStore(
        coordinator_url="localhost:53286",
        table_name="akb_chunks_test",
        dense_dim=4,  # tiny so dense vectors are readable in failures
        distance="ip",
        auto_create=True,
        timeout=2.0,
    )


@pytest.fixture
def mock_channel(monkeypatch: pytest.MonkeyPatch, store: sdgrpc.SeahorseDbGrpcStore) -> MagicMock:
    """Replace ``store._ch()`` with a coroutine returning a MagicMock
    channel — stubs get constructed with this dummy and have their
    methods overridden per test."""
    ch = MagicMock(name="channel")
    async def _ch() -> MagicMock:
        return ch
    monkeypatch.setattr(store, "_ch", _ch)
    return ch


@pytest.mark.asyncio
async def test_health_ok(monkeypatch: pytest.MonkeyPatch,
                          store: sdgrpc.SeahorseDbGrpcStore,
                          mock_channel: MagicMock) -> None:
    stub = MagicMock()
    stub.Check = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(sdgrpc.health_pb2_grpc, "HealthServiceStub",
                        lambda _ch: stub)
    assert await store.health() is True
    stub.Check.assert_awaited_once()


@pytest.mark.asyncio
async def test_health_grpc_error_returns_false(
    monkeypatch: pytest.MonkeyPatch,
    store: sdgrpc.SeahorseDbGrpcStore,
    mock_channel: MagicMock,
) -> None:
    err = grpc.aio.AioRpcError(
        code=grpc.StatusCode.UNAVAILABLE,
        initial_metadata=grpc.aio.Metadata(),
        trailing_metadata=grpc.aio.Metadata(),
        details="connection refused",
    )
    stub = MagicMock()
    stub.Check = AsyncMock(side_effect=err)
    monkeypatch.setattr(sdgrpc.health_pb2_grpc, "HealthServiceStub",
                        lambda _ch: stub)
    assert await store.health() is False


@pytest.mark.asyncio
async def test_ensure_collection_exists_no_create(
    monkeypatch: pytest.MonkeyPatch,
    store: sdgrpc.SeahorseDbGrpcStore,
    mock_channel: MagicMock,
) -> None:
    stub = MagicMock()
    stub.GetTable = AsyncMock(return_value=MagicMock())
    stub.CreateTable = AsyncMock()
    monkeypatch.setattr(sdgrpc.table_pb2_grpc, "TableServiceStub",
                        lambda _ch: stub)
    await store.ensure_collection()
    stub.GetTable.assert_awaited_once()
    stub.CreateTable.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_collection_creates_on_not_found(
    monkeypatch: pytest.MonkeyPatch,
    store: sdgrpc.SeahorseDbGrpcStore,
    mock_channel: MagicMock,
) -> None:
    not_found = grpc.aio.AioRpcError(
        code=grpc.StatusCode.NOT_FOUND,
        initial_metadata=grpc.aio.Metadata(),
        trailing_metadata=grpc.aio.Metadata(),
        details="table missing",
    )
    stub = MagicMock()
    stub.GetTable = AsyncMock(side_effect=not_found)
    stub.CreateTable = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(sdgrpc.table_pb2_grpc, "TableServiceStub",
                        lambda _ch: stub)
    await store.ensure_collection()
    # CreateTable was called with the AKB-shape spec.
    create_arg = stub.CreateTable.await_args.args[0]
    assert isinstance(create_arg, table_pb2.CreateTableRequest)
    assert create_arg.table.table_name == "akb_chunks_test"


@pytest.mark.asyncio
async def test_ensure_collection_already_exists_is_ok(
    monkeypatch: pytest.MonkeyPatch,
    store: sdgrpc.SeahorseDbGrpcStore,
    mock_channel: MagicMock,
) -> None:
    """Raced peer beat us to CreateTable — that's the expected
    success path, not an exception."""
    not_found = grpc.aio.AioRpcError(
        code=grpc.StatusCode.NOT_FOUND,
        initial_metadata=grpc.aio.Metadata(),
        trailing_metadata=grpc.aio.Metadata(),
        details="missing",
    )
    already = grpc.aio.AioRpcError(
        code=grpc.StatusCode.ALREADY_EXISTS,
        initial_metadata=grpc.aio.Metadata(),
        trailing_metadata=grpc.aio.Metadata(),
        details="raced",
    )
    stub = MagicMock()
    stub.GetTable = AsyncMock(side_effect=not_found)
    stub.CreateTable = AsyncMock(side_effect=already)
    monkeypatch.setattr(sdgrpc.table_pb2_grpc, "TableServiceStub",
                        lambda _ch: stub)
    await store.ensure_collection()  # no raise
    assert store._ensured_collection is True


@pytest.mark.asyncio
async def test_upsert_one_jsonl_record_shape(
    monkeypatch: pytest.MonkeyPatch,
    store: sdgrpc.SeahorseDbGrpcStore,
    mock_channel: MagicMock,
) -> None:
    stub = MagicMock()
    stub.InsertJsonl = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(sdgrpc.ingest_pb2_grpc, "IngestServiceStub",
                        lambda _ch: stub)

    await store.upsert_one(
        chunk_id="aaaaaaaa-bbbb-cccc-dddd-000000000099",
        content="hello world",
        section_path="intro",
        chunk_index=3,
        dense=[0.1, 0.2, 0.3, 0.4],
        sparse_indices=[5, 7, 9],
        sparse_values=[1.0, 0.5, 0.25],
        source_type="document",
        source_id="11111111-2222-3333-4444-555555555555",
        vault_id="99999999-8888-7777-6666-555555555555",
    )

    args = stub.InsertJsonl.await_args.args[0]
    assert isinstance(args, ingest_pb2.InsertJsonlRequest)
    assert args.table_name == "akb_chunks_test"
    # JSONL is one record + newline.
    line = args.jsonl.decode("utf-8").rstrip("\n")
    record = json.loads(line)
    assert record["chunk_id"] == "aaaaaaaa-bbbb-cccc-dddd-000000000099"
    assert record["content"] == "hello world"
    assert record["section_path"] == "intro"
    assert record["chunk_index"] == 3
    # vault_id (issue #189 Phase 2) is stored on every row for the vault filter.
    assert record["vault_id"] == "99999999-8888-7777-6666-555555555555"
    assert record["embedding"] == [0.1, 0.2, 0.3, 0.4]
    # Sparse is the "term_id:weight ..." compact form.
    assert record["sparse"] == "5:1 7:0.5 9:0.25"
    # `id` is the deterministic signed-i64 label derived from the
    # chunk UUID. The exact value is in seahorse_db._chunk_id_to_label;
    # what matters here is that it fits in i64 (regression for 0.7.7).
    assert -(2**63) <= record["id"] < 2**63


@pytest.mark.asyncio
async def test_upsert_one_dense_none_raises(
    monkeypatch: pytest.MonkeyPatch,
    store: sdgrpc.SeahorseDbGrpcStore,
    mock_channel: MagicMock,
) -> None:
    from app.services.vector_store.base import VectorStoreUnavailable
    stub = MagicMock()
    stub.InsertJsonl = AsyncMock()
    monkeypatch.setattr(sdgrpc.ingest_pb2_grpc, "IngestServiceStub",
                        lambda _ch: stub)
    with pytest.raises(VectorStoreUnavailable):
        await store.upsert_one(
            chunk_id="aaaaaaaa-bbbb-cccc-dddd-000000000099",
            content="x", section_path="", chunk_index=0,
            dense=None,
            sparse_indices=[], sparse_values=[],
            source_type="document",
            source_id="11111111-2222-3333-4444-555555555555",
            vault_id="99999999-8888-7777-6666-555555555555",
        )
    stub.InsertJsonl.assert_not_called()


@pytest.mark.asyncio
async def test_delete_point_where_clause(
    monkeypatch: pytest.MonkeyPatch,
    store: sdgrpc.SeahorseDbGrpcStore,
    mock_channel: MagicMock,
) -> None:
    stub = MagicMock()
    stub.DeleteTableData = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(sdgrpc.ingest_pb2_grpc, "IngestServiceStub",
                        lambda _ch: stub)
    await store.delete_point("aaaaaaaa-bbbb-cccc-dddd-000000000077")
    req = stub.DeleteTableData.await_args.args[0]
    assert isinstance(req, ingest_pb2.DeleteTableDataRequest)
    assert req.table_name == "akb_chunks_test"
    assert req.delete_condition == "chunk_id = 'aaaaaaaa-bbbb-cccc-dddd-000000000077'"


@pytest.mark.asyncio
async def test_delete_point_rejects_non_uuid(
    monkeypatch: pytest.MonkeyPatch,
    store: sdgrpc.SeahorseDbGrpcStore,
    mock_channel: MagicMock,
) -> None:
    stub = MagicMock()
    stub.DeleteTableData = AsyncMock()
    monkeypatch.setattr(sdgrpc.ingest_pb2_grpc, "IngestServiceStub",
                        lambda _ch: stub)
    with pytest.raises(ValueError):
        # No quote escape would land in the WHERE clause — defense in
        # depth even though AKB never produces a non-UUID chunk_id.
        await store.delete_point("not-a-uuid'; DROP TABLE x; --")
    stub.DeleteTableData.assert_not_called()


@pytest.mark.asyncio
async def test_hybrid_search_request_shape(
    monkeypatch: pytest.MonkeyPatch,
    store: sdgrpc.SeahorseDbGrpcStore,
    mock_channel: MagicMock,
) -> None:
    # Stub QueryService — return an async iterator yielding one
    # header, one chunk (Arrow IPC), and one footer.
    rows = [{
        "chunk_id": "aaaaaaaa-bbbb-cccc-dddd-000000000088",
        "source_type": "document",
        "source_id": "11111111-2222-3333-4444-555555555555",
        "section_path": "",
        "content": "Coral handles HTTP REST and gRPC on the same port",
        "score": 0.92,
    }]

    async def fake_stream(_request: query_pb2.HybridSearchRequest,
                          timeout: float) -> object:
        # captured for assertions
        fake_stream.captured = _request  # type: ignore[attr-defined]
        events = [
            query_pb2.ResultStreamEvent(
                header=query_pb2.ResultStreamHeader(num_result_sets=1),
            ),
            query_pb2.ResultStreamEvent(
                chunk=query_pb2.ResultStreamChunk(
                    chunk=_encode_record_batch(rows),
                ),
            ),
            query_pb2.ResultStreamEvent(
                footer=query_pb2.ResultStreamFooter(),
            ),
        ]
        for e in events:
            yield e

    stub = MagicMock()
    stub.HybridSearch = fake_stream
    monkeypatch.setattr(sdgrpc.query_pb2_grpc, "QueryServiceStub",
                        lambda _ch: stub)

    # bm25 stats — patch sparse_encoder's loaders so the hybrid_search
    # path doesn't try to hit a real Postgres pool.
    monkeypatch.setattr(
        "app.services.sparse_encoder.load_stats",
        AsyncMock(return_value={
            "total_docs": 1000, "avgdl": 87.5, "k1": 1.5, "b": 0.75,
        }),
    )
    monkeypatch.setattr(
        "app.services.sparse_encoder.load_df_for_terms",
        AsyncMock(return_value={5: 12, 7: 4}),
    )

    hits = await store.hybrid_search(
        query_text="hybrid search",
        query_dense=[0.1, 0.2, 0.3, 0.4],
        query_sparse_indices=[5, 7],
        query_sparse_values=[0.8, 0.4],
        source_ids=None,
        limit=3,
        prefetch_per_leg=32,
    )

    # Decoded hit comes through.
    assert len(hits) == 1
    assert hits[0].chunk_id == rows[0]["chunk_id"]
    assert hits[0].score == pytest.approx(0.92)

    # Wire assertions — the captured request fields look right.
    req = fake_stream.captured  # type: ignore[attr-defined]
    assert isinstance(req, query_pb2.HybridSearchRequest)
    assert req.table_name == "akb_chunks_test"
    assert req.top_k == 3
    # Dense leg
    assert req.dense.column == "embedding"
    assert list(req.dense.vectors[0].values) == pytest.approx([0.1, 0.2, 0.3, 0.4])
    assert req.dense.parameters.ef_search == max(32, 3 * 2)
    # Sparse leg
    assert req.sparse.column == "sparse"
    assert req.sparse.vectors[0] == "5:0.8 7:0.4"
    assert req.sparse.parameters.k == pytest.approx(1.5)
    assert req.sparse.parameters.b == pytest.approx(0.75)
    # Sparse metadata sent when both stats and query tokens exist.
    assert req.sparse.metadata.n == 1000
    assert req.sparse.metadata.avgdl == pytest.approx(87.5)
    assert list(req.sparse.metadata.df) == ["5:12 7:4"]
    # Fusion = RRF with k=60 — regression for the missing-k bug in
    # the first cut of this driver.
    assert req.fusion.type == "rrf"
    assert req.fusion.parameters.k == 60
    # Projection matches REST.
    assert req.projection == "chunk_id, source_type, source_id, section_path, content"
    # No filter when source_ids is None.
    assert req.filter == ""


@pytest.mark.asyncio
async def test_hybrid_search_source_ids_filter(
    monkeypatch: pytest.MonkeyPatch,
    store: sdgrpc.SeahorseDbGrpcStore,
    mock_channel: MagicMock,
) -> None:
    async def fake_stream(_request: query_pb2.HybridSearchRequest,
                          timeout: float) -> object:
        fake_stream.captured = _request  # type: ignore[attr-defined]
        # No chunks — only header/footer. Hits list will be empty.
        yield query_pb2.ResultStreamEvent(
            header=query_pb2.ResultStreamHeader(num_result_sets=1),
        )
        yield query_pb2.ResultStreamEvent(
            footer=query_pb2.ResultStreamFooter(),
        )

    stub = MagicMock()
    stub.HybridSearch = fake_stream
    monkeypatch.setattr(sdgrpc.query_pb2_grpc, "QueryServiceStub",
                        lambda _ch: stub)
    monkeypatch.setattr(
        "app.services.sparse_encoder.load_stats",
        AsyncMock(return_value={"total_docs": 10, "avgdl": 5.0,
                                "k1": 1.5, "b": 0.75}),
    )
    monkeypatch.setattr(
        "app.services.sparse_encoder.load_df_for_terms",
        AsyncMock(return_value={5: 1}),
    )

    await store.hybrid_search(
        query_text="x",
        query_dense=[0.0, 0.0, 0.0, 0.1],
        query_sparse_indices=[5], query_sparse_values=[0.1],
        source_ids=[
            "11111111-2222-3333-4444-555555555555",
            "aaaaaaaa-bbbb-cccc-dddd-000000000010",
        ],
        limit=2, prefetch_per_leg=8,
    )
    req = fake_stream.captured  # type: ignore[attr-defined]
    expected_filter = (
        "source_id IN ('11111111-2222-3333-4444-555555555555', "
        "'aaaaaaaa-bbbb-cccc-dddd-000000000010')"
    )
    assert req.filter == expected_filter


@pytest.mark.asyncio
async def test_hybrid_search_vault_ids_filter(
    monkeypatch: pytest.MonkeyPatch,
    store: sdgrpc.SeahorseDbGrpcStore,
    mock_channel: MagicMock,
) -> None:
    """The vault path (issue #189 Phase 2): vault_ids must filter on the
    `vault_id` column, NOT source_id — a column mix-up here is a cross-vault
    leak. Symmetric to test_hybrid_search_source_ids_filter."""
    async def fake_stream(_request: query_pb2.HybridSearchRequest,
                          timeout: float) -> object:
        fake_stream.captured = _request  # type: ignore[attr-defined]
        yield query_pb2.ResultStreamEvent(
            header=query_pb2.ResultStreamHeader(num_result_sets=1),
        )
        yield query_pb2.ResultStreamEvent(footer=query_pb2.ResultStreamFooter())

    stub = MagicMock()
    stub.HybridSearch = fake_stream
    monkeypatch.setattr(sdgrpc.query_pb2_grpc, "QueryServiceStub",
                        lambda _ch: stub)
    monkeypatch.setattr(
        "app.services.sparse_encoder.load_stats",
        AsyncMock(return_value={"total_docs": 10, "avgdl": 5.0, "k1": 1.5, "b": 0.75}),
    )
    monkeypatch.setattr(
        "app.services.sparse_encoder.load_df_for_terms",
        AsyncMock(return_value={5: 1}),
    )

    await store.hybrid_search(
        query_text="x",
        query_dense=[0.0, 0.0, 0.0, 0.1],
        query_sparse_indices=[5], query_sparse_values=[0.1],
        source_ids=None,
        vault_ids=["11111111-2222-3333-4444-555555555555"],
        limit=2, prefetch_per_leg=8,
    )
    req = fake_stream.captured  # type: ignore[attr-defined]
    assert req.filter == "vault_id IN ('11111111-2222-3333-4444-555555555555')"


@pytest.mark.asyncio
async def test_hybrid_search_rejects_both_vault_and_source(
    monkeypatch: pytest.MonkeyPatch,
    store: sdgrpc.SeahorseDbGrpcStore,
    mock_channel: MagicMock,
) -> None:
    """Both filters at once is a caller bug → AssertionError (exactly-one)."""
    monkeypatch.setattr(
        "app.services.sparse_encoder.load_stats",
        AsyncMock(return_value={"total_docs": 10, "avgdl": 5.0, "k1": 1.5, "b": 0.75}),
    )
    monkeypatch.setattr(
        "app.services.sparse_encoder.load_df_for_terms",
        AsyncMock(return_value={5: 1}),
    )
    with pytest.raises(AssertionError):
        await store.hybrid_search(
            query_text="x", query_dense=[0.0, 0.0, 0.0, 0.1],
            query_sparse_indices=[5], query_sparse_values=[0.1],
            source_ids=["11111111-2222-3333-4444-555555555555"],
            vault_ids=["22222222-3333-4444-5555-666666666666"],
            limit=2, prefetch_per_leg=8,
        )


@pytest.mark.asyncio
async def test_hybrid_search_rejects_non_uuid_source_id(
    monkeypatch: pytest.MonkeyPatch,
    store: sdgrpc.SeahorseDbGrpcStore,
    mock_channel: MagicMock,
) -> None:
    stub = MagicMock()
    stub.HybridSearch = MagicMock()
    monkeypatch.setattr(sdgrpc.query_pb2_grpc, "QueryServiceStub",
                        lambda _ch: stub)
    monkeypatch.setattr(
        "app.services.sparse_encoder.load_stats",
        AsyncMock(return_value={"total_docs": 0, "avgdl": 0.0,
                                "k1": 1.5, "b": 0.75}),
    )

    with pytest.raises(ValueError):
        await store.hybrid_search(
            query_text="x",
            query_dense=[0.0, 0.0, 0.0, 0.1],
            query_sparse_indices=[], query_sparse_values=[],
            source_ids=["not-a-uuid'; DROP TABLE x; --"],
            limit=1, prefetch_per_leg=1,
        )
    stub.HybridSearch.assert_not_called()
