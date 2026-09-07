"""Search filters are applied before retrieval limits, across REST and storage arms."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import httpx
import pytest
from fastapi import FastAPI

from app.api.deps import get_current_user
from app.api.routes import search as routes
from app.exceptions import ValidationError
from app.models.document import SearchResponse
from app.services import search_service as ss
from app.services.search_filters import collection_predicate, escape_like, metadata_matches
from app.services.m1_native_grep_service import HeadBody, M1NativeGrepService


def test_collection_boundary_and_literal_metacharacters():
    params = ["existing"]
    sql = collection_predicate("c.path", "/guide_%/", params)
    assert params == ["existing", "guide_%", "guide\\_\\%/%"]
    assert sql == "(c.path = $2 OR c.path LIKE $3 ESCAPE '\\')"
    assert escape_like("100%_\\") == "100\\%\\_\\\\"


def test_metadata_filters_are_or_within_and_between():
    meta = {"type": "report", "tags": ["ops"], "status": "archived"}
    assert metadata_matches(meta, ["note", "report"], ["ops", "dev"], True)
    assert not metadata_matches(meta, ["note"], ["ops"], True)
    assert not metadata_matches(meta, ["report"], ["dev"], True)
    assert not metadata_matches(meta, ["report"], ["ops"], False)


async def test_rest_serializes_all_search_and_grep_filters(monkeypatch):
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(user_id="user")
    search = AsyncMock(return_value=SearchResponse(query="x", total=0, returned=0, total_matches=0, results=[]))
    grep = AsyncMock(return_value={"pattern": "x", "total_docs": 0, "total_matches": 0, "results": []})
    monkeypatch.setattr(routes.search_service, "search", search)
    monkeypatch.setattr(routes.search_service, "grep", grep)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        params = [("q", "x"), ("vault", "a"), ("vault", "b"), ("collection", "guide_%"),
                  ("doc_types", "report"), ("doc_types", "note"), ("tags", "ops"), ("include_archived", "false")]
        assert (await client.get("/search", params=params + [("source_type", "document")])).status_code == 200
        assert search.call_args.kwargs["doc_types"] == ["report", "note"]
        assert search.call_args.kwargs["vault"] == ["a", "b"]
        assert search.call_args.kwargs["source_type"] == "document"
        assert (await client.get("/grep", params=params + [("regex", "true"), ("case_sensitive", "true")])).status_code == 200
        assert grep.call_args.kwargs["regex"] is True
        assert grep.call_args.kwargs["case_sensitive"] is True
        assert grep.call_args.kwargs["include_archived"] is False
        assert grep.call_args.kwargs["collection"] == "guide_%"
        assert grep.call_args.kwargs["tags"] == ["ops"]
        assert grep.call_args.kwargs["doc_types"] == ["report", "note"]
        await client.get("/grep?q=x")
        assert grep.call_args.kwargs["include_archived"] is True  # existing API default


class CandidateConnection:
    def __init__(self):
        self.queries = []

    async def fetchval(self, *_args):
        return False

    async def fetch(self, query, *params):
        self.queries.append((query, params))
        if "FROM documents d" in query:
            return [{"id": uuid.UUID(int=30)}]
        if "FROM vault_tables t" in query:
            return [{"id": uuid.UUID(int=40)}]
        if "FROM vault_files f" in query:
            return [{"id": uuid.UUID(int=50)}]
        return []


class Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@pytest.mark.parametrize("source,expected", [("document", 30), ("table", 40), ("file", 50)])
async def test_source_and_collection_constrain_candidates_before_vector_limit(monkeypatch, source, expected):
    conn = CandidateConnection()
    monkeypatch.setattr(ss, "get_pool", AsyncMock(return_value=Pool(conn)))
    monkeypatch.setattr(ss, "generate_embeddings", AsyncMock(return_value=[[0.1]]))
    monkeypatch.setattr(ss, "_configured_document_source_type", lambda: ss.LEGACY_DOCUMENT_SOURCE)
    service = ss.SearchService()
    vector = AsyncMock(return_value=([], None))
    monkeypatch.setattr(service, "_run_vector_search", vector)
    await service.search("x", vault=["mine", "private"], user_id=str(uuid.UUID(int=1)), collection="guide_%", source_type=source, limit=25)
    assert vector.call_args.kwargs["candidate_source_ids"] == [str(uuid.UUID(int=expected))]
    for query, params in conn.queries:
        assert "vault_access" in query  # every candidate query retains ACL
        assert "guide\\_\\%/%" in params
        assert "LIKE" in query and "ESCAPE" in query


async def test_document_metadata_filters_do_not_admit_unrelated_tables_and_files(monkeypatch):
    conn = CandidateConnection()
    monkeypatch.setattr(ss, "get_pool", AsyncMock(return_value=Pool(conn)))
    monkeypatch.setattr(ss, "generate_embeddings", AsyncMock(return_value=[[0.1]]))
    monkeypatch.setattr(ss, "_configured_document_source_type", lambda: ss.LEGACY_DOCUMENT_SOURCE)
    service = ss.SearchService()
    vector = AsyncMock(return_value=([], None))
    monkeypatch.setattr(service, "_run_vector_search", vector)
    await service.search("x", vault="mine", doc_types=["report", "note"], tags=["ops"], limit=25)
    assert len(conn.queries) == 1
    query, params = conn.queries[0]
    assert "d.doc_type = ANY" in query and "d.tags &&" in query and "d.status != 'archived'" in query
    assert ["report", "note"] in params and ["ops"] in params
    assert vector.call_args.kwargs["candidate_source_ids"] == [str(uuid.UUID(int=30))]


async def test_native_grep_filters_before_counting_and_limiting(monkeypatch):
    bodies = [HeadBody(namespace_id=uuid.UUID(int=1), vault="mine", resource_id=uuid.UUID(int=i + 1),
                       surface="document", path=f"guides/{i}.md", revision_id="r", digest="d", byte_size=100,
                       canonical_bytes=(f"---\ntype: {'report' if i >= 25 else 'note'}\ntags: [ops]\nstatus: {'archived' if i == 29 else 'active'}\n---\nAPI-223\n").encode())
              for i in range(30)]
    service = M1NativeGrepService(None)
    monkeypatch.setattr(service, "_head_bodies", AsyncMock(return_value=bodies))
    response = await service.grep_public("API-223", user_id=uuid.UUID(int=1), doc_types=["report"], tags=["ops"], include_archived=False, limit=2)
    assert response["total_docs"] == 4
    assert response["returned_docs"] == 2
    assert response["truncated"] is True
    assert all(int(item["path"].split("/")[-1][:-3]) >= 25 for item in response["results"])
    response = await service.grep_public("API-223", user_id=uuid.UUID(int=1), doc_types=["report"], include_archived=True)
    assert response["total_docs"] == 5


async def test_invalid_regex_is_an_error_not_a_successful_zero_match():
    with pytest.raises(ValidationError, match="Invalid regex"):
        await ss.SearchService().grep("[", vault="mine", regex=True)


async def test_standard_grep_binds_filters_before_scanning(monkeypatch):
    conn = CandidateConnection()
    monkeypatch.setattr(ss, "get_pool", AsyncMock(return_value=Pool(conn)))
    monkeypatch.setattr(ss, "_configured_document_source_type", lambda: ss.LEGACY_DOCUMENT_SOURCE)
    await ss.SearchService().grep("100%_", vault=["mine", "private"], user_id=str(uuid.UUID(int=1)),
                                 collection="guide_%", doc_types=["report"], tags=["ops"], include_archived=False)
    query, params = conn.queries[0]
    assert "vault_access" in query
    assert "d.doc_type = ANY" in query and "d.tags &&" in query
    assert "d.status != 'archived'" in query
    assert "100\\%\\_" in params and "guide\\_\\%/%" in params
    assert ["report"] in params and ["ops"] in params


@pytest.mark.parametrize("pattern,regex,case_sensitive,count", [
    ("api-223", False, False, 1), ("api-223", False, True, 0),
    ("API-[0-9]+", True, True, 1), ("api-[0-9]+", True, True, 0),
])
async def test_native_exact_and_regex_options_affect_results(monkeypatch, pattern, regex, case_sensitive, count):
    body = HeadBody(namespace_id=uuid.UUID(int=1), vault="mine", resource_id=uuid.UUID(int=2),
                    surface="document", path="x.md", revision_id="r", digest="d", byte_size=8,
                    canonical_bytes=b"API-223\n")
    service = M1NativeGrepService(None)
    monkeypatch.setattr(service, "_head_bodies", AsyncMock(return_value=[body]))
    response = await service.grep_public(pattern, user_id=uuid.UUID(int=1), regex=regex, case_sensitive=case_sensitive)
    assert response["total_matches"] == count
