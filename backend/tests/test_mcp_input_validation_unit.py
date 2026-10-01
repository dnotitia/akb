"""MCP calls enforce the published operation schemas before dispatch."""

from __future__ import annotations

import json
import tempfile

import pytest

from app.config import settings

settings.git_storage_path = tempfile.mkdtemp(prefix="akb-mcp-schema-vaults-")

from mcp.types import CallToolRequestParams  # noqa: E402
from mcp_server import server as mcp_server  # noqa: E402


@pytest.fixture
def audit_sinks(monkeypatch):
    audit = []
    usage = []
    monkeypatch.setattr(
        mcp_server.audit_log,
        "record_tool",
        lambda name, args, user, result, **kwargs: audit.append(
            (name, args, result, kwargs)
        ),
    )
    monkeypatch.setattr(
        mcp_server.tool_usage,
        "record",
        lambda name, args, user, result, **kwargs: usage.append(
            (name, args, result, kwargs)
        ),
    )
    monkeypatch.setattr(mcp_server, "_vault_skill_preflight_version", lambda: None)
    return audit, usage


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "name",
        "arguments",
        "field",
        "expected_type",
        "handler_name",
        "audit_name",
        "audit_arguments",
    ),
    [
        (
            "akb_create_table",
            {"name": "items", "columns": "[]"},
            "columns",
            "array",
            "akb_create_table",
            "akb_create_table",
            {"name": "items", "columns": "[]"},
        ),
        (
            "akb_import",
            {"vault": "v", "files": []},
            "files",
            "object",
            "akb_import",
            "akb_import",
            {"vault": "v", "files": []},
        ),
        (
            "akb_document_read",
            {"action": "get"},
            "uri",
            "string",
            "akb_get",
            "akb_get",
            {},
        ),
        (
            "akb_document_read",
            {"action": "get", "uri": 42},
            "uri",
            "string",
            "akb_get",
            "akb_get",
            {"uri": 42},
        ),
    ],
)
async def test_sdk_tool_request_rejects_invalid_schema_before_handler(
    monkeypatch,
    audit_sinks,
    name,
    arguments,
    field,
    expected_type,
    handler_name,
    audit_name,
    audit_arguments,
):
    audit, usage = audit_sinks
    dispatches = []

    async def current_user():
        return mcp_server._MCPUser(user_id="user-1", token_scopes=frozenset({"read", "write"}))

    async def handler(args, uid, user):
        dispatches.append((args, uid))
        return {"accepted": True}

    monkeypatch.setattr(mcp_server, "_get_user", current_user)
    monkeypatch.setitem(mcp_server._HANDLERS, handler_name, handler)

    response = await mcp_server._call_tool_request(
        None,
        CallToolRequestParams(name=name, arguments=arguments),
    )
    payload = json.loads(response.content[0].text)

    assert response.is_error is True
    assert payload["code"] == "invalid_argument"
    assert payload["details"]["field"] == field
    assert payload["details"]["expected_type"] == expected_type
    assert payload.get("hint")
    assert dispatches == []
    assert audit[0][0:2] == (audit_name, audit_arguments)
    assert usage[0][0:2] == (audit_name, audit_arguments)
