from __future__ import annotations

import hashlib
import json
import threading
import uuid

import pytest

from app.exceptions import ValidationError
from app.models.document import GrepResponse
from app.services.m1_native_grep_service import HeadBody, M1NativeGrepService
from app.services.m1_pg_body_store import M1PgBodyStore
from app.services.m1_reference_payload_store import M1ReferencePayloadStore
from app.services.native_payload_verification import NativePayloadPlacementError
from app.services.search_service import SearchService, active_document_source_type
from app.services.vector_store import VectorHit


def test_document_source_selection_is_exactly_guarded():
    assert active_document_source_type(
        backend="bare_git_current",
        measurement_only=False,
        database="akb",
    ) == "document"
    assert active_document_source_type(
        backend="native_ledger_m1",
        measurement_only=True,
        database="akb_revision_m1_measurement",
    ) == "native_document"

    with pytest.raises(RuntimeError, match="dedicated measurement database"):
        active_document_source_type(
            backend="native_ledger_m1",
            measurement_only=True,
            database="akb",
        )


def test_native_public_grep_shape_preserves_document_contract():
    body = HeadBody(
        namespace_id=uuid.uuid4(),
        vault="measure",
        resource_id=uuid.uuid4(),
        surface="document",
        path="guide.md",
        revision_id="a" * 40,
        digest="b" * 64,
        byte_size=74,
        canonical_bytes=b"---\ntitle: Guide\ntags:\n  - secret-frontmatter-token\n---\nneedle body\n",
    )

    assert body.title == "Guide"
    assert body.search_text == "needle body"
    result = M1NativeGrepService._public_response(
        pattern="needle",
        regex=False,
        native={
            "total_resources": 1,
            "total_matches": 1,
            "returned_resources": 1,
            "returned_matches": 1,
            "truncated": False,
            "results": [{
                "uri": body.uri,
                "vault": body.vault,
                "path": body.path,
                "title": body.title,
                "matches": [{"line": 1, "text": "needle body"}],
            }],
        },
    )
    assert result == {
        "pattern": "needle",
        "regex": False,
        "returned_docs": 1,
        "returned_matches": 1,
        "total_docs": 1,
        "total_matches": 1,
        "truncated": False,
        "results": [{
            "uri": "akb://measure/doc/guide.md",
            "vault": "measure",
            "path": "guide.md",
            "title": "Guide",
            "matches": [{"section": None, "text": "needle body"}],
        }],
    }


def test_rest_grep_model_preserves_additive_text_file_head_identity():
    response = GrepResponse.model_validate(
        {
            "kind": "grep",
            "pattern": "needle",
            "regex": False,
            "total_docs": 1,
            "total_matches": 1,
            "results": [{
                "uri": "akb://measure/file/00000000-0000-0000-0000-000000000001",
                "vault": "measure",
                "path": "src/main.py",
                "title": "main.py",
                "resource_type": "file",
                "revision": "a" * 40,
                "content_hash": "b" * 64,
                "matches": [{"section": None, "text": "needle"}],
            }],
        }
    )

    item = response.model_dump(exclude_none=True)["results"][0]
    assert item["resource_type"] == "file"
    assert item["revision"] == "a" * 40
    assert item["content_hash"] == "b" * 64


def test_rest_grep_model_preserves_bounded_native_truncation_details():
    response = GrepResponse.model_validate(
        {
            "kind": "grep",
            "pattern": "needle",
            "regex": False,
            "truncated": True,
            "truncation": {
                "reasons": ["per_resource_match_limit", "total_match_limit"],
                "limits": {
                    "resources": 20,
                    "matches_per_resource": 1_000,
                    "total_matches": 5_000,
                    "snippet_bytes": 4_096,
                    "snippet_bytes_per_resource": 262_144,
                    "total_snippet_bytes": 1_048_576,
                },
            },
            "results": [],
        }
    )

    assert response.model_dump(exclude_none=True)["truncation"] == {
        "reasons": ["per_resource_match_limit", "total_match_limit"],
        "limits": {
            "resources": 20,
            "matches_per_resource": 1_000,
            "total_matches": 5_000,
            "snippet_bytes": 4_096,
            "snippet_bytes_per_resource": 262_144,
            "total_snippet_bytes": 1_048_576,
        },
    }


