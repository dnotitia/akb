"""Focused proof for the first candidate MCP operation slice."""

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
    DEFERRED_OPERATION_NAMES,
    FIRST_SLICE_LEGACY_NAMES,
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


def test_candidate_catalog_is_registry_owned_and_read_only() -> None:
    tools = _candidate_tools()

    later_tools = {tool.name for tool in available_tools()} - FIRST_SLICE_LEGACY_NAMES
    assert set(tools) == {"akb_discover", "akb_document_read", *later_tools}
    assert FIRST_SLICE_LEGACY_NAMES.isdisjoint(tools)
    legacy_names = {tool.name for tool in TOOLS}
    assert FIRST_SLICE_LEGACY_NAMES.isdisjoint(DEFERRED_OPERATION_NAMES)
    assert DEFERRED_OPERATION_NAMES <= legacy_names
    assert CANDIDATE_REGISTRY.first_slice_coverage() == {
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
    }

    for name in ("akb_discover", "akb_document_read"):
        tool = tools[name]
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert len(tool.input_schema["oneOf"]) == len(CANDIDATE_REGISTRY.actions_for(name))
        for branch in tool.input_schema["oneOf"]:
            assert branch["additionalProperties"] is False
            assert "action" in branch["required"]
            assert branch["properties"]["action"]["const"] in CANDIDATE_REGISTRY.actions_for(name)


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
        {"action": "unknown"},
        {"action": "get"},
        {"action": "get", "uri": 42},
        {"action": "get", "uri": "akb://v/doc/n.md", "section": "other-action"},
        {"action": "grep", "pattern": "needle", "replace": "mutation"},
    )
    for arguments in invalid_calls:
        public_tool = "akb_document_read" if arguments.get("action") in {"get", "unknown"} else "akb_discover"
        with pytest.raises(OperationValidationError):
            CANDIDATE_REGISTRY.validate(public_tool, arguments)


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
            names = {tool["name"] for tool in listed.json()["result"]["tools"]}
            assert {"akb_discover", "akb_document_read", "akb_help", "akb_sql"} <= names
            assert FIRST_SLICE_LEGACY_NAMES.isdisjoint(names)

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
