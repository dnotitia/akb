"""Endpoint-level MCP protocol matrix checks.

These tests exercise the mounted authenticated ASGI surface rather than
calling the SDK transport or the tool dispatcher directly. The business
operation is ``tools/list`` so the matrix stays database-free while still
proving routing, session headers, response eras, and fail-closed boundaries.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from app.config import settings
from app.services.auth_service import AuthenticatedUser
from mcp_server import http_app
from mcp_server.http_app import MCPApp


LEGACY_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")


def _user(username: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=str(uuid.uuid4()),
        username=username,
        email=f"{username}@example.invalid",
        display_name=username,
        is_admin=True,
        auth_method="pat",
    )


def _modern_meta(name: str = "protocol-test") -> dict:
    return {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": name, "version": "1"},
    }


def _headers(token: str = "token") -> dict[str, str]:
    return {
        "authorization": f"Bearer {token}",
        "content-type": "application/json",
        "accept": "application/json",
    }


async def _post(client: httpx.AsyncClient, body: dict, *, token: str = "token", **extra: str):
    headers = _headers(token)
    headers.update(extra)
    return await client.post("/mcp/", headers=headers, json=body)


@pytest.mark.asyncio
async def test_modern_and_all_legacy_revisions_share_one_authenticated_endpoint(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    alice = _user("alice")
    monkeypatch.setattr(http_app, "resolve_mcp_authorization", lambda _header: _resolved(alice))

    app = MCPApp()
    async with app.run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            meta = _modern_meta()
            modern_headers = {
                "mcp-protocol-version": "2026-07-28",
                "mcp-method": "server/discover",
            }
            response = await _post(
                client,
                {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": meta}},
                **modern_headers,
            )
            assert response.status_code == 200
            assert response.headers.get("mcp-session-id") is None
            assert response.json()["result"]["supportedVersions"] == ["2026-07-28"]

            response = await _post(
                client,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": meta}},
                **{**modern_headers, "mcp-method": "tools/list"},
            )
            assert response.status_code == 200
            assert response.headers.get("mcp-session-id") is None
            assert response.json()["result"]["resultType"] == "complete"

            for index, revision in enumerate(LEGACY_VERSIONS, start=10):
                response = await _post(
                    client,
                    {
                        "jsonrpc": "2.0",
                        "id": index,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": revision,
                            "capabilities": {},
                            "clientInfo": {"name": "legacy-test", "version": "1"},
                        },
                    },
                )
                assert response.status_code == 200
                session_id = response.headers.get("mcp-session-id")
                assert session_id
                assert response.json()["result"]["protocolVersion"] == revision

                response = await _post(
                    client,
                    {"jsonrpc": "2.0", "id": index + 100, "method": "tools/list", "params": {}},
                    **{"mcp-session-id": session_id},
                )
                assert response.status_code == 200
                assert response.json()["result"].get("resultType") is None
                assert response.json()["result"].get("_meta") is None


@pytest.mark.asyncio
async def test_protocol_conflicts_fail_before_dispatch_or_session_creation(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    alice = _user("alice")
    monkeypatch.setattr(http_app, "resolve_mcp_authorization", lambda _header: _resolved(alice))

    app = MCPApp()
    async with app.run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            meta = _modern_meta()
            modern_call = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "akb_help", "arguments": {}, "_meta": meta},
            }
            base_headers = {
                "mcp-protocol-version": "2026-07-28",
                "mcp-method": "tools/call",
                "mcp-name": "akb_help",
            }

            response = await _post(
                client,
                modern_call,
                **{**base_headers, "mcp-protocol-version": "2025-06-18"},
            )
            assert response.status_code == 400
            assert response.json()["error"]["code"] == -32020
            assert response.headers.get("mcp-session-id") is None

            response = await _post(
                client,
                {**modern_call, "id": 5, "method": "initialize", "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "legacy", "version": "1"},
                }},
                **{"mcp-protocol-version": "2026-07-28", "mcp-method": "initialize"},
            )
            assert response.status_code == 400
            assert response.json()["error"]["code"] == -32022

            response = await _post(
                client,
                modern_call,
                **{**base_headers, "mcp-name": "akb_search"},
            )
            assert response.status_code == 400
            assert response.json()["error"]["code"] == -32020

            unsupported_meta = {**meta, "io.modelcontextprotocol/protocolVersion": "2099-01-01"}
            response = await _post(
                client,
                {**modern_call, "id": 2, "params": {"name": "akb_help", "arguments": {}, "_meta": unsupported_meta}},
                **{**base_headers, "mcp-protocol-version": "2099-01-01"},
            )
            assert response.status_code == 400
            assert response.json()["error"] == {
                "code": -32022,
                "message": "Unsupported protocol version",
                "data": {"supported": ["2026-07-28"], "requested": "2099-01-01"},
            }

            initialize = {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "legacy", "version": "1"},
                },
            }
            response = await _post(client, initialize)
            session_id = response.headers["mcp-session-id"]
            response = await _post(
                client,
                {**modern_call, "id": 4},
                **{**base_headers, "mcp-session-id": session_id},
            )
            assert response.status_code == 400
            assert response.json()["error"]["code"] == -32600

            for method in ("GET", "DELETE"):
                response = await client.request(
                    method,
                    "/mcp/",
                    headers=_headers(),
                )
                assert response.status_code == 404
                assert response.json() == {"error": "Invalid session"}


@pytest.mark.asyncio
async def test_legacy_session_is_bound_to_the_initializing_principal(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    users = {"one": _user("alice"), "two": _user("bob")}

    async def resolve(header: str):
        return users[header.rsplit("-", 1)[-1]]

    monkeypatch.setattr(http_app, "resolve_mcp_authorization", resolve)
    app = MCPApp()
    async with app.run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await _post(
                client,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "legacy", "version": "1"},
                    },
                },
                token="token-one",
            )
            session_id = response.headers["mcp-session-id"]

            response = await _post(
                client,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                token="token-two",
                **{"mcp-session-id": session_id},
            )
            assert response.status_code == 404
            assert response.json()["error"] == {"code": -32600, "message": "Session not found"}


@pytest.mark.asyncio
async def test_legacy_delete_preserves_success_shape_and_invalidates_session(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    alice = _user("alice")
    monkeypatch.setattr(http_app, "resolve_mcp_authorization", lambda _header: _resolved(alice))

    app = MCPApp()
    async with app.run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await _post(
                client,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "legacy", "version": "1"},
                    },
                },
            )
            session_id = response.headers["mcp-session-id"]

            response = await client.request(
                "DELETE",
                "/mcp/",
                headers={**_headers(), "mcp-session-id": session_id},
            )
            assert response.status_code == 200
            assert response.json() == {"terminated": True}
            assert response.headers.get("mcp-session-id") is None

            response = await _post(
                client,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                **{"mcp-session-id": session_id},
            )
            assert response.status_code == 404
            assert response.json()["error"]["message"] == "Not Found: Session has been terminated"


@pytest.mark.asyncio
async def test_shared_tool_core_audits_generation_revision_and_auth_method(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    alice = _user("alice")
    monkeypatch.setattr(http_app, "resolve_mcp_authorization", lambda _header: _resolved(alice))
    from mcp_server import server as server_module

    audit_records: list[dict] = []
    monkeypatch.setattr(
        server_module.audit_log,
        "record_tool",
        lambda _name, _args, _user, _result, **kwargs: audit_records.append(kwargs["protocol"]),
    )
    monkeypatch.setattr(server_module.tool_usage, "record", lambda *_args, **_kwargs: None)

    app = MCPApp()
    async with app.run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            meta = _modern_meta()
            response = await _post(
                client,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "akb_help", "arguments": {}, "_meta": meta},
                },
                **{
                    "mcp-protocol-version": "2026-07-28",
                    "mcp-method": "tools/call",
                    "mcp-name": "akb_help",
                },
            )
            assert response.status_code == 200

            response = await _post(
                client,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "legacy", "version": "1"},
                    },
                },
            )
            session_id = response.headers["mcp-session-id"]
            response = await _post(
                client,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "akb_help", "arguments": {}},
                },
                **{"mcp-session-id": session_id},
            )
            assert response.status_code == 200

    assert audit_records == [
        {"protocol_generation": "modern", "protocol_revision": "2026-07-28", "auth_method": "pat"},
        {"protocol_generation": "legacy", "protocol_revision": "2025-06-18", "auth_method": "pat"},
    ]


async def _resolved(user: AuthenticatedUser) -> AuthenticatedUser:
    return user
