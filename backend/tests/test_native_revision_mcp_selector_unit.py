"""MCP wire contracts for Native Revision selector ambiguity."""

from __future__ import annotations

import json

import pytest

from app.repositories.native_revision_repo import NativeRevisionSelectorAmbiguousError


pytestmark = pytest.mark.asyncio


class _AmbiguousNativeBackend:
    async def document_version(self, _vault: str, _doc_ref: str, version: str):
        raise NativeRevisionSelectorAmbiguousError(version)

    async def document_diff(self, _vault: str, _doc_ref: str, commit: str):
        raise NativeRevisionSelectorAmbiguousError(commit)


@pytest.mark.parametrize(
    ("tool", "selector_key"),
    (("akb_get", "version"), ("akb_history", "commit")),
)
async def test_native_selector_ambiguity_survives_mcp_wire_envelope(
    monkeypatch,
    tmp_path,
    tool: str,
    selector_key: str,
):
    from app.config import settings

    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    from mcp_server import server

    monkeypatch.setattr(server, "revision_backend", _AmbiguousNativeBackend())
    monkeypatch.setattr(server, "split_uri", lambda *_args, **_kwargs: ("v", "doc.md"))

    async def allow_access(*_args, **_kwargs):
        return None

    async def resolve_user():
        return server._MCPUser()

    monkeypatch.setattr(server, "check_vault_access", allow_access)
    monkeypatch.setattr(server, "_get_user", resolve_user)
    monkeypatch.setattr(server.audit_log, "record_tool", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server.tool_usage, "record", lambda *_args, **_kwargs: None)

    response = await server.call_tool(
        tool,
        {"uri": "akb://v/doc/doc.md", selector_key: "abcdef0", **({"mode": "diff"} if tool == "akb_history" else {})},
    )

    envelope = json.loads(response.content[0].text)
    assert envelope["code"] == "native_revision_selector_ambiguous"
    assert envelope["code"] != "conflict"


@pytest.mark.parametrize("arguments", [
    {"mode": "list", "commit": "abcdef0"},
    {"mode": "diff"},
    {"mode": "diff", "commit": "abcdef0", "limit": 1},
    {"mode": "diff", "commit": "ABCDEF0"},
    {"mode": "other"},
    {"mode": "diff", "commit": 1234567},
    {"mode": "list", "limit": "2"},
])
async def test_history_rejects_invalid_arguments(monkeypatch, tmp_path, arguments):
    from app.config import settings

    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    from mcp_server import server

    async def resolve_user():
        return server._MCPUser()

    async def allow_access(*_args, **_kwargs):
        return None

    monkeypatch.setattr(server, "_get_user", resolve_user)
    monkeypatch.setattr(server, "check_vault_access", allow_access)
    monkeypatch.setattr(server.audit_log, "record_tool", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server.tool_usage, "record", lambda *_args, **_kwargs: None)
    result = await server.call_tool("akb_history", {"uri": "akb://v/doc/doc.md", **arguments})
    assert result.is_error
    assert json.loads(result.content[0].text)["code"] == "invalid_argument"


