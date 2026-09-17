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
                # No revision mints a session: a stateless transport binds
                # nothing to the replica that answered `initialize`.
                assert response.headers.get("mcp-session-id") is None
                assert response.json()["result"]["protocolVersion"] == revision

                response = await _post(
                    client,
                    {"jsonrpc": "2.0", "id": index + 100, "method": "tools/list", "params": {}},
                )
                assert response.status_code == 200
                assert response.json()["result"].get("resultType") is None
                assert response.json()["result"].get("_meta") is None


@pytest.mark.asyncio
async def test_a_legacy_client_is_served_by_a_replica_that_never_saw_it(monkeypatch, tmp_path):
    """The reason this transport is stateless, stated as a test.

    Two `MCPApp` instances stand in for two replicas: separate processes, so
    separate SDK session managers. A stateful transport keeps its session in
    one manager's memory, which is why a client that initialized against the
    first got `Session not found` from the second for roughly half its calls --
    the deployment runs two API replicas with no session affinity.

    Nothing here can be shared: the manager holds live streams, not data. So
    the assertion is that the client needs nothing from the first replica.
    """
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    alice = _user("alice")
    monkeypatch.setattr(http_app, "resolve_mcp_authorization", lambda _h: _resolved(alice))

    replica_one, replica_two = MCPApp(), MCPApp()
    async with replica_one.run(), replica_two.run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=replica_one), base_url="http://one"
        ) as first:
            handshake = await _post(
                first,
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
            assert handshake.status_code == 200
            # Nothing was handed out that only replica one could honour.
            assert handshake.headers.get("mcp-session-id") is None

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=replica_two), base_url="http://two"
        ) as second:
            for call_id in range(2, 8):
                response = await _post(
                    second,
                    {"jsonrpc": "2.0", "id": call_id, "method": "tools/list", "params": {}},
                    **{"mcp-protocol-version": "2025-06-18"},
                )
                assert response.status_code == 200, response.text
                assert response.json()["result"]["tools"]


@pytest.mark.asyncio
async def test_no_legacy_exchange_mints_or_needs_a_session(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    alice = _user("alice")
    monkeypatch.setattr(http_app, "resolve_mcp_authorization", lambda _header: _resolved(alice))

    app = MCPApp()
    async with app.run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            modern = await _post(
                client,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "server/discover",
                    "params": {"_meta": _modern_meta()},
                },
                **{
                    "mcp-protocol-version": "2026-07-28",
                    "mcp-method": "server/discover",
                },
            )
            assert modern.status_code == 200
            assert modern.headers.get("mcp-session-id") is None

            # A legacy POST used to be refused without a session so the manager
            # could not mint a transport outside `initialize`. Nothing is minted
            # now, so the request is self-contained and simply works -- which is
            # what lets any replica answer it.
            sessionless_legacy = await _post(
                client,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
            assert sessionless_legacy.status_code == 200
            assert sessionless_legacy.json()["result"]["tools"]
            assert sessionless_legacy.headers.get("mcp-session-id") is None

            initialize = await _post(
                client,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "legacy", "version": "1"},
                    },
                },
            )
            assert initialize.status_code == 200
            assert initialize.headers.get("mcp-session-id") is None


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
            assert response.headers.get("mcp-session-id") is None
            # The rule under test is that a modern envelope must not carry a
            # session id at all, so any value exercises it -- there is no longer
            # a minted one to borrow.
            response = await _post(
                client,
                {**modern_call, "id": 4},
                **{**base_headers, "mcp-session-id": "any-session-id"},
            )
            assert response.status_code == 400
            assert response.json()["error"]["code"] == -32600

            # The standalone GET stream only exists for a stateful session, so
            # it stays refused. DELETE is a client saying "I am done": with no
            # session to release, the established success body is the answer.
            response = await client.request("GET", "/mcp/", headers=_headers())
            assert response.status_code == 404
            assert response.json() == {"error": "Invalid session"}

            response = await client.request("DELETE", "/mcp/", headers=_headers())
            assert response.status_code == 200
            assert response.json() == {"terminated": True}


@pytest.mark.asyncio
async def test_each_legacy_request_is_authenticated_on_its_own(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    users = {"one": _user("alice"), "two": _user("bob")}
    seen_principals: list[str] = []

    async def resolve(header: str):
        user = users[header.rsplit("-", 1)[-1]]
        seen_principals.append(user.user_id)
        return user

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
            # A session used to carry the principal that created it, and the
            # SDK refused a second principal presenting it. Nothing carries a
            # principal now: every request resolves its own from its own
            # Authorization header, so a borrowed session id lends no identity
            # -- it is simply ignored, and the caller is served as themselves.
            assert response.headers.get("mcp-session-id") is None

            response = await _post(
                client,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                token="token-two",
                **{"mcp-session-id": "borrowed-session-id"},
            )
            assert response.status_code == 200
            assert seen_principals[-1] == users["two"].user_id


@pytest.mark.asyncio
async def test_legacy_delete_preserves_its_success_shape_with_no_session(monkeypatch, tmp_path):
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
            assert response.headers.get("mcp-session-id") is None

            # The public DELETE contract is unchanged: the established success
            # body, and no session header echoed back. What changed is that
            # there is no state behind it to invalidate, so a client shutting
            # down still gets a clean answer instead of a 404 for a session it
            # was never given.
            response = await client.request("DELETE", "/mcp/", headers=_headers())
            assert response.status_code == 200
            assert response.json() == {"terminated": True}
            assert response.headers.get("mcp-session-id") is None

            response = await _post(
                client,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
            assert response.status_code == 200


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
            assert response.headers.get("mcp-session-id") is None
            # With no session to remember the handshake, the revision a legacy
            # call is audited under is the one it declares on the wire. The MCP
            # spec already requires clients to send this header after
            # initializing; what changed is that it is now the only carrier.
            response = await _post(
                client,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "akb_help", "arguments": {}},
                },
                **{"mcp-protocol-version": "2025-06-18"},
            )
            assert response.status_code == 200

    assert audit_records == [
        {"protocol_generation": "modern", "protocol_revision": "2026-07-28", "auth_method": "pat"},
        {"protocol_generation": "legacy", "protocol_revision": "2025-06-18", "auth_method": "pat"},
    ]


async def _resolved(user: AuthenticatedUser) -> AuthenticatedUser:
    return user