def test_additive_grep_only_adds_head_identity_to_file_rows():
    result = M1NativeGrepService._public_response(
        pattern="needle",
        regex=False,
        native={
            "total_resources": 2,
            "total_matches": 2,
            "returned_resources": 2,
            "returned_matches": 2,
            "truncated": False,
            "results": [
                {
                    "uri": "akb://measure/doc/a.md",
                    "vault": "measure",
                    "path": "a.md",
                    "title": "A",
                    "resource_type": "document",
                    "revision": "a" * 40,
                    "content_hash": "b" * 64,
                    "matches": [{"text": "needle"}],
                },
                {
                    "uri": "akb://measure/coll/src/file/00000000-0000-0000-0000-000000000001",
                    "vault": "measure",
                    "path": "src/main.py",
                    "title": "main.py",
                    "resource_type": "file",
                    "revision": "c" * 40,
                    "content_hash": "d" * 64,
                    "matches": [{"text": "needle"}],
                },
            ],
        },
    )

    assert set(result["results"][0]) == {"uri", "vault", "path", "title", "matches"}
    assert result["results"][1]["resource_type"] == "file"
    assert result["results"][1]["revision"] == "c" * 40
    assert result["results"][1]["content_hash"] == "d" * 64


def test_file_uri_uses_collection_from_current_native_path():
    body = HeadBody(
        namespace_id=uuid.uuid4(),
        vault="measure",
        resource_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        surface="file",
        path="src/lib/main.py",
        revision_id="a" * 40,
        digest="b" * 64,
        byte_size=7,
        canonical_bytes=b"needle\n",
    )

    assert body.uri == "akb://measure/coll/src/lib/file/00000000-0000-0000-0000-000000000001"


class _CandidateConn:
    def __init__(self, *, resource_count=0, body_bytes=0, rows=None):
        self.sql = ""
        self.params = ()
        self.queries = []
        self.resource_count = resource_count
        self.body_bytes = body_bytes
        self.rows = rows or []

    class _Transaction:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_args):
            return None

    def transaction(self, **_kwargs):
        return self._Transaction()

    async def fetchrow(self, sql, *params):
        self.queries.append((sql, params))
        return {
            "resource_count": self.resource_count,
            "body_bytes": self.body_bytes,
        }

    async def fetch(self, sql, *params):
        self.sql = sql
        self.params = params
        self.queries.append((sql, params))
        return self.rows


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("collection", "escaped"),
    [("src", "src/%"), ("team_%", "team\\_\\%/%")],
)
async def test_native_search_collection_is_exact_descendant_boundary_and_escaped(collection, escaped):
    conn = _CandidateConn()

    await SearchService()._native_document_candidates(
        conn,
        user_uuid=None,
        is_admin=True,
        vaults=None,
        collection=collection,
        doc_type=None,
        tags=None,
        include_archived=False,
        source_uris=None,
    )

    assert "r.current_path =" in conn.sql
    assert "ESCAPE '\\'" in conn.sql
    assert conn.params == (collection, escaped)


@pytest.mark.asyncio
async def test_native_search_pushes_source_uri_into_bounded_sql_scope():
    conn = _CandidateConn()
    await SearchService()._native_document_candidates(
        conn,
        user_uuid=None,
        is_admin=True,
        vaults=None,
        collection=None,
        doc_type=None,
        tags=None,
        include_archived=False,
        source_uris=["akb://measure/doc/specs/guide.md"],
    )

    assert all("r.current_path" in sql and "r.resource_id::text" in sql for sql, _ in conn.queries)
    assert conn.params == ("measure", "specs/guide.md")


