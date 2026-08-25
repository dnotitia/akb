"""Source-blind HTTP checks for AKB's single modern MCP revision."""

from __future__ import annotations

import tempfile
import json
import re
from pathlib import Path

import httpx
import pytest

from app.config import settings
from mcp_server.protocol import MCP_PROTOCOL_VERSION, MCP_SUPPORTED_PROTOCOL_VERSIONS

settings.git_storage_path = tempfile.mkdtemp(prefix="akb-mcp-protocol-test-vaults-")


def test_protocol_and_release_metadata_are_aligned():
    root = Path(__file__).resolve().parents[2]
    pyproject = (root / "backend" / "pyproject.toml").read_text()
    package = json.loads((root / "packages" / "akb-mcp-client" / "package.json").read_text())
    registry = json.loads((root / "server.json").read_text())
    proxy_source = (root / "packages" / "akb-mcp-client" / "lib" / "proxy.mjs").read_text()

    assert MCP_PROTOCOL_VERSION == "2026-07-28"
    assert MCP_SUPPORTED_PROTOCOL_VERSIONS == ("2026-07-28",)
    assert '"mcp[cli]==2.1.0"' in pyproject
    assert re.search(r'^version\s*=\s*"0\.15\.0"$', pyproject, re.MULTILINE)
    assert package["version"] == "2.3.0"
    assert registry["version"] == package["version"]
    assert registry["packages"][0]["version"] == package["version"]
    assert f'const PROXY_VERSION = "{package["version"]}";' in proxy_source


class _User:
    username = "protocol-test"
    user_id = "protocol-test"
    is_admin = True
    oauth_scopes = None
    token_scopes = None
    key_class = "pat"


def _params(method: str, *, client_capabilities: dict | None = None) -> dict:
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": client_capabilities or {},
        "io.modelcontextprotocol/clientInfo": {"name": "protocol-test", "version": "1"},
    }
    params: dict = {"_meta": meta}
    if method == "tools/call":
        params.update({"name": "akb_list_vaults", "arguments": {}})
    return params


def _headers(method: str, *, session_id: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": "Bearer akb_protocol_test",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Mcp-Protocol-Version": "2026-07-28",
        "Mcp-Method": method,
    }
    if method == "tools/call":
        headers["Mcp-Name"] = "akb_list_vaults"
    if session_id is not None:
        headers["Mcp-Session-Id"] = session_id
    return headers


async def _post(monkeypatch, *, headers: dict[str, str], body: dict) -> httpx.Response:
    from mcp_server import http_app

    async def resolve(_authorization: str):
        return _User()

    monkeypatch.setattr(http_app, "resolve_mcp_authorization", resolve)
    await http_app.mcp_app.start()
    transport = httpx.ASGITransport(app=http_app.mcp_app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as value:
            return await value.post("/mcp", headers=headers, json=body)
    finally:
        await http_app.mcp_app.stop()


@pytest.mark.asyncio
async def test_server_discover_advertises_single_revision_and_identity(monkeypatch):
    response = await _post(
        monkeypatch,
        headers=_headers("server/discover"),
        body={"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": _params("server/discover")},
    )

    assert response.status_code == 200
    assert response.headers.get("mcp-session-id") is None
    result = response.json()["result"]
    assert result["supportedVersions"] == ["2026-07-28"]
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "akb"
    assert result["ttlMs"] == 300000
    assert result["cacheScope"] == "public"


@pytest.mark.asyncio
async def test_tools_list_is_sorted_and_cacheable(monkeypatch):
    capabilities = {
        "experimental": {"io.dnotitia.akb/vault-skill-preflight": {"version": 2}},
    }
    response = await _post(
        monkeypatch,
        headers=_headers("tools/list"),
        body={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": _params("tools/list", client_capabilities=capabilities),
        },
    )

    assert response.status_code == 200
    result = response.json()["result"]
    names = [tool["name"] for tool in result["tools"]]
    assert names == sorted(names)
    assert result["resultType"] == "complete"
    assert result["ttlMs"] == 300000
    assert result["cacheScope"] == "public"
    tools = {tool["name"]: tool for tool in result["tools"]}
    assert "_vault_skill_ack" in tools["akb_put"]["inputSchema"]["properties"]
    assert "_vault_skill_ack" not in tools["akb_get"]["inputSchema"]["properties"]


@pytest.mark.asyncio
async def test_legacy_initialize_gets_typed_unsupported_version_error(monkeypatch):
    response = await _post(
        monkeypatch,
        headers={
            "Authorization": "Bearer akb_protocol_test",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        body={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "legacy", "version": "1"},
            },
        },
    )

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == -32022
    assert error["data"] == {"supported": ["2026-07-28"], "requested": "2025-11-25"}


@pytest.mark.asyncio
async def test_session_header_is_rejected_on_modern_request(monkeypatch):
    response = await _post(
        monkeypatch,
        headers=_headers("tools/list", session_id="legacy-session"),
        body={"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": _params("tools/list")},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32600


@pytest.mark.asyncio
async def test_routing_header_mismatch_is_rejected_before_dispatch(monkeypatch):
    headers = _headers("tools/list")
    headers["Mcp-Method"] = "tools/call"
    response = await _post(
        monkeypatch,
        headers=headers,
        body={"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": _params("tools/list")},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32020
