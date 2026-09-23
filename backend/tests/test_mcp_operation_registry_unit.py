"""Focused proof for the candidate MCP operation registry."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
import httpx

from app.config import settings
from app.exceptions import ForbiddenError
from app.services import audit_log
from app.services.auth_service import AuthenticatedUser
from mcp_server.operation_registry import (
    CANDIDATE_LEGACY_NAMES,
    CANDIDATE_REPLACED_NAMES,
    DEFERRED_OPERATION_REASONS,
    DEFERRED_OPERATION_NAMES,
    DEFERRED_MUTATION_NAMES,
    INDEPENDENT_OPERATION_REASONS,
    OperationRegistry,
    OperationValidationError,
    READ_SCOPE,
)
from mcp_server.tools import CANDIDATE_REGISTRY, TOOLS, available_tools, candidate_tools


@pytest.fixture
def server_module(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    from mcp_server import server

    return server


def _candidate_tools() -> dict:
    return {tool.name: tool for tool in candidate_tools()}


def test_candidate_catalog_is_registry_owned_with_deferred_grep_write() -> None:
    tools = _candidate_tools()

    later_tools = {tool.name for tool in available_tools()} - CANDIDATE_LEGACY_NAMES
    candidate_tools = {
        "akb_discover",
        "akb_document_read",
        "akb_relationships",
        "akb_vault_access",
        "akb_identity",
        "akb_publication_read",
        "akb_export_read",
        "akb_grep",
        *later_tools,
    }
    assert set(tools) == candidate_tools
    assert CANDIDATE_REPLACED_NAMES.isdisjoint(tools)
    assert DEFERRED_MUTATION_NAMES == {"akb_grep"}
    assert "akb_grep" in tools
    assert "replace" in tools["akb_grep"].input_schema["required"]
    assert tools["akb_grep"].annotations is not None
    assert tools["akb_grep"].annotations.read_only_hint is False
    assert tools["akb_grep"].annotations.destructive_hint is True
    legacy_names = {tool.name for tool in TOOLS}
    assert CANDIDATE_LEGACY_NAMES.isdisjoint(DEFERRED_OPERATION_NAMES)
    assert DEFERRED_OPERATION_NAMES <= legacy_names
    assert CANDIDATE_REGISTRY.operation_coverage() == {
        "akb_list_vaults": ("akb_discover", "list_vaults"),
        "akb_vault_info": ("akb_discover", "vault_info"),
        "akb_browse": ("akb_discover", "browse"),
        "akb_search": ("akb_discover", "search"),
        "akb_grep": ("akb_discover", "grep"),
        "akb_get": ("akb_document_read", "get"),
        "akb_drill_down": ("akb_document_read", "section"),
        "akb_activity": ("akb_document_read", "activity"),
        "akb_history": ("akb_document_read", "history"),
        "akb_diff": ("akb_document_read", "diff"),
        "akb_provenance": ("akb_document_read", "provenance"),
        "akb_relations": ("akb_relationships", "relations"),
        "akb_graph": ("akb_relationships", "graph"),
        "akb_vault_members": ("akb_vault_access", "members"),
        "akb_explain_access": ("akb_vault_access", "explain"),
        "akb_whoami": ("akb_identity", "whoami"),
        "akb_search_users": ("akb_identity", "search_users"),
        "akb_publications": ("akb_publication_read", "list"),
        "akb_export": ("akb_export_read", "export"),
    }

    assert set(INDEPENDENT_OPERATION_REASONS) == {"akb_help", "akb_sql"}
    assert set(DEFERRED_OPERATION_REASONS) == {"backend_write_manage", "stdio_local_files"}

    for name in {
        "akb_discover",
        "akb_document_read",
        "akb_relationships",
        "akb_vault_access",
        "akb_identity",
        "akb_publication_read",
        "akb_export_read",
    }:
        tool = tools[name]
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert len(tool.input_schema["oneOf"]) == len(CANDIDATE_REGISTRY.actions_for(name))
        for branch in tool.input_schema["oneOf"]:
            assert branch["additionalProperties"] is False
            assert "action" in branch["required"]
            assert branch["properties"]["action"]["const"] in CANDIDATE_REGISTRY.actions_for(name)


def test_remaining_backend_read_operations_are_candidate_actions() -> None:
    tools = _candidate_tools()
    expected_tools = {
        "akb_relationships",
        "akb_vault_access",
        "akb_identity",
        "akb_publication_read",
        "akb_export_read",
    }
    assert expected_tools <= set(tools)

    expected_coverage = {
        "akb_relations": ("akb_relationships", "relations"),
        "akb_graph": ("akb_relationships", "graph"),
        "akb_vault_members": ("akb_vault_access", "members"),
        "akb_explain_access": ("akb_vault_access", "explain"),
        "akb_whoami": ("akb_identity", "whoami"),
        "akb_search_users": ("akb_identity", "search_users"),
        "akb_publications": ("akb_publication_read", "list"),
        "akb_export": ("akb_export_read", "export"),
    }
    assert {
        name: CANDIDATE_REGISTRY.operation_coverage().get(name)
        for name in expected_coverage
    } == expected_coverage
    assert set(expected_coverage).isdisjoint(set(tools))
    for operation, (public_tool, action) in expected_coverage.items():
        spec = CANDIDATE_REGISTRY.spec_for(public_tool, action)
        assert spec is not None
        assert spec.handler == operation
        assert spec.required_scope == READ_SCOPE
        assert spec.risk == "read"
        assert spec.logical_audit_operation == operation
        assert spec.vault_role is not None or spec.target == "none"


def test_registry_rejects_duplicate_and_incomplete_contracts() -> None:
    spec = CANDIDATE_REGISTRY.specs[0]
    registry = OperationRegistry()
    registry.register(spec)
    with pytest.raises(ValueError, match="duplicate candidate operation"):
        registry.register(spec)

    for replacement, message in (
        ({"public_tool": ""}, "requires public_tool"),
        ({"handler": ""}, "requires public_tool"),
        ({"logical_audit_operation": ""}, "missing logical audit operation"),
        ({"required_scope": ""}, "invalid OAuth scope"),
    ):
        with pytest.raises(ValueError, match=message):
            OperationRegistry().register(replace(spec, **replacement))

    assert spec.required_scope == READ_SCOPE


def test_registry_validation_rejects_bad_action_shapes() -> None:
    invalid_calls = (
        ("akb_document_read", {"action": "unknown"}),
        ("akb_document_read", {"action": "get"}),
        ("akb_document_read", {"action": "get", "uri": 42}),
        (
            "akb_document_read",
            {"action": "get", "uri": "akb://v/doc/n.md", "section": "other-action"},
        ),
        ("akb_discover", {"action": "grep", "pattern": "needle", "replace": "mutation"}),
        ("akb_relationships", {"action": "graph"}),
        ("akb_relationships", {"action": "relations", "uri": "akb://v/doc/n.md", "vault": "v"}),
        ("akb_vault_access", {"action": "explain", "vault": "v"}),
        ("akb_identity", {"action": "search_users", "query": 42}),
        ("akb_publication_read", {"action": "list", "vault": "v", "resource_type": "user"}),
        ("akb_export_read", {"action": "export", "vault": "v", "format": "zip"}),
    )
    for public_tool, arguments in invalid_calls:
        with pytest.raises(OperationValidationError):
            CANDIDATE_REGISTRY.validate(public_tool, arguments)


def test_graph_target_resolution_keeps_uri_and_vault_forms() -> None:
    graph = CANDIDATE_REGISTRY.spec_for("akb_relationships", "graph")
    assert graph is not None
    assert CANDIDATE_REGISTRY.vaults_for(
        graph, {"uri": "akb://vault-a/doc/spec.md"}
    ) == ("vault-a",)
    assert CANDIDATE_REGISTRY.vaults_for(graph, {"vault": "vault-b"}) == ("vault-b",)


@pytest.mark.asyncio
async def test_scope_denial_happens_before_candidate_handler(
    monkeypatch: pytest.MonkeyPatch, server_module
) -> None:
    called = False

    async def forbidden_handler(_args, _uid, _user):
        nonlocal called
        called = True
        return {"ok": True}

    key = ("akb_document_read", "get")
    original = CANDIDATE_REGISTRY._handlers[key]
    CANDIDATE_REGISTRY._handlers[key] = forbidden_handler
    monkeypatch.setattr(server_module, "check_vault_access", lambda *_args, **_kwargs: None)
    try:
        user = server_module._MCPUser(user_id="u-1", oauth_scopes=[])
        result = await server_module._dispatch(
            "akb_document_read",
            {"action": "get", "uri": "akb://v/doc/n.md"},
            user,
        )
    finally:
        CANDIDATE_REGISTRY._handlers[key] = original

    assert result["code"] == "insufficient_scope"
    assert called is False


@pytest.mark.asyncio
async def test_all_new_read_actions_require_read_scope_before_handler(
    server_module,
) -> None:
    cases = (
        ("akb_relationships", "relations", {"uri": "akb://v/doc/n.md"}),
        ("akb_relationships", "graph", {"vault": "v"}),
        ("akb_vault_access", "members", {"vault": "v"}),
        ("akb_vault_access", "explain", {"vault": "v", "user": "reader"}),
        ("akb_identity", "whoami", {}),
        ("akb_identity", "search_users", {"query": "reader"}),
        ("akb_publication_read", "list", {"vault": "v"}),
        ("akb_export_read", "export", {"vault": "v"}),
    )
    called: list[tuple[str, str]] = []
    originals = {}

    for public_tool, action, _args in cases:
        spec = CANDIDATE_REGISTRY.spec_for(public_tool, action)
        assert spec is not None
        key = (public_tool, action)
        originals[key] = CANDIDATE_REGISTRY._handlers[key]

        async def handler(_args, _uid, _user, *, _key=key):
            called.append(_key)
            return {"ok": True}

        CANDIDATE_REGISTRY._handlers[key] = handler

    user = server_module._MCPUser(user_id="u-1", username="reader", oauth_scopes=[])
    try:
        for public_tool, action, args in cases:
            result = await server_module._dispatch(
                public_tool, {"action": action, **args}, user
            )
            assert result["code"] == "insufficient_scope"
    finally:
        CANDIDATE_REGISTRY._handlers.update(originals)

    assert called == []


@pytest.mark.asyncio
async def test_explain_access_role_is_checked_before_handler(
    monkeypatch: pytest.MonkeyPatch, server_module
) -> None:
    called = False
    checked: list[tuple[str, str]] = []

    async def role_for(_uid, username):
        return "reader" if username == "reader" else "admin"

    async def deny(_uid, vault, *, required_role):
        checked.append((vault, required_role))
        raise ForbiddenError("denied")

    async def handler(_args, _uid, _user):
        nonlocal called
        called = True
        return {"ok": True}

    key = ("akb_vault_access", "explain")
    original = CANDIDATE_REGISTRY._handlers[key]
    CANDIDATE_REGISTRY._handlers[key] = handler
    monkeypatch.setattr(server_module, "required_explain_access_role", role_for)
    monkeypatch.setattr(server_module, "check_vault_access", deny)
    try:
        result = await server_module._dispatch(
            "akb_vault_access",
            {"action": "explain", "vault": "private", "user": "owner"},
            server_module._MCPUser(user_id="reader-id", username="reader"),
        )
    finally:
        CANDIDATE_REGISTRY._handlers[key] = original

    assert result["code"] == "permission_denied"
    assert checked == [("private", "admin")]
    assert called is False


@pytest.mark.asyncio
async def test_vault_rbac_denial_happens_before_candidate_handler(
    monkeypatch: pytest.MonkeyPatch, server_module
) -> None:
    called = False

    async def forbidden_handler(_args, _uid, _user):
        nonlocal called
        called = True
        return {"ok": True}

    async def deny(*_args, **_kwargs):
        raise ForbiddenError("denied")

    key = ("akb_document_read", "get")
    original = CANDIDATE_REGISTRY._handlers[key]
    CANDIDATE_REGISTRY._handlers[key] = forbidden_handler
    monkeypatch.setattr(server_module, "check_vault_access", deny)
    try:
        result = await server_module._dispatch(
            "akb_document_read",
            {"action": "get", "uri": "akb://private/doc/n.md"},
            server_module._MCPUser(user_id="u-1"),
        )
    finally:
        CANDIDATE_REGISTRY._handlers[key] = original

    assert result["code"] == "permission_denied"
    assert called is False


@pytest.mark.asyncio
async def test_candidate_handler_receives_only_action_arguments(
    monkeypatch: pytest.MonkeyPatch, server_module
) -> None:
    received: dict = {}

    async def read_handler(args, uid, _user):
        received.update({"args": args, "uid": uid})
        return {"ok": True}

    async def allow(*_args, **_kwargs):
        return {"vault_id": "v-id"}

    key = ("akb_document_read", "get")
    original = CANDIDATE_REGISTRY._handlers[key]
    CANDIDATE_REGISTRY._handlers[key] = read_handler
    monkeypatch.setattr(server_module, "check_vault_access", allow)
    try:
        result = await server_module._dispatch(
            "akb_document_read",
            {"action": "get", "uri": "akb://v/doc/n.md", "version": "a" * 7},
            server_module._MCPUser(user_id="u-1"),
        )
    finally:
        CANDIDATE_REGISTRY._handlers[key] = original

    assert result == {"ok": True}
    assert received == {
        "args": {"uri": "akb://v/doc/n.md", "version": "a" * 7},
        "uid": "u-1",
    }


def test_sql_remains_an_independent_legacy_contract() -> None:
    tools = _candidate_tools()
    sql = tools["akb_sql"]
    assert sql.input_schema["required"] == ["sql"]
    assert "action" not in sql.input_schema["properties"]
    assert "akb_sql" in {tool.name for tool in TOOLS}


def test_audit_records_registry_logical_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    records: list[dict] = []

    monkeypatch.setattr(settings.audit, "enabled", True)
    monkeypatch.setattr(settings.audit, "log_reads", True)
    monkeypatch.setattr(audit_log, "record", lambda **kwargs: records.append(kwargs))

    audit_log.record_tool(
        "akb_document_read",
        {"action": "get", "uri": "akb://v/doc/n.md"},
        type("User", (), {"username": "reader", "user_id": "u-1"})(),
        {"ok": True},
        logical_operation="akb_get",
    )

    assert records[0]["action"] == "akb_get"
    assert records[0]["meta"]["public_tool"] == "akb_document_read"
    assert records[0]["meta"]["action"] == "get"


@pytest.mark.asyncio
async def test_candidate_http_catalog_and_action_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from mcp_server import http_app

    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    user = AuthenticatedUser(
        user_id="00000000-0000-0000-0000-000000000001",
        username="reader",
        email="reader@example.invalid",
        display_name="Reader",
        is_admin=False,
        auth_method="pat",
    )
    async def resolve(_header):
        return user

    monkeypatch.setattr(http_app, "resolve_mcp_authorization", resolve)
    activity_calls: list[dict] = []
    identity_calls: list[dict] = []
    activity_key = ("akb_document_read", "activity")
    identity_key = ("akb_identity", "whoami")

    async def activity_stub(args, _uid, _user):
        activity_calls.append(args)
        return {"unexpected": True}

    async def identity_stub(args, _uid, _user):
        identity_calls.append(args)
        return {"unexpected": True}

    monkeypatch.setitem(CANDIDATE_REGISTRY._handlers, activity_key, activity_stub)
    monkeypatch.setitem(CANDIDATE_REGISTRY._handlers, identity_key, identity_stub)

    headers = {
        "authorization": "Bearer test-token",
        "content-type": "application/json",
        "accept": "application/json",
        "mcp-protocol-version": "2026-07-28",
        "mcp-method": "tools/list",
    }
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "registry-test", "version": "1"},
    }

    app = http_app.MCPApp()
    async with app.run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            listed = await client.post(
                "/mcp/",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": meta}},
            )
            assert listed.status_code == 200
            listed_tools = listed.json()["result"]["tools"]
            names = {tool["name"] for tool in listed_tools}
            expected_capabilities = {
                "akb_discover",
                "akb_document_read",
                "akb_relationships",
                "akb_vault_access",
                "akb_identity",
                "akb_publication_read",
                "akb_export_read",
                "akb_help",
                "akb_sql",
            }
            assert expected_capabilities <= names
            assert CANDIDATE_REPLACED_NAMES.isdisjoint(names)
            for name in expected_capabilities - {"akb_help", "akb_sql"}:
                listed_tool = next(tool for tool in listed_tools if tool["name"] == name)
                branches = listed_tool["inputSchema"]["oneOf"]
                assert branches
                assert "action" in listed_tool["description"].lower()
                assert all(branch["additionalProperties"] is False for branch in branches)
                assert all("action" in branch["required"] for branch in branches)
                assert all(
                    branch["properties"]["action"].get("const")
                    in CANDIDATE_REGISTRY.actions_for(name)
                    for branch in branches
                )
            grep = next(tool for tool in listed_tools if tool["name"] == "akb_grep")
            assert "replace" in grep["inputSchema"]["required"]
            discover_grep = next(
                branch
                for branch in next(
                    tool for tool in listed_tools if tool["name"] == "akb_discover"
                )["inputSchema"]["oneOf"]
                if branch["properties"]["action"].get("const") == "grep"
            )
            assert "replace" not in discover_grep["properties"]
            assert "max_replacements" not in discover_grep["properties"]

            legacy_rejected = await client.post(
                "/mcp/",
                headers={**headers, "mcp-method": "tools/call", "mcp-name": "akb_get"},
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "akb_get",
                        "arguments": {"uri": "akb://v/doc/n.md"},
                        "_meta": meta,
                    },
                },
            )
            legacy_body = json.loads(legacy_rejected.json()["result"]["content"][0]["text"])
            assert legacy_body["code"] == "unknown_tool"

            graph_alias_rejected = await client.post(
                "/mcp/",
                headers={**headers, "mcp-method": "tools/call", "mcp-name": "akb_graph"},
                json={
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {
                        "name": "akb_graph",
                        "arguments": {"vault": "v"},
                        "_meta": meta,
                    },
                },
            )
            graph_alias_body = json.loads(
                graph_alias_rejected.json()["result"]["content"][0]["text"]
            )
            assert graph_alias_body["code"] == "unknown_tool"

            identity_read = await client.post(
                "/mcp/",
                headers={**headers, "mcp-method": "tools/call", "mcp-name": "akb_identity"},
                json={
                    "jsonrpc": "2.0",
                    "id": 8,
                    "method": "tools/call",
                    "params": {
                        "name": "akb_identity",
                        "arguments": {"action": "whoami"},
                        "_meta": meta,
                    },
                },
            )
            identity_body = json.loads(
                identity_read.json()["result"]["content"][0]["text"]
            )
            assert identity_body == {"unexpected": True}
            assert identity_calls == [{}]

            invalid_identity = await client.post(
                "/mcp/",
                headers={**headers, "mcp-method": "tools/call", "mcp-name": "akb_identity"},
                json={
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "tools/call",
                    "params": {
                        "name": "akb_identity",
                        "arguments": {"action": "search_users", "query": 42},
                        "_meta": meta,
                    },
                },
            )
            invalid_identity_body = json.loads(
                invalid_identity.json()["result"]["content"][0]["text"]
            )
            assert invalid_identity_body["code"] == "invalid_argument"
            assert identity_calls == [{}]

            deferred_rejected = await client.post(
                "/mcp/",
                headers={**headers, "mcp-method": "tools/call", "mcp-name": "akb_grep"},
                json={
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "akb_grep",
                        "arguments": {"pattern": "needle"},
                        "_meta": meta,
                    },
                },
            )
            deferred_body = json.loads(
                deferred_rejected.json()["result"]["content"][0]["text"]
            )
            assert deferred_body["code"] == "invalid_argument"

            read_mutation_rejected = await client.post(
                "/mcp/",
                headers={**headers, "mcp-method": "tools/call", "mcp-name": "akb_discover"},
                json={
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {
                        "name": "akb_discover",
                        "arguments": {
                            "action": "grep",
                            "pattern": "needle",
                            "max_replacements": 1,
                        },
                        "_meta": meta,
                    },
                },
            )
            read_mutation_body = json.loads(
                read_mutation_rejected.json()["result"]["content"][0]["text"]
            )
            assert read_mutation_body["code"] == "unknown_argument"

            unknown_activity_argument = await client.post(
                "/mcp/",
                headers={
                    **headers,
                    "mcp-method": "tools/call",
                    "mcp-name": "akb_document_read",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "tools/call",
                    "params": {
                        "name": "akb_document_read",
                        "arguments": {
                            "action": "activity",
                            "vault": "candidate-vault",
                            "user": "nobody",
                        },
                        "_meta": meta,
                    },
                },
            )
            unknown_activity_body = json.loads(
                unknown_activity_argument.json()["result"]["content"][0]["text"]
            )
            assert unknown_activity_body["code"] == "unknown_argument"
            assert "author" in unknown_activity_body.get("hint", "")
            assert activity_calls == []

            call_headers = {**headers, "mcp-method": "tools/call", "mcp-name": "akb_document_read"}
            rejected = await client.post(
                "/mcp/",
                headers=call_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "akb_document_read",
                        "arguments": {"action": "not-an-action"},
                        "_meta": meta,
                    },
                },
            )
            assert rejected.status_code == 200
            payload = rejected.json()["result"]
            body = json.loads(payload["content"][0]["text"])
            assert body["code"] == "invalid_argument"
