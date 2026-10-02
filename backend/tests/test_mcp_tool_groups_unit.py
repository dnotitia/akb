"""The mixed MCP catalog normalizes each action to its canonical operation."""

from __future__ import annotations

import json
import tempfile

import pytest

from app.config import settings
from app.services import vault_skill_service as vss

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


def test_vault_skill_request_binding_tracks_the_canonical_tool_and_arguments():
    args = {"vault": "v1", "name": "table_a", "columns": []}
    binding = mcp_server._vault_skill_request_binding

    assert binding("akb_create_table", args) == binding(
        "akb_create_table", {"columns": [], "name": "table_a", "vault": "v1"}
    )
    assert binding("akb_create_table", args) != binding(
        "akb_alter_table", args
    )
    assert binding("akb_create_table", args) != binding(
        "akb_create_table", {**args, "name": "table_b"}
    )


@pytest.mark.asyncio
async def test_vault_skill_ack_does_not_authorize_a_different_write(
    monkeypatch, quiet_sinks
):
    """An ACK for A cannot let a different same-vault write B dispatch first."""
    audit, usage = quiet_sinks
    vss.reset()

    async def fetch_skill(_vault, _vault_id=None):
        return {"content": "# Vault instructions", "version": "v1"}

    permissions = {"oauth_scopes": ["akb:vault:read", "akb:vault:write"]}

    async def current_user():
        return mcp_server._MCPUser(
            user_id="user-1",
            username="writer",
            auth_method="oauth",
            oauth_scopes=permissions["oauth_scopes"],
            token_scopes=frozenset({"read", "write"}),
        )

    async def check_access(_uid, _vault, required_role="reader"):
        assert required_role == "reader"
        role = "writer" if "akb:vault:write" in permissions["oauth_scopes"] else "reader"
        return {
            "vault_id": "vault-uuid",
            "role": role,
            "role_source": "member",
            "status": "active",
        }

    created = []

    async def create_table(args, _uid, _user):
        created.append(args["name"])
        return {"created": True, "name": args["name"]}

    monkeypatch.setattr(vss, "_fetch_skill", fetch_skill)
    monkeypatch.setattr(mcp_server, "_get_user", current_user)
    monkeypatch.setattr(mcp_server, "_session_id", lambda: "session-1")
    monkeypatch.setattr(mcp_server, "_vault_skill_preflight_version", lambda: 2)
    monkeypatch.setattr(mcp_server, "check_vault_access", check_access)
    monkeypatch.setattr(mcp_server, "authorized_vault_id", lambda: "vault-uuid")
    monkeypatch.setattr(mcp_server, "authorized_vault", lambda: "vault-1")
    monkeypatch.setitem(mcp_server._HANDLERS, "akb_create_table", create_table)

    request_a = {
        "vault": "vault-1",
        "name": "t_ac6_a",
        "columns": [{"name": "title", "type": "text"}],
    }
    request_b = {**request_a, "name": "t_ac6_b"}

    try:
        first = await mcp_server.call_tool("akb_create_table", request_a)
        first_result = json.loads(first.content[0].text)
        assert first_result["code"] == "vault_skill_required"
        acknowledgement_a = first_result["vault_skill"]["ack_token"]

        # Send B with A's valid token before the exact A retry, matching the
        # observed exploit order. Neither the challenged A nor B may dispatch.
        mismatched = await mcp_server.call_tool(
            "akb_create_table",
            {**request_b, mcp_server.VAULT_SKILL_ACK_ARGUMENT: acknowledgement_a},
        )
        mismatched_result = json.loads(mismatched.content[0].text)
        assert mismatched_result["vault_skill"]["ack_token"] != acknowledgement_a

        # The credential permission snapshot also participates in the binding.
        permissions["oauth_scopes"] = ["akb:vault:read"]
        changed_permission = await mcp_server.call_tool(
            "akb_create_table",
            {**request_a, mcp_server.VAULT_SKILL_ACK_ARGUMENT: acknowledgement_a},
        )
        changed_permission_result = json.loads(changed_permission.content[0].text)
        assert changed_permission_result["code"] == "vault_skill_required"
        assert changed_permission_result["vault_skill"]["ack_token"] != acknowledgement_a

        permissions["oauth_scopes"] = ["akb:vault:read", "akb:vault:write"]
        retry_a = await mcp_server.call_tool(
            "akb_create_table",
            {**request_a, mcp_server.VAULT_SKILL_ACK_ARGUMENT: acknowledgement_a},
        )
        retry_a_result = json.loads(retry_a.content[0].text)

        assert (
            mismatched_result.get("code"),
            created,
            retry_a_result.get("name"),
        ) == ("vault_skill_required", ["t_ac6_a"], "t_ac6_a")
        assert mcp_server.VAULT_SKILL_ACK_ARGUMENT not in retry_a_result
        assert all(
            mcp_server.VAULT_SKILL_ACK_ARGUMENT not in args
            for _, args, _, _ in audit
        )
        assert all(
            mcp_server.VAULT_SKILL_ACK_ARGUMENT not in args
            for _, args, _, _ in usage
        )
    finally:
        vss.reset()


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