@pytest.mark.asyncio
async def test_native_search_rejects_corpus_before_fetching_bodies():
    from app.services import search_service

    conn = _CandidateConn(
        resource_count=search_service.NATIVE_SEARCH_MAX_CANDIDATE_RESOURCES + 1,
    )
    with pytest.raises(ValidationError, match="bounded candidate corpus"):
        await SearchService()._native_document_candidates(
            conn,
            user_uuid=None,
            is_admin=True,
            vaults=None,
            collection=None,
            doc_type=None,
            tags=None,
            include_archived=False,
            source_uris=None,
        )

    assert len(conn.queries) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "store",
    (M1ReferencePayloadStore, M1PgBodyStore),
)
async def test_native_candidate_verification_and_decode_run_off_event_loop(monkeypatch, store):
    body = b"---\ntitle: Worker\n---\nbody\n"
    row = {
        "resource_id": uuid.uuid4(),
        "current_path": "worker.md",
        "vault_name": "measure",
        "payload_id": uuid.uuid4(),
        "namespace_id": uuid.uuid4(),
        "content_profile": "text",
        "digest": hashlib.sha256(body).hexdigest(),
        "byte_size": len(body),
        "encoding": "utf-8",
        "selected_placement": store.selected_placement,
        "verification_profile": "sha256-size-utf8-v1",
        "canonical_bytes": body,
    }
    conn = _CandidateConn(resource_count=1, body_bytes=len(body), rows=[row])
    loop_thread = threading.get_ident()
    verify_threads = []
    original_verify = store._verify_row

    def guarded_verify(candidate):
        verify_threads.append(threading.get_ident())
        assert threading.get_ident() != loop_thread
        return original_verify(candidate)

    monkeypatch.setattr(store, "_verify_row", staticmethod(guarded_verify))

    candidates = await SearchService()._native_document_candidates(
        conn,
        user_uuid=None,
        is_admin=True,
        vaults=None,
        collection=None,
        doc_type=None,
        tags=None,
        include_archived=False,
        source_uris=None,
    )

    assert candidates == [str(row["resource_id"])]
    assert verify_threads


class _HydrationConn:
    def __init__(self, row):
        self.row = row

    async def fetchval(self, _sql, *_params):
        return self.row["byte_size"]

    async def fetch(self, sql, *_params):
        if "native_derived_heads" in sql:
            return [self.row]
        return []


class _HydrationAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return None


class _HydrationPool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _HydrationAcquire(self.conn)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "store",
    (M1ReferencePayloadStore, M1PgBodyStore),
)
async def test_native_hydration_verification_and_decode_run_off_event_loop(monkeypatch, store):
    from app.services import search_service

    body = b"---\ntitle: Hydrated\n---\nbody\n"
    chunk_id = uuid.uuid4()
    resource_id = uuid.uuid4()
    row = {
        "chunk_id": chunk_id,
        "resource_id": resource_id,
        "current_path": "hydrated.md",
        "head_revision_id": "a" * 40,
        "vault_name": "measure",
        "payload_id": uuid.uuid4(),
        "namespace_id": uuid.uuid4(),
        "content_profile": "text",
        "digest": hashlib.sha256(body).hexdigest(),
        "byte_size": len(body),
        "encoding": "utf-8",
        "selected_placement": store.selected_placement,
        "verification_profile": "sha256-size-utf8-v1",
        "canonical_bytes": body,
    }
    pool = _HydrationPool(_HydrationConn(row))

    async def get_test_pool():
        return pool

    monkeypatch.setattr(search_service, "get_pool", get_test_pool)
    monkeypatch.setattr(
        search_service,
        "_configured_document_source_type",
        lambda: search_service.NATIVE_DOCUMENT_SOURCE,
    )
    loop_thread = threading.get_ident()
    verify_threads = []
    original_verify = store._verify_row

    def guarded_verify(candidate):
        verify_threads.append(threading.get_ident())
        assert threading.get_ident() != loop_thread
        return original_verify(candidate)

    monkeypatch.setattr(store, "_verify_row", staticmethod(guarded_verify))
    results = await SearchService()._hydrate_hits(
        [
            VectorHit(
                chunk_id=str(chunk_id),
                source_type=search_service.NATIVE_DOCUMENT_SOURCE,
                source_id=str(resource_id),
                section_path="",
                content="body",
                score=1.0,
            )
        ]
    )

    assert results[0].title == "Hydrated"
    assert verify_threads


