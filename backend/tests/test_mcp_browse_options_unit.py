"""MCP browse keeps legacy list paging separate from bounded resource traversal."""
from unittest.mock import AsyncMock

from jsonschema import Draft202012Validator
import pytest

from app.models.document import BrowseItem, BrowseResponse
from mcp_server import server
from mcp_server.tools import available_tools


@pytest.fixture
def browse(monkeypatch):
    item = BrowseItem(type="document", uri="akb://v/doc/item.md", name="Item", path="item.md")
    service = AsyncMock(return_value=BrowseResponse(vault="v", path="", items=[item]))
    monkeypatch.setattr(server.doc_service, "browse", service)
    monkeypatch.setattr(server, "check_vault_access", AsyncMock())
    return service


def test_browse_advertises_name_archive_and_resource_options():
    schema = next(t.input_schema for t in available_tools() if t.name == "akb_browse")
    props = schema["properties"]
    assert props["name_query"]["maxLength"] == 200
    assert set(props["archive_scope"]["enum"]) == {"unarchived", "archived", "all"}
    assert "resources" in props["content_type"]["enum"]
    validator = Draft202012Validator(schema)
    assert validator.is_valid({"vault": "v", "name_query": "가" * 200, "content_type": "resources"})
    assert validator.is_valid({"vault": "v", "archive_scope": "archived", "offset": 0, "limit": 1})
    assert not validator.is_valid({"vault": "v", "name_query": "가" * 201})


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [
    {"name_query": "가" * 200}, {"name_query": ""}, {"content_type": "resources"},
])
async def test_new_page_modes_use_default_page_size(browse, options):
    response = await server._handle_browse({"vault": "v", **options}, "reader", None)
    assert "total" not in response and response["returned"] == 1
    assert browse.await_args.kwargs["page_size"] == 100
    for key, value in options.items():
        assert browse.await_args.kwargs[key] == value


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [
    {"filter": "a", "name_query": "b"}, {"filter": "", "name_query": ""},
    {"name_query": "a", "offset": 0}, {"content_type": "resources", "limit": 1},
    {"name_query": "x" * 201}, {"name_query": "x\x00y"}, {"name_query": 42},
    {"archive_scope": "unknown"},
])
async def test_invalid_options_are_rejected_before_browse(browse, options):
    args = {"vault": "v", **options}
    schema = next(t.input_schema for t in available_tools() if t.name == "akb_browse")
    assert not Draft202012Validator(schema).is_valid(args)
    result = await server._handle_browse(args, "reader", None)
    assert result["code"] == "invalid_argument"
    browse.assert_not_awaited()


@pytest.mark.asyncio
async def test_archive_scope_alone_preserves_legacy_pagination(browse):
    args = {"vault": "v", "filter": "item", "limit": 1, "offset": 0}
    legacy = await server._handle_browse(args, "reader", None)
    browse.assert_awaited_once_with(
        "v", collection=None, depth=1, content_type="all", include_hashes=False, include_archived=False,
    )
    explicit = await server._handle_browse({**args, "archive_scope": "unarchived"}, "reader", None)
    assert explicit == legacy and explicit["total"] == explicit["returned"] == 1
    assert browse.await_args.kwargs["archive_scope"] == "unarchived"
    assert "page_size" not in browse.await_args.kwargs
