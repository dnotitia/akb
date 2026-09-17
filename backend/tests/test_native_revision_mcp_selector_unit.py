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
    (("akb_get", "version"), ("akb_diff", "commit")),
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
        {"uri": "akb://v/doc/doc.md", selector_key: "abcdef0"},
    )

    envelope = json.loads(response.content[0].text)
    assert envelope["code"] == "native_revision_selector_ambiguous"
    assert envelope["code"] != "conflict"