@pytest.mark.asyncio
async def test_native_file_hydration_preserves_public_file_identity(monkeypatch):
    from app.services import search_service

    body = b"legacy to native\n"
    chunk_id = uuid.uuid4()
    resource_id = uuid.uuid4()
    row = {
        "chunk_id": chunk_id,
        "resource_id": resource_id,
        "current_path": "files/cutover.txt",
        "head_revision_id": "a" * 40,
        "vault_name": "measure",
        "name": "cutover.txt",
        "description": "fixture",
        "mime_type": "text/plain",
        "collection": "files",
        "payload_id": uuid.uuid4(),
        "namespace_id": uuid.uuid4(),
        "content_profile": "text",
        "digest": hashlib.sha256(body).hexdigest(),
        "byte_size": len(body),
        "encoding": "utf-8",
        "selected_placement": M1PgBodyStore.selected_placement,
        "verification_profile": "sha256-size-utf8-v1",
        "canonical_bytes": body,
    }
    pool = _HydrationPool(_HydrationConn(row))

    async def get_test_pool():
        return pool

    monkeypatch.setattr(search_service, "get_pool", get_test_pool)
    monkeypatch.setattr(
        search_service,
        "_configured_document_source_type",
        lambda: search_service.NATIVE_DOCUMENT_SOURCE,
    )
    loop_thread = threading.get_ident()
    verify_threads = []
    original_verify = M1PgBodyStore._verify_row

    def guarded_verify(candidate):
        verify_threads.append(threading.get_ident())
        assert threading.get_ident() != loop_thread
        return original_verify(candidate)

    monkeypatch.setattr(M1PgBodyStore, "_verify_row", staticmethod(guarded_verify))
    results = await SearchService()._hydrate_hits(
        [
            VectorHit(
                chunk_id=str(chunk_id),
                source_type="native_file",
                source_id=str(resource_id),
                section_path="",
                content="legacy to native",
                score=1.0,
            )
        ]
    )

    assert len(results) == 1
    assert results[0].source_type == "file"
    assert results[0].uri == f"akb://measure/coll/files/file/{resource_id}"
    assert results[0].path == "files/cutover.txt"
    assert results[0].title == "cutover.txt"
    assert verify_threads


class _StubAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return None


class _StubPool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _StubAcquire(self.conn)


class _LegacyChunkConn:
    """Serves the one chunk query the legacy Document-only grep branch runs."""

    def __init__(self, rows):
        self.rows = rows
        self.sql = ""

    async def fetch(self, sql, *_params):
        self.sql = sql
        return self.rows


class _NativeHeadConn:
    """Serves the native arm's aggregate guard + Head body fetch."""

    def __init__(self, row):
        self.row = row

    def transaction(self, **_kwargs):
        return _StubAcquire(self)

    async def fetchrow(self, _sql, *_params):
        return {"resource_count": 1, "total_bytes": self.row["byte_size"]}

    async def fetch(self, _sql, *_params):
        return [self.row]


def _legacy_arm(monkeypatch) -> None:
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "document_revision_backend", "bare_git_current")
    monkeypatch.setattr(app_settings, "native_revision_m1_measurement_only", False)
    monkeypatch.setattr(app_settings, "db_name", "akb")


def _native_arm(monkeypatch) -> None:
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "document_revision_backend", "native_ledger_m1")
    monkeypatch.setattr(app_settings, "native_revision_m1_measurement_only", True)
    monkeypatch.setattr(app_settings, "db_name", "akb_revision_m1_measurement")


def _install_pool(monkeypatch, conn) -> None:
    from app.services import search_service

    async def get_test_pool():
        return _StubPool(conn)

    monkeypatch.setattr(search_service, "get_pool", get_test_pool)