async def test_history_list_and_diff_dispatch_same_uri_and_commit(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    from app.config import settings

    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    from mcp_server import server

    backend = type("Revision", (), {})()
    backend.document_history = AsyncMock(return_value={"uri": "akb://v/doc/doc.md", "history": [{"hash": "abcdef0"}]})
    backend.document_diff = AsyncMock(return_value={"file": "doc.md", "commit": "abcdef0", "diff": "+new\n-old"})
    access = AsyncMock()
    monkeypatch.setattr(server, "revision_backend", backend)
    monkeypatch.setattr(server, "check_vault_access", access)
    user = server._MCPUser()
    listed = await server._handle_history({"uri": "akb://v/doc/doc.md", "limit": 3}, "uid", user)
    diff = await server._handle_history({"uri": "akb://v/doc/doc.md", "mode": "diff", "commit": "abcdef0"}, "uid", user)
    assert listed["history"] == [{"hash": "abcdef0"}]
    assert diff["commit"] == "abcdef0" and diff["diff"] == "+new\n-old"
    backend.document_history.assert_awaited_once_with("v", "doc.md", limit=3)
    backend.document_diff.assert_awaited_once_with("v", "doc.md", "abcdef0")
    assert access.await_count == 2
    assert all(call.kwargs == {"required_role": "reader"} for call in access.await_args_list)


@pytest.mark.parametrize("result", [
    {"file": "doc.md", "commit": "abcdef0", "type": "unchanged", "diff": ""},
    {"file": "doc.md", "commit": "abcdef0", "type": "unknown", "diff": "", "error": "commit not found"},
])
async def test_history_diff_rejects_commit_outside_document(monkeypatch, tmp_path, result):
    from unittest.mock import AsyncMock
    from app.config import settings

    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    from mcp_server import server

    backend = type("Revision", (), {})()
    backend.document_diff = AsyncMock(return_value=result)
    monkeypatch.setattr(server, "revision_backend", backend)
    monkeypatch.setattr(server, "check_vault_access", AsyncMock())
    output = await server._handle_history({"uri": "akb://v/doc/doc.md", "mode": "diff", "commit": "abcdef0"}, "uid", server._MCPUser())
    assert output.get("code") == "invalid_argument"


async def test_history_checks_access_before_examining_commit(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    from app.config import settings
    from app.exceptions import ForbiddenError

    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    from mcp_server import server

    monkeypatch.setattr(server, "check_vault_access", AsyncMock(side_effect=ForbiddenError("denied")))
    with pytest.raises(ForbiddenError):
        await server._handle_history({"uri": "akb://v/doc/doc.md", "mode": "diff", "commit": "bad"}, "uid", server._MCPUser())


@pytest.mark.parametrize("limit", ["2", True, 2.0, None])
async def test_history_list_rejects_non_integer_limit_before_backend(monkeypatch, tmp_path, limit):
    from unittest.mock import AsyncMock
    from app.config import settings

    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    from mcp_server import server

    backend = type("Revision", (), {})()
    backend.document_history = AsyncMock(return_value={"uri": "akb://v/doc/doc.md", "history": []})
    monkeypatch.setattr(server, "revision_backend", backend)
    monkeypatch.setattr(server, "check_vault_access", AsyncMock())
    monkeypatch.setattr(server, "_get_user", AsyncMock(return_value=server._MCPUser()))
    monkeypatch.setattr(server.audit_log, "record_tool", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server.tool_usage, "record", lambda *_args, **_kwargs: None)
    result = await server.call_tool("akb_history", {"uri": "akb://v/doc/doc.md", "mode": "list", "limit": limit})
    assert result.is_error
    assert json.loads(result.content[0].text)["code"] == "invalid_argument"
    backend.document_history.assert_not_awaited()


@pytest.mark.parametrize(("arguments", "expected"), [({}, 20), ({"limit": 3}, 3), ({"limit": 0}, 0), ({"limit": -1}, -1)])
async def test_history_list_passes_integer_limit_unchanged(monkeypatch, tmp_path, arguments, expected):
    from unittest.mock import AsyncMock
    from app.config import settings

    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    from mcp_server import server

    backend = type("Revision", (), {})()
    backend.document_history = AsyncMock(return_value={"uri": "akb://v/doc/doc.md", "history": []})
    monkeypatch.setattr(server, "revision_backend", backend)
    monkeypatch.setattr(server, "check_vault_access", AsyncMock())
    monkeypatch.setattr(server, "_get_user", AsyncMock(return_value=server._MCPUser()))
    monkeypatch.setattr(server.audit_log, "record_tool", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server.tool_usage, "record", lambda *_args, **_kwargs: None)
    result = await server.call_tool("akb_history", {"uri": "akb://v/doc/doc.md", **arguments})
    assert not result.is_error
    backend.document_history.assert_awaited_once_with("v", "doc.md", limit=expected)
