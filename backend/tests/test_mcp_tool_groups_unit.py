"""The mixed MCP catalog normalizes each action to its canonical operation."""

from __future__ import annotations

import json
import tempfile

import pytest

from app.config import settings

settings.git_storage_path = tempfile.mkdtemp(prefix="akb-mcp-groups-vaults-")

from mcp_server import server as mcp_server  # noqa: E402


@pytest.fixture
def quiet_sinks(monkeypatch):
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
    monkeypatch.setattr(mcp_server, "_can_read_vault", _deny_skill_projection)
    return audit, usage


async def _deny_skill_projection(*_args, **_kwargs):
    return False


@pytest.mark.asyncio
async def test_group_action_uses_canonical_dispatch_scope_and_audit(
    monkeypatch, quiet_sinks
):
    audit, usage = quiet_sinks
    calls = []

    async def current_user():
        return mcp_server._MCPUser(
            user_id="user-1",
            oauth_scopes=[mcp_server._READ_SCOPE],
        )

    async def search_handler(args, uid, user):
        calls.append((args, uid, user))
        return {"query": args["query"], "results": [{"uri": "akb://v/doc/a.md"}]}

    monkeypatch.setattr(mcp_server, "_get_user", current_user)
    monkeypatch.setitem(mcp_server._HANDLERS, "akb_search", search_handler)

    response = await mcp_server.call_tool(
        "akb_discover",
        {"action": "search", "query": "authentication", "vault": "v"},
    )

    assert json.loads(response.content[0].text) == {
        "query": "authentication",
        "results": [{"uri": "akb://v/doc/a.md"}],
    }
    assert len(calls) == 1
    assert calls[0][0] == {"query": "authentication", "vault": "v"}
    assert calls[0][1] == "user-1"
    assert calls[0][2].oauth_scopes == [mcp_server._READ_SCOPE]
    assert audit[0][0:2] == ("akb_search", {"query": "authentication", "vault": "v"})
    assert audit[0][3]["is_write"] is False
    assert usage[0][0:2] == ("akb_search", {"query": "authentication", "vault": "v"})
    assert usage[0][3]["is_write"] is False


@pytest.mark.asyncio
async def test_group_action_preserves_canonical_unknown_argument_error(
    monkeypatch, quiet_sinks
):
    audit, _usage = quiet_sinks

    async def current_user():
        return mcp_server._MCPUser(
            user_id="user-1",
            token_scopes=frozenset({"read"}),
        )

    monkeypatch.setattr(mcp_server, "_get_user", current_user)

    response = await mcp_server.call_tool(
        "akb_discover",
        {"action": "search", "query": "x", "misspelled": True},
    )
    result = json.loads(response.content[0].text)

    assert result["code"] == "unknown_argument"
    assert "for akb_search" in result["error"]
    assert result["details"]["available_arguments"] == sorted(
        mcp_server._TOOL_ARG_NAMES["akb_search"]
    )
    assert audit[0][0:2] == ("akb_search", {"query": "x", "misspelled": True})


@pytest.mark.asyncio
async def test_group_rejects_an_unknown_action(monkeypatch, quiet_sinks):
    audit, usage = quiet_sinks

    async def current_user():
        return mcp_server._MCPUser(token_scopes=frozenset({"read"}))

    monkeypatch.setattr(mcp_server, "_get_user", current_user)

    response = await mcp_server.call_tool(
        "akb_discover", {"action": "replace", "pattern": "x"}
    )
    result = json.loads(response.content[0].text)

    assert result["code"] == "invalid_argument"
    assert result["details"]["field"] == "action"
    assert result["details"]["allowed_values"] == list(
        mcp_server.TOOL_GROUPS["akb_discover"]
    )
    assert audit[0][0:2] == (
        "akb_discover",
        {"action": "replace", "pattern": "x"},
    )
    assert audit[0][3]["is_write"] is False
    assert usage[0][0:2] == audit[0][0:2]


@pytest.mark.parametrize(
    "operation",
    sorted(
        operation
        for actions in mcp_server.TOOL_GROUPS.values()
        for operation in actions.values()
    ),
)
@pytest.mark.asyncio
async def test_removed_direct_read_tool_is_not_a_public_dispatch_path(
    monkeypatch, quiet_sinks, operation
):
    async def current_user():
        return mcp_server._MCPUser(token_scopes=frozenset({"read"}))

    monkeypatch.setattr(mcp_server, "_get_user", current_user)

    response = await mcp_server.call_tool(operation, {})

    assert json.loads(response.content[0].text)["code"] == "unknown_tool"


@pytest.mark.parametrize(
    ("group", "action", "operation", "arguments"),
    [
        (
            "akb_document_read",
            "section",
            "akb_drill_down",
            {"uri": "akb://v/doc/a.md"},
        ),
        (
            "akb_vault_access",
            "explain",
            "akb_explain_access",
            {"vault": "v", "user": "user-2"},
        ),
    ],
)
@pytest.mark.asyncio
async def test_public_group_actions_match_the_accepted_contract(
    monkeypatch, quiet_sinks, group, action, operation, arguments
):
    audit, usage = quiet_sinks
    calls = []

    async def current_user():
        return mcp_server._MCPUser(
            user_id="user-1",
            token_scopes=frozenset({"read"}),
        )

    async def handler(args, uid, user):
        calls.append((args, uid))
        return {"operation": operation}

    monkeypatch.setattr(mcp_server, "_get_user", current_user)
    monkeypatch.setitem(mcp_server._HANDLERS, operation, handler)

    response = await mcp_server.call_tool(group, {"action": action, **arguments})
    result = json.loads(response.content[0].text)

    assert result == {"operation": operation}
    assert calls == [(arguments, "user-1")]
    assert audit[0][0:2] == (operation, arguments)
    assert usage[0][0:2] == (operation, arguments)