@pytest.mark.asyncio
async def test_legacy_document_grep_response_has_no_placement_key(monkeypatch):
    """Byte-invariance proof: the native-arm-OFF grep response is unchanged.

    Placement observability is additive on the native arm only. The legacy
    Document-only branch builds its own result dicts and must keep the exact
    frozen key set, both as the service dict and after REST serialization
    (`response_model_exclude_none=True`, which is how the earlier additive
    fields stay invisible here too).
    """
    _legacy_arm(monkeypatch)
    conn = _LegacyChunkConn([
        {
            "doc_id": str(uuid.uuid4()),
            "vault": "legacy",
            "path": "notes/guide.md",
            "title": "Guide",
            "metadata": {},
            "section_path": "Intro",
            "content": "needle body\n",
            "chunk_index": 0,
        },
    ])
    _install_pool(monkeypatch, conn)

    response = await SearchService().grep(
        pattern="needle", vault="legacy", user_id=str(uuid.uuid4()),
    )

    assert response == {
        "pattern": "needle",
        "regex": False,
        "returned_docs": 1,
        "returned_matches": 1,
        "total_docs": 1,
        "total_matches": 1,
        "truncated": False,
        "results": [{
            "uri": "akb://legacy/coll/notes/doc/guide.md",
            "vault": "legacy",
            "path": "notes/guide.md",
            "title": "Guide",
            "matches": [{"section": "Intro", "text": "needle body"}],
        }],
    }
    serialized = GrepResponse.model_validate(
        {"kind": "grep", **response},
    ).model_dump(exclude_none=True)
    assert set(serialized["results"][0]) == {"uri", "vault", "path", "title", "matches"}
    assert "payload_placement" not in json.dumps(serialized)


@pytest.mark.asyncio
async def test_native_arm_grep_response_reports_the_head_placement(monkeypatch):
    """The same call on the native arm makes the Document's placement visible."""
    _native_arm(monkeypatch)
    assert active_document_source_type(
        backend="native_ledger_m1",
        measurement_only=True,
        database="akb_revision_m1_measurement",
    ) == "native_document"
    body = b"needle body\n"
    conn = _NativeHeadConn({
        "namespace_id": uuid.uuid4(),
        "vault": "measure",
        "resource_id": uuid.uuid4(),
        "surface": "document",
        "current_path": "notes/guide.md",
        "head_revision_id": "a" * 40,
        "digest": hashlib.sha256(body).hexdigest(),
        "byte_size": len(body),
        "encoding": "utf-8",
        "selected_placement": M1PgBodyStore.selected_placement,
        "verification_profile": "sha256-size-utf8-v1",
        "canonical_bytes": body,
    })
    _install_pool(monkeypatch, conn)

    response = await SearchService().grep(
        pattern="needle", vault="measure", user_id=str(uuid.uuid4()),
    )

    result = response["results"][0]
    assert result["payload_placement"] == M1PgBodyStore.selected_placement
    assert {"payload_id", "private_locator", "payload_manifest_id"}.isdisjoint(result)
    serialized = GrepResponse.model_validate(
        {"kind": "grep", **response},
    ).model_dump(exclude_none=True)
    assert serialized["results"][0]["payload_placement"] == (
        M1PgBodyStore.selected_placement
    )


@pytest.mark.asyncio
async def test_native_arm_grep_reports_a_historical_reference_placement(monkeypatch):
    """A row that never moved still reports its own placement, not the default."""
    _native_arm(monkeypatch)
    body = b"needle body\n"
    conn = _NativeHeadConn({
        "namespace_id": uuid.uuid4(),
        "vault": "measure",
        "resource_id": uuid.uuid4(),
        "surface": "document",
        "current_path": "notes/historical.md",
        "head_revision_id": "b" * 40,
        "digest": hashlib.sha256(body).hexdigest(),
        "byte_size": len(body),
        "encoding": "utf-8",
        "selected_placement": M1ReferencePayloadStore.selected_placement,
        "verification_profile": "sha256-size-utf8-v1",
        "canonical_bytes": body,
    })
    _install_pool(monkeypatch, conn)

    response = await SearchService().grep(
        pattern="needle", vault="measure", user_id=str(uuid.uuid4()),
    )

    assert response["results"][0]["payload_placement"] == (
        M1ReferencePayloadStore.selected_placement
    )


def test_native_search_metadata_verification_rejects_unknown_placement():
    from app.services.search_service import _verified_native_metadata

    with pytest.raises(NativePayloadPlacementError, match="Unsupported native payload placement"):
        _verified_native_metadata({"selected_placement": "unknown-placement-v1"})
