"""The MCP discovery schema and raw tool calls agree on the optional search filters."""
from unittest.mock import AsyncMock

from jsonschema import Draft202012Validator
import pytest

from app.models.document import SearchResponse
from mcp_server import server
from mcp_server.tools import available_tools


def _validator():
    schema = next(t.input_schema for t in available_tools() if t.name == "akb_search")
    return Draft202012Validator(schema)


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
