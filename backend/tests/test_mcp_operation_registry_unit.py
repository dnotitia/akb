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


def test_candidate_catalog_is_registry_owned_with_all_backend_writes() -> None:
    tools = _candidate_tools()

    independent_tools = {tool.name for tool in available_tools()} - CANDIDATE_LEGACY_NAMES
    capability_tools = {
        "akb_discover",
        "akb_document_read",
        "akb_relationships",
        "akb_vault_access",
        "akb_identity",
        "akb_publication_read",
        "akb_export_read",
        "akb_document_write",
        "akb_collection_manage",
        "akb_relationship_manage",
        "akb_vault_access_manage",
        "akb_publication_manage",
        "akb_vault_manage",
        "akb_table_schema_manage",
        "akb_bundle_manage",
    }
    assert set(tools) == capability_tools | independent_tools
    assert CANDIDATE_REPLACED_NAMES.isdisjoint(tools)
    assert CANDIDATE_LEGACY_NAMES.isdisjoint(DEFERRED_OPERATION_NAMES)
    assert DEFERRED_OPERATION_NAMES == {
        "akb_put_file",
        "akb_get_file",
        "akb_delete_file",
    }
    expected_coverage = {
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
        "akb_put": ("akb_document_write", "put"),
        "akb_update": ("akb_document_write", "update"),
        "akb_edit": ("akb_document_write", "edit"),
        "akb_move": ("akb_document_write", "move"),
        "akb_delete": ("akb_document_write", "delete"),
        "akb_grep_replace": ("akb_document_write", "grep_replace"),
        "akb_create_collection": ("akb_collection_manage", "create"),
        "akb_delete_collection": ("akb_collection_manage", "delete"),
        "akb_link": ("akb_relationship_manage", "link"),
        "akb_unlink": ("akb_relationship_manage", "unlink"),
        "akb_grant": ("akb_vault_access_manage", "grant"),
        "akb_revoke": ("akb_vault_access_manage", "revoke"),
        "akb_transfer_ownership": ("akb_vault_access_manage", "transfer_ownership"),
        "akb_set_public": ("akb_vault_access_manage", "set_public"),
        "akb_publish": ("akb_publication_manage", "publish"),
        "akb_publication_snapshot": ("akb_publication_manage", "snapshot"),
        "akb_unpublish": ("akb_publication_manage", "unpublish"),
        "akb_create_vault": ("akb_vault_manage", "create"),
        "akb_archive_vault": ("akb_vault_manage", "archive"),
        "akb_delete_vault": ("akb_vault_manage", "delete"),
        "akb_create_table": ("akb_table_schema_manage", "create"),
        "akb_alter_table": ("akb_table_schema_manage", "alter"),
        "akb_drop_table": ("akb_table_schema_manage", "drop"),
        "akb_import": ("akb_bundle_manage", "import"),
    }
    assert CANDIDATE_REGISTRY.operation_coverage() == expected_coverage

    expected_write_contract = {
        "akb_put": ("parent", "writer", "write"),
        "akb_update": ("document_uri", "writer", "write"),
        "akb_edit": ("document_uri", "writer", "write"),
        "akb_move": ("document_uri", "writer", "write"),
        "akb_delete": ("document_uri", "writer", "destructive"),
        "akb_grep_replace": ("many_vaults", "writer", "destructive"),
        "akb_create_collection": ("vault", "writer", "write"),
        "akb_delete_collection": ("vault", "writer", "destructive"),
        "akb_link": ("source", "writer", "write"),
        "akb_unlink": ("source", "writer", "destructive"),
        "akb_grant": ("vault", "admin", "write"),
        "akb_revoke": ("vault", "admin", "destructive"),
        "akb_transfer_ownership": ("vault", "owner", "destructive"),
        "akb_set_public": ("vault", "owner", "destructive"),
        "akb_publish": ("publication", "writer", "write"),
        "akb_publication_snapshot": ("publication_slug", "writer", "write"),
        "akb_unpublish": ("publication_target", "writer", "destructive"),
        "akb_create_vault": ("none", None, "write"),
        "akb_archive_vault": ("vault", "owner", "destructive"),
        "akb_delete_vault": ("vault", "owner", "destructive"),
        "akb_create_table": ("parent", "writer", "write"),
        "akb_alter_table": ("table_uri", "admin", "destructive"),
        "akb_drop_table": ("table_uri", "admin", "destructive"),
        "akb_import": ("vault", "writer", "write"),
    }
    assert len(expected_write_contract) == 24
    for operation, (public_tool, action) in expected_coverage.items():
        if operation not in expected_write_contract:
            continue
        spec = CANDIDATE_REGISTRY.spec_for(public_tool, action)
        assert spec is not None
        target, role, risk = expected_write_contract[operation]
        assert (spec.target, spec.vault_role, spec.risk) == (target, role, risk)
        assert spec.required_scope == "akb:vault:write"

    assert set(INDEPENDENT_OPERATION_REASONS) == {"akb_help", "akb_sql"}
    assert set(DEFERRED_OPERATION_REASONS) == {"stdio_local_files"}

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

    for name in capability_tools - {
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
        assert tool.annotations.read_only_hint is False
        assert tool.annotations.destructive_hint is (
            name != "akb_bundle_manage"
        )
        for branch in tool.input_schema["oneOf"]:
            assert branch["additionalProperties"] is False
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
        (
            "akb_document_write",
            {"action": "grep_replace", "pattern": "needle", "replace": ""},
        ),
        (
            "akb_document_write",
            {"action": "grep_replace", "vault": "v", "pattern": "needle"},
        ),
        ("akb_document_write", {"action": "put", "title": "t", "content": "c"}),
        (
            "akb_publication_manage",
            {"action": "publish", "resource_type": "document", "vault": "v"},
        ),
        (
            "akb_publication_manage",
            {"action": "publish", "resource_type": "table_query", "vault": "v"},
        ),
        (
            "akb_publication_manage",
            {"action": "publish", "vault": "v", "query_sql": "SELECT 1"},
        ),
        (
            "akb_publication_manage",
            {"action": "unpublish"},
        ),
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

    grep_replace = CANDIDATE_REGISTRY.validate(
        "akb_document_write",
        {
            "action": "grep_replace",
            "vault": ["v1", "v2"],
            "pattern": "needle",
            "replace": "",
        },
    )
    assert grep_replace.required_scope == "akb:vault:write"
    assert grep_replace.risk == "destructive"

    publish_query = CANDIDATE_REGISTRY.validate(
        "akb_publication_manage",
        {
            "action": "publish",
            "resource_type": "table_query",
            "vault": "v",
            "query_sql": "SELECT 1",
        },
    )
    assert publish_query.logical_audit_operation == "akb_publish"


def test_graph_target_resolution_keeps_uri_and_vault_forms() -> None:
    graph = CANDIDATE_REGISTRY.spec_for("akb_relationships", "graph")
    assert graph is not None
    assert CANDIDATE_REGISTRY.vaults_for(
        graph, {"uri": "akb://vault-a/doc/spec.md"}
    ) == ("vault-a",)
    assert CANDIDATE_REGISTRY.vaults_for(graph, {"vault": "vault-b"}) == ("vault-b",)


def test_write_target_resolution_covers_uri_parent_and_multi_vault_actions() -> None:
    put = CANDIDATE_REGISTRY.spec_for("akb_document_write", "put")
    grep = CANDIDATE_REGISTRY.spec_for("akb_document_write", "grep_replace")
    publish = CANDIDATE_REGISTRY.spec_for("akb_publication_manage", "publish")
    unpublish = CANDIDATE_REGISTRY.spec_for("akb_publication_manage", "unpublish")
    link = CANDIDATE_REGISTRY.spec_for("akb_relationship_manage", "link")
    assert put and grep and publish and unpublish and link

    assert CANDIDATE_REGISTRY.vaults_for(
        put, {"parent": "akb://parent-v/coll/specs", "vault": "ignored"}
    ) == ("parent-v",)
    assert CANDIDATE_REGISTRY.vaults_for(
        put, {"vault": "fallback-v", "collection": "specs"}
    ) == ("fallback-v",)
    assert CANDIDATE_REGISTRY.vaults_for(
        grep, {"vault": ["v1", "v2", "v1"]}
    ) == ("v1", "v2")
    assert CANDIDATE_REGISTRY.vaults_for(
        publish,
        {"resource_type": "table_query", "vault": "base", "query_vault_names": ["base", "extra"]},
    ) == ("base", "extra")
    assert CANDIDATE_REGISTRY.vaults_for(
        link,
        {"source": "akb://same/doc/a.md", "target": "akb://same/table/t"},
    ) == ("same",)
    assert CANDIDATE_REGISTRY.vaults_for(
        link,
        {"source": "akb://same/doc/a.md", "target": "akb://other/doc/t.md"},
    ) == ()
    assert CANDIDATE_REGISTRY.vaults_for(
        link,
        {"source": "akb://same/doc/a.md", "target": "not-an-akb-uri"},
    ) == ()
    assert CANDIDATE_REGISTRY.vaults_for(
        publish, {"resource_type": "file", "uri": "akb://v/doc/a.md"}
    ) == ()
    assert CANDIDATE_REGISTRY.vaults_for(
        publish, {"uri": "akb://v/table/t"}
    ) == ()
    assert CANDIDATE_REGISTRY.vaults_for(
        unpublish, {"uri": "akb://v/doc/a.md"}
    ) == ("v",)
    assert CANDIDATE_REGISTRY.vaults_for(
        unpublish, {"uri": "akb://v/table/t"}
    ) == ()


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
@pytest.mark.parametrize(
    ("public_tool", "arguments", "expected_code"),
    [
        (
            "akb_relationship_manage",
            {
                "action": "link",
                "source": "akb://source-v/doc/a.md",
                "target": "akb://other-v/doc/b.md",
                "relation": "related_to",
            },
            "invalid_argument",
        ),
        (
            "akb_publication_manage",
            {
                "action": "publish",
                "resource_type": "file",
                "uri": "akb://publish-v/doc/a.md",
            },
            "invalid_uri",
        ),
    ],
)
async def test_invalid_target_semantics_precede_candidate_rbac(
    monkeypatch: pytest.MonkeyPatch,
    server_module,
    public_tool: str,
    arguments: dict,
    expected_code: str,
) -> None:
    checked = []

    async def forbidden(_uid, vault, *, required_role):
        checked.append((vault, required_role))
        raise ForbiddenError("denied")

    monkeypatch.setattr(server_module, "check_vault_access", forbidden)
    result = await server_module._dispatch(
        public_tool,
        arguments,
        server_module._MCPUser(user_id="u-1"),
    )

    assert result["code"] == expected_code
    assert checked == []


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
async def test_new_write_actions_check_their_target_role_before_handler(
    monkeypatch: pytest.MonkeyPatch, server_module
) -> None:
    cases = (
        (
            "akb_document_write", "put",
            {"parent": "akb://doc-v/coll/specs", "title": "t", "content": "c"},
            "doc-v", "writer",
        ),
        (
            "akb_collection_manage", "create", {"vault": "collection-v", "path": "docs"},
            "collection-v", "writer",
        ),
        (
            "akb_relationship_manage", "link",
            {
                "source": "akb://link-v/doc/a.md",
                "target": "akb://link-v/table/t",
                "relation": "references",
            },
            "link-v", "writer",
        ),
        (
            "akb_vault_access_manage", "grant",
            {"vault": "access-v", "user": "reader", "role": "reader"},
            "access-v", "admin",
        ),
        (
            "akb_publication_manage", "publish", {"uri": "akb://publish-v/doc/a.md"},
            "publish-v", "writer",
        ),
        (
            "akb_vault_manage", "archive", {"vault": "owner-v"},
            "owner-v", "owner",
        ),
        (
            "akb_table_schema_manage", "alter", {"uri": "akb://table-v/table/t"},
            "table-v", "admin",
        ),
        (
            "akb_bundle_manage", "import", {"vault": "bundle-v", "files": {}},
            "bundle-v", "writer",
        ),
        (
            "akb_publication_manage", "snapshot", {"slug": "private-publication"},
            "slug-v", "writer",
        ),
    )
    checked = []
    called = []

    async def deny(_uid, vault, *, required_role):
        checked.append((vault, required_role))
        raise ForbiddenError("denied")

    async def handler(*_args):
        called.append(True)
        return {"ok": True}

    async def publication_vault(_slug):
        return "slug-v"

    monkeypatch.setattr(server_module, "check_vault_access", deny)
    monkeypatch.setattr(server_module, "_publication_vault_for_slug", publication_vault)
    keys = []
    for public_tool, action, _arguments, _vault, _role in cases:
        key = (public_tool, action)
        keys.append((key, CANDIDATE_REGISTRY._handlers[key]))
        CANDIDATE_REGISTRY._handlers[key] = handler
    try:
        for public_tool, action, arguments, _vault, _role in cases:
            result = await server_module._dispatch(
                public_tool,
                {"action": action, **arguments},
                server_module._MCPUser(user_id="u-1"),
            )
            assert result["code"] == "permission_denied", (public_tool, action, result)
    finally:
        for key, original in keys:
            CANDIDATE_REGISTRY._handlers[key] = original

    assert checked == [(vault, role) for *_rest, vault, role in cases]
    assert called == []


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
                "akb_document_write",
                "akb_collection_manage",
                "akb_relationship_manage",
                "akb_vault_access_manage",
                "akb_publication_manage",
                "akb_vault_manage",
                "akb_table_schema_manage",
                "akb_bundle_manage",
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
            document_write = next(
                tool for tool in listed_tools if tool["name"] == "akb_document_write"
            )
            grep_replace = next(
                branch
                for branch in document_write["inputSchema"]["oneOf"]
                if branch["properties"]["action"].get("const") == "grep_replace"
            )
            assert {"pattern", "replace"} <= set(grep_replace["required"])
            assert grep_replace["anyOf"] == [{"required": ["vault"]}]
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

            unscoped_grep_rejected = await client.post(
                "/mcp/",
                headers={
                    **headers,
                    "mcp-method": "tools/call",
                    "mcp-name": "akb_document_write",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "akb_document_write",
                        "arguments": {
                            "action": "grep_replace",
                            "pattern": "needle",
                            "replace": "",
                        },
                        "_meta": meta,
                    },
                },
            )
            unscoped_grep_body = json.loads(
                unscoped_grep_rejected.json()["result"]["content"][0]["text"]
            )
            assert unscoped_grep_body["code"] == "invalid_argument"

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
