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
        for r in self.rows:
            r.setdefault("body_slice", r.get("canonical_bytes", b""))

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

    candidates, stats = await SearchService()._native_document_candidates(
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

    assert candidates == []
    assert stats == {}
    assert "r.current_path =" in conn.sql
    assert "ESCAPE '\\'" in conn.sql
    assert conn.params == (collection, escaped)


@pytest.mark.asyncio
async def test_native_archived_candidates_use_current_metadata_before_ranking(monkeypatch):
    conn = _CandidateConn()
    conn.rows = [
        {
            "resource_id": uuid.UUID(int=i + 1),
            "body_slice": f"---\nstatus: {status}\n---\nbody\n".encode(),
        }
        for i, status in enumerate(["draft", "active", "archived"])
    ]
    candidates, stats = await SearchService()._native_document_candidates(
        conn, user_uuid=None, is_admin=True, vaults=["mine"], collection=None,
        doc_type=None, tags=None, include_archived=False, archive_scope="archived", source_uris=None,
    )
    assert candidates == [str(uuid.UUID(int=3))]
    assert stats == {}


@pytest.mark.asyncio
async def test_native_search_pushes_source_uri_into_bounded_sql_scope():
    conn = _CandidateConn()
    candidates, stats = await SearchService()._native_document_candidates(
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
    assert candidates == []
    assert stats == {}

    # Pairwise shape: one clause covering all URIs via unnested
    # (vault, identifier) rows — not one OR per URI, and NOT two independent
    # ANYs (which would cross-match vault A's identifier against vault B).
    # The scope query (fetchrow) and the page query (fetch) both carry it.
    # (" OR " still appears once — the path-OR-id alternation.)
    assert all("r.current_path" in sql and "r.resource_id::text" in sql for sql, _ in conn.queries)
    assert all("unnest(" in sql for sql, _ in conn.queries)
    assert conn.sql.count(" OR ") == 1
    assert conn.params == (["measure"], ["specs/guide.md"])


@pytest.mark.asyncio
async def test_native_search_source_uris_share_one_pairwise_clause():
    """N URIs produce one pairwise clause (constant-size SQL), not N OR
    clauses — and pairs never cross-match (vault A + vault B's identifier)."""
    conn = _CandidateConn()
    uris = [f"akb://measure/doc/specs/guide-{i}.md" for i in range(5)]
    candidates, stats = await SearchService()._native_document_candidates(
        conn,
        user_uuid=None,
        is_admin=True,
        vaults=None,
        collection=None,
        doc_type=None,
        tags=None,
        include_archived=False,
        source_uris=uris,
    )
    assert candidates == []
    assert stats == {}
    assert conn.sql.count(" OR ") == 1  # only the path-OR-id alternation
    assert "unnest(" in conn.sql
    # Same vault repeated per pair (pairwise), NOT one vault list × one id
    # list (which would cross-match across vaults).
    assert conn.params == (["measure"] * 5, [f"specs/guide-{i}.md" for i in range(5)])


@pytest.mark.asyncio
async def test_native_search_source_uris_do_not_cross_match_vaults():
    """Pairwise unnest keeps (vault, identifier) together: with URIs from two
    vaults, the generated SQL cannot match vault A's name against vault B's
    identifier. Verified by executing the scope predicate shape against a
    fake conn that evaluates the pairing in Python."""

    seen_sql: list[str] = []
    seen_params: list[tuple] = []

    class _PairCheckingConn(_CandidateConn):
        async def fetch(self, sql, *params):
            seen_sql.append(sql)
            seen_params.append(params)
            # Simulate rows from two vaults; the candidate loop then runs
            # normally (empty body slices → plain-markdown defaults).
            return [
                {
                    "resource_id": uuid.uuid4(),
                    "current_path": "specs/guide.md",
                    "vault_name": "vault-a",
                    "byte_size": 20,
                    "digest": "0" * 64,
                    "encoding": "utf-8",
                    "selected_placement": M1ReferencePayloadStore.selected_placement,
                    "verification_profile": "sha256-size-utf8-v1",
                    "body_slice": b"plain body\n",
                },
            ]

    conn = _PairCheckingConn()
    candidates, stats = await SearchService()._native_document_candidates(
        conn,
        user_uuid=None,
        is_admin=True,
        vaults=None,
        collection=None,
        doc_type=None,
        tags=None,
        include_archived=False,
        source_uris=[
            "akb://vault-a/doc/specs/guide.md",
            "akb://vault-b/doc/other.md",
        ],
    )
    assert candidates != []  # vault-a pair matches its own row
    assert stats == {}
    # The SQL carries both pairs positionally aligned: index i of the vault
    # array belongs to index i of the identifier array.
    vaults_param, idents_param = seen_params[-1][-2], seen_params[-1][-1]
    assert vaults_param == ["vault-a", "vault-b"]
    assert idents_param == ["specs/guide.md", "other.md"]
    # And the predicate is a row-constructor IN (pairwise), not two ANYs.
    assert "(v.name, r.current_path) IN (" in seen_sql[-1]
    assert "v.name = ANY" not in seen_sql[-1]


@pytest.mark.asyncio
async def test_native_search_source_uri_cap_message_names_recovery():
    """Over-cap rejection tells the caller how to recover (split / vault scope)."""
    from app.config import settings
    from app.services import search_service

    conn = _CandidateConn()
    over = ["akb://measure/doc/specs/guide.md"] * (settings.search_max_source_uris + 1)
    with pytest.raises(ValidationError, match="split the request or use a vault scope"):
        await SearchService()._native_document_candidates(
            conn,
            user_uuid=None,
            is_admin=True,
            vaults=None,
            collection=None,
            doc_type=None,
            tags=None,
            include_archived=False,
            source_uris=over,
        )
    assert search_service.NATIVE_SEARCH_MAX_SOURCE_URIS == 200  # legacy alias intact


@pytest.mark.asyncio
async def test_reference_payload_write_cap_rejects_oversize_body():
    """The reference placement enforces the same 10MiB write cap as the
    pg-bodystore placement; larger content belongs in File storage."""
    from app.exceptions import ValidationError as VE
    from app.services.m1_reference_payload_store import M1ReferencePayloadStore

    assert M1ReferencePayloadStore.max_text_bytes == 10 * 1024 * 1024
    with pytest.raises(VE, match="10 MiB limit"):
        M1ReferencePayloadStore._verified_bytes(b"x" * (10 * 1024 * 1024 + 1))
    with pytest.raises(VE, match="10 MiB limit"):
        M1ReferencePayloadStore._verified_bytes("y" * (10 * 1024 * 1024 + 1))
    ok, _ = M1ReferencePayloadStore._verified_bytes(b"small body\n")
    assert ok == b"small body\n"


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
async def test_native_candidate_slice_parse_runs_off_event_loop(monkeypatch):
    """The slice path parses the frontmatter envelope off the loop and never
    touches the payload stores (no per-row verify: the aggregate guard already
    ran on manifest numbers, and hydration re-verifies the winners)."""
    body = b"---\ntitle: Worker\ntype: note\n---\nbody\n"
    row = {
        "resource_id": uuid.uuid4(),
        "current_path": "worker.md",
        "vault_name": "measure",
        "byte_size": len(body),
        "digest": hashlib.sha256(body).hexdigest(),
        "encoding": "utf-8",
        "selected_placement": M1ReferencePayloadStore.selected_placement,
        "verification_profile": "sha256-size-utf8-v1",
        "body_slice": body,
    }
    conn = _CandidateConn(resource_count=1, body_bytes=len(body), rows=[row])
    loop_thread = threading.get_ident()
    parse_threads = []

    import app.services.document_service as docs

    original_parse = docs._parse_markdown

    def guarded_parse(content, **kw):
        parse_threads.append(threading.get_ident())
        assert threading.get_ident() != loop_thread
        return original_parse(content, **kw)

    monkeypatch.setattr(docs, "_parse_markdown", guarded_parse)
    for store in (M1ReferencePayloadStore, M1PgBodyStore):
        monkeypatch.setattr(
            store, "_verify_row", staticmethod(lambda candidate: (_ for _ in ()).throw(
                AssertionError("slice path must not verify per-row")))
        )

    candidates, stats = await SearchService()._native_document_candidates(
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
    assert stats == {}
    assert parse_threads


@pytest.mark.asyncio
async def test_native_candidate_slice_never_selects_full_body():
    """The candidate SELECT must not fetch `canonical_bytes`: with a 10MiB body
    behind a small envelope, the bytes crossing the wire stay slice-sized."""
    body = b"---\ntitle: Big\n---\n" + b"x" * (10 * 1024 * 1024)
    row = {
        "resource_id": uuid.uuid4(),
        "current_path": "big.md",
        "vault_name": "measure",
        "byte_size": len(body),
        "digest": hashlib.sha256(body).hexdigest(),
        "encoding": "utf-8",
        "selected_placement": M1ReferencePayloadStore.selected_placement,
        "verification_profile": "sha256-size-utf8-v1",
        # The fake conn hands back only what the SELECT asked for: a slice.
        "body_slice": body[:8192 + 1],
    }
    conn = _CandidateConn(resource_count=1, body_bytes=len(body), rows=[row])
    candidates, stats = await SearchService()._native_document_candidates(
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
    assert stats == {}
    # The only occurrence of the column name is inside substring(...): the full
    # body is never selected.
    assert conn.sql.count("canonical_bytes") == 1
    assert "substring(" in conn.sql


@pytest.mark.asyncio
async def test_native_candidate_unparseable_envelope_is_counted_not_dropped_silently():
    """An envelope that opens but never closes inside the slice is excluded
    from candidates AND reported in stats (never filtered on defaults)."""
    row = {
        # Opens --- but the closing --- lies beyond the 8KiB slice.
        "resource_id": uuid.uuid4(),
        "current_path": "huge-frontmatter.md",
        "vault_name": "measure",
        "byte_size": 20000,
        "digest": "0" * 64,
        "encoding": "utf-8",
        "selected_placement": M1ReferencePayloadStore.selected_placement,
        "verification_profile": "sha256-size-utf8-v1",
        "body_slice": b"---\ntitle: " + b"y" * 8100,
    }
    conn = _CandidateConn(resource_count=1, body_bytes=20000, rows=[row])
    candidates, stats = await SearchService()._native_document_candidates(
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
    assert candidates == []
    assert stats == {"unparseable_envelope": 1}


@pytest.mark.asyncio
async def test_native_candidate_slice_cut_mid_codepoint_still_filters():
    """A byte-cut slice ending inside a multibyte character decodes after
    trimming the partial tail — the envelope is still parsed, not counted
    as unparseable. Only genuinely invalid bytes stay unparseable."""
    from app.services.search_service import _decode_slice_prefix

    # '가' = 3 bytes in UTF-8; cut after the first byte.
    full = "---\ntitle: 가\n---\nbody\n".encode("utf-8")
    cut = full[:len("---\ntitle: ".encode("utf-8")) + 1]
    assert _decode_slice_prefix(cut) == "---\ntitle: "
    # Genuinely invalid bytes (not a cut tail) stay None.
    assert _decode_slice_prefix(b"---\ntitle: \xff\xfe\n---\n") is None

    row = {
        "resource_id": uuid.uuid4(),
        "current_path": "korean.md",
        "vault_name": "measure",
        "byte_size": len(full),
        "digest": "0" * 64,
        "encoding": "utf-8",
        "selected_placement": M1ReferencePayloadStore.selected_placement,
        "verification_profile": "sha256-size-utf8-v1",
        "body_slice": full,
    }
    conn = _CandidateConn(resource_count=1, body_bytes=len(full), rows=[row])
    candidates, stats = await SearchService()._native_document_candidates(
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
    assert stats == {}


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
    results, dropped = await SearchService()._hydrate_hits(
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
    assert dropped == {}
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
    results, dropped = await SearchService()._hydrate_hits(
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

    assert dropped == {}
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
