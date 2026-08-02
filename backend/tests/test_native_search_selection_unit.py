from __future__ import annotations

import uuid

import pytest

from app.exceptions import ValidationError
from app.models.document import GrepResponse
from app.services.m1_native_grep_service import HeadBody, M1NativeGrepService
from app.services.search_service import SearchService, active_document_source_type


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
    def __init__(self, *, resource_count=0, body_bytes=0):
        self.sql = ""
        self.params = ()
        self.queries = []
        self.resource_count = resource_count
        self.body_bytes = body_bytes

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
        return []


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
