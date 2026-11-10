"""The MCP discovery schema and raw tool calls agree on the optional search filters."""
from datetime import UTC, datetime
from unittest.mock import AsyncMock

from jsonschema import Draft202012Validator
import pytest

from app.models.document import SearchResponse
from app.services.search_filters import DocumentTimeRange
from mcp_server import server
from mcp_server.tools import available_tools


def _validator():
    schema = next(t.input_schema for t in available_tools() if t.name == "akb_search")
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


@pytest.mark.asyncio
async def test_omitted_filters_preserve_the_existing_service_call(monkeypatch):
    result = SearchResponse(query="x", total=0, returned=0, total_matches=0, results=[])
    search = AsyncMock(return_value=result)
    monkeypatch.setattr(server.search_service, "search", search)
    args = {"query": "x"}
    assert _validator().is_valid(args)
    assert await server._handle_search(args, "user", None) == result.model_dump()
    search.assert_awaited_once_with(
        query="x", vault=None, collection=None, doc_type=None, tags=None, limit=10,
        user_id="user", include_archived=False, source_uris=None,
    )


@pytest.mark.parametrize(("options", "field"), [
    ({"source_type": "note"}, "/source_type"),
    ({"archive_scope": "both"}, "/archive_scope"),
    ({"doc_types": "report"}, "/doc_types"),
    ({"doc_types": ["note", 1]}, "/doc_types/1"),
    ({"doc_types": None}, "/doc_types"),
])
@pytest.mark.asyncio
async def test_invalid_filter_cannot_fall_back_to_a_broader_search(monkeypatch, options, field):
    search = AsyncMock()
    monkeypatch.setattr(server.search_service, "search", search)
    args = {"query": "x", **options}
    assert not _validator().is_valid(args)
    result = await server._handle_search(args, "user", None)
    assert result["code"] == "invalid_argument" and result["details"]["field"] == field
    search.assert_not_awaited()


_START = "2026-01-01T09:00:00+09:00"
_END = "2026-01-01T00:00:00.000001Z"


@pytest.mark.parametrize("field", ["created_at", "updated_at"])
@pytest.mark.parametrize("bounds", [{"gte": _START}, {"lt": _END}, {"gte": _START, "lt": _END}])
@pytest.mark.asyncio
async def test_time_range_discovery_dispatch_and_utc_echo(monkeypatch, field, bounds):
    result = SearchResponse(query="x", total=0, returned=0, total_matches=0, results=[])
    search = AsyncMock(return_value=result)
    monkeypatch.setattr(server.search_service, "search", search)
    filters = {"doc_types": ["report"], "source_type": "document", "archive_scope": "all"}
    args = {"query": "x", **filters, "time_range": {"field": field, **bounds}}
    assert _validator().is_valid(args)
    page = await server._dispatch("akb_search", args, server._MCPUser())
    utc_bounds = {k: datetime.fromisoformat(v).astimezone(UTC) for k, v in bounds.items()}
    window = DocumentTimeRange(field=field, **utc_bounds)
    assert search.await_args.kwargs["time_range"] == window
    assert {k: search.await_args.kwargs[k] for k in filters} == filters
    assert page == {**result.model_dump(), "time_range": window.model_dump(mode="json", exclude_none=True)}


@pytest.mark.parametrize(("window", "path"), [
    (None, "/time_range"),
    ({"gte": _START}, "/time_range/field"),
    ({"field": "created_at"}, "/time_range"),
    ({"field": "indexed_at", "gte": _START}, "/time_range/field"),
    ({"field": "updated_at", "from": _START, "lt": _END}, "/time_range/from"),
    *[({"field": "created_at", "gte": invalid}, "/time_range/gte") for invalid in (
        None, 0, "2026-01-01", "2026-01-01T00:00:00", "2026-01-01 00:00:00Z",
        "2026-01-01T00:00:00.0000001Z", "2026-01-01T00:00:00+01:60", "2026-01-01T24:00:00Z",
    )],
])
@pytest.mark.asyncio
async def test_invalid_time_range_schema_and_handler(monkeypatch, window, path):
    search = AsyncMock()
    monkeypatch.setattr(server.search_service, "search", search)
    args = {"query": "x", "source_type": "document", "time_range": window}
    assert not _validator().is_valid(args)
    error = await server._handle_search(args, "user", None)
    assert error["code"] == "invalid_argument" and error["details"]["field"] == path
    search.assert_not_awaited()


@pytest.mark.parametrize(("bounds", "path"), [
    ({"gte": _START, "lt": "2026-01-01T00:00:00Z"}, "/time_range"),
    ({"gte": _END, "lt": _START}, "/time_range"),
    ({"lt": "2026-02-30T00:00:00Z"}, "/time_range/lt"),
    ({"gte": "0001-01-01T00:00:00+01:00"}, "/time_range/gte"),
])
@pytest.mark.asyncio
async def test_unusable_instants_fail_before_retrieval(monkeypatch, bounds, path):
    search = AsyncMock()
    monkeypatch.setattr(server.search_service, "search", search)
    args = {"query": "x", "source_type": "document", "time_range": {"field": "created_at", **bounds}}
    error = await server._handle_search(args, "user", None)
    assert error["code"] == "invalid_argument" and error["details"]["field"] == path
    search.assert_not_awaited()


@pytest.mark.parametrize("source", [{}, {"source_type": "table"}, {"source_type": "file"}])
@pytest.mark.asyncio
async def test_date_requires_explicit_document_kind(monkeypatch, source):
    search = AsyncMock()
    monkeypatch.setattr(server.search_service, "search", search)
    args = {"query": "x", **source, "time_range": {"field": "updated_at", "lt": _END}}
    assert not _validator().is_valid(args)
    error = await server._handle_search(args, "user", None)
    assert error["code"] == "invalid_argument" and error["details"]["field"] == "/source_type"
    search.assert_not_awaited()


@pytest.mark.asyncio
async def test_server_without_advertised_time_range_rejects_dated_requests(monkeypatch):
    legacy_args = server._TOOL_ARG_NAMES["akb_search"] - {"time_range"}
    monkeypatch.setitem(server._TOOL_ARG_NAMES, "akb_search", legacy_args)
    search = AsyncMock()
    monkeypatch.setattr(server.search_service, "search", search)
    args = {"query": "x", "source_type": "document", "time_range": {"field": "created_at", "gte": _START}}
    error = await server._dispatch("akb_search", args, server._MCPUser())
    assert error["code"] == "unknown_argument"
    search.assert_not_awaited()
