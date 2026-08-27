"""Focused contract checks for the dual-generation MCP surface."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import subprocess
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from app.config import settings

settings.git_storage_path = tempfile.mkdtemp(prefix="akb-mcp-compatibility-vaults-")

from mcp_server import http_app  # noqa: E402
from mcp_server import server as server_mod  # noqa: E402
from mcp_server.protocol import (  # noqa: E402
    MCP_LEGACY_PROTOCOL_VERSIONS,
    MCP_MODERN_PROTOCOL_VERSION,
)


def test_public_protocol_matrix_is_explicit_and_ordered():
    assert MCP_MODERN_PROTOCOL_VERSION == "2026-07-28"
    assert MCP_LEGACY_PROTOCOL_VERSIONS == (
        "2024-11-05",
        "2025-03-26",
        "2025-06-18",
        "2025-11-25",
    )


def test_legacy_usage_correlation_does_not_store_the_raw_session_id(monkeypatch):
    raw_session_id = "opaque-session-from-wire"
    monkeypatch.setattr(server_mod, "_session_id", lambda: raw_session_id)

    stored = server_mod._usage_session_id({
        "protocol_generation": "legacy",
        "protocol_revision": "2025-06-18",
        "auth_method": "pat",
    })

    assert stored is not None
    assert raw_session_id not in stored
    assert stored.startswith("legacy:2025-06-18:pat:")


def test_protocol_release_metadata_is_aligned():
    root = Path(__file__).resolve().parents[2]
    pyproject = (root / "backend" / "pyproject.toml").read_text()
    package = json.loads((root / "packages" / "akb-mcp-client" / "package.json").read_text())
    registry = json.loads((root / "server.json").read_text())
    proxy_source = (root / "packages" / "akb-mcp-client" / "lib" / "proxy.mjs").read_text()

    assert '"mcp[cli]==2.1.0"' in pyproject
    assert re.search(r'^version\s*=\s*"0\.15\.0"$', pyproject, re.MULTILINE)
    assert package["version"] == "2.3.0"
    assert registry["version"] == package["version"]
    assert registry["packages"][0]["version"] == package["version"]
    assert f'const PROXY_VERSION = "{package["version"]}";' in proxy_source


def test_modern_shell_helper_emits_valid_default_params():
    root = Path(__file__).resolve().parents[2]
    command = r'''
curl() {
    while [ "$#" -gt 0 ]; do
        if [ "$1" = "-d" ]; then
            printf '%s\n' "$2"
            return 0
        fi
        shift
    done
}
source backend/tests/mcp_modern.sh
mcp_modern_discover fake
'''
    completed = subprocess.run(
        ["bash", "-c", command],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    request = json.loads(completed.stdout)
    assert request["method"] == "server/discover"
    assert request["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"


class _User:
    def __init__(self, user_id: str, username: str):
        self.user_id = user_id
        self.username = username
        self.email = f"{username}@example.test"
        self.display_name = username
        self.is_admin = True
        self.auth_method = "pat"
        self.account_kind = "human"
        self.oauth_scopes = None
        self.token_scopes = None
        self.key_class = "pat"


_USERS = {
    "Bearer alpha": _User("00000000-0000-0000-0000-000000000001", "alpha"),
    "Bearer beta": _User("00000000-0000-0000-0000-000000000002", "beta"),
}


@asynccontextmanager
async def running_mcp(monkeypatch):
    async def resolve(authorization: str):
        return _USERS.get(authorization)

    monkeypatch.setattr(http_app, "resolve_mcp_authorization", resolve)
    monkeypatch.setattr(server_mod, "resolve_mcp_authorization", resolve)
    app = http_app.MCPApp()
    await app.start()
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield app, client
    finally:
        await app.stop()


def _modern_meta(version: str = MCP_MODERN_PROTOCOL_VERSION) -> dict:
    return {
        "io.modelcontextprotocol/protocolVersion": version,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "compatibility-test", "version": "1"},
    }


def _modern_headers(
    method: str,
    *,
    name: str | None = None,
    version: str = MCP_MODERN_PROTOCOL_VERSION,
) -> dict[str, str]:
    headers = {
        "Authorization": "Bearer alpha",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Mcp-Protocol-Version": version,
        "Mcp-Method": method,
    }
    if name is not None:
        headers["Mcp-Name"] = name
    return headers


def _legacy_headers(
    *,
    session_id: str | None = None,
    version: str | None = None,
    authorization: str = "Bearer alpha",
) -> dict[str, str]:
    headers = {
        "Authorization": authorization,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id is not None:
        headers["Mcp-Session-Id"] = session_id
    if version is not None:
        headers["Mcp-Protocol-Version"] = version
    return headers


def _modern_request(
    request_id: int,
    method: str,
    params: dict | None = None,
    *,
    meta: dict | None = None,
) -> dict:
    body = dict(params or {})
    body["_meta"] = meta or _modern_meta()
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": body}


@pytest.mark.asyncio
async def test_modern_discover_and_repeated_requests_are_stateless(monkeypatch):
    async with running_mcp(monkeypatch) as (_, client):
        discover = await client.post(
            "/",
            headers=_modern_headers("server/discover"),
            json=_modern_request(1, "server/discover"),
        )
        assert discover.status_code == 200
        assert discover.headers.get("mcp-session-id") is None
        assert discover.json()["result"]["supportedVersions"] == [MCP_MODERN_PROTOCOL_VERSION]

        for request_id in (2, 3):
            listing = await client.post(
                "/",
                headers=_modern_headers("tools/list"),
                json=_modern_request(request_id, "tools/list"),
            )
            assert listing.status_code == 200
            assert listing.headers.get("mcp-session-id") is None
            assert listing.json()["result"]["resultType"] == "complete"
            assert listing.json()["result"]["cacheScope"] == "public"


@pytest.mark.asyncio
async def test_legacy_initialize_tools_call_get_and_delete_keep_session_contract(monkeypatch):
    async with running_mcp(monkeypatch) as (app, client):
        initialize = await client.post(
            "/",
            headers=_legacy_headers(),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "legacy-test", "version": "1"},
                },
            },
        )
        assert initialize.status_code == 200
        session_id = initialize.headers.get("mcp-session-id")
        assert session_id
        assert initialize.json()["result"]["protocolVersion"] == "2025-06-18"

        initialized = await client.post(
            "/",
            headers=_legacy_headers(session_id=session_id, version="2025-06-18"),
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert initialized.status_code == 202

        listing = await client.post(
            "/",
            headers=_legacy_headers(session_id=session_id, version="2025-06-18"),
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert listing.status_code == 200
        assert "tools" in listing.json()["result"]
        assert listing.json()["result"].get("resultType") is None
        assert listing.json()["result"].get("cacheScope") is None

        call = await client.post(
            "/",
            headers=_legacy_headers(session_id=session_id, version="2025-06-18"),
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "akb_help", "arguments": {"topic": "quickstart"}},
            },
        )
        assert call.status_code == 200
        assert call.json()["result"]["content"]

        events: list[dict] = []
        started = asyncio.Event()
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [
                (key.lower().encode(), value.encode())
                for key, value in _legacy_headers(session_id=session_id, version="2025-06-18").items()
            ],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "root_path": "",
        }

        async def receive():
            await asyncio.sleep(0)
            return {"type": "http.disconnect"}

        async def send(message):
            events.append(message)
            if message["type"] == "http.response.start":
                started.set()

        get_task = asyncio.create_task(app(scope, receive, send))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
        finally:
            get_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await get_task
        response_start = next(message for message in events if message["type"] == "http.response.start")
        assert response_start["status"] == 200

        terminated = await client.delete(
            "/",
            headers=_legacy_headers(session_id=session_id, version="2025-06-18"),
        )
        assert terminated.status_code == 200
        assert terminated.json() == {"terminated": True}
        after_delete = await client.post(
            "/",
            headers=_legacy_headers(session_id=session_id, version="2025-06-18"),
            json={"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
        )
        assert after_delete.status_code == 404


@pytest.mark.asyncio
async def test_every_allowlisted_http_legacy_revision_negotiates(monkeypatch):
    async with running_mcp(monkeypatch) as (_, client):
        for request_id, revision in enumerate(MCP_LEGACY_PROTOCOL_VERSIONS, start=50):
            initialize = await client.post(
                "/",
                headers=_legacy_headers(),
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": revision,
                        "capabilities": {},
                        "clientInfo": {"name": "legacy-test", "version": "1"},
                    },
                },
            )
            assert initialize.status_code == 200
            assert initialize.json()["result"]["protocolVersion"] == revision
            session_id = initialize.headers["mcp-session-id"]
            terminated = await client.delete(
                "/",
                headers=_legacy_headers(session_id=session_id, version=revision),
            )
            assert terminated.status_code == 200
            assert terminated.json() == {"terminated": True}


@pytest.mark.asyncio
async def test_legacy_session_is_bound_to_the_authenticated_account(monkeypatch):
    async with running_mcp(monkeypatch) as (_, client):
        initialize = await client.post(
            "/",
            headers=_legacy_headers(),
            json={
                "jsonrpc": "2.0",
                "id": 20,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "legacy-test", "version": "1"},
                },
            },
        )
        session_id = initialize.headers["mcp-session-id"]
        mismatch = await client.post(
            "/",
            headers=_legacy_headers(
                session_id=session_id,
                version="2025-06-18",
                authorization="Bearer beta",
            ),
            json={"jsonrpc": "2.0", "id": 21, "method": "tools/list", "params": {}},
        )
        assert mismatch.status_code == 404
        assert "alpha" not in mismatch.text
        assert "beta" not in mismatch.text


@pytest.mark.asyncio
async def test_legacy_session_revision_cannot_change_after_initialize(monkeypatch):
    async with running_mcp(monkeypatch) as (_, client):
        initialize = await client.post(
            "/",
            headers=_legacy_headers(),
            json={
                "jsonrpc": "2.0",
                "id": 22,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "legacy-test", "version": "1"},
                },
            },
        )
        session_id = initialize.headers["mcp-session-id"]
        mismatch = await client.post(
            "/",
            headers=_legacy_headers(session_id=session_id, version="2025-06-18"),
            json={"jsonrpc": "2.0", "id": 23, "method": "tools/list", "params": {}},
        )
        assert mismatch.status_code == 400
        assert mismatch.json()["error"]["code"] == -32020


@pytest.mark.asyncio
async def test_protocol_generation_mixing_and_routing_mismatch_fail_closed(monkeypatch):
    async with running_mcp(monkeypatch) as (_, client):
        dispatches = 0
        original_help = server_mod._HANDLERS["akb_help"]

        async def counted_help(*args, **kwargs):
            nonlocal dispatches
            dispatches += 1
            return await original_help(*args, **kwargs)

        monkeypatch.setitem(server_mod._HANDLERS, "akb_help", counted_help)
        modern_with_session = await client.post(
            "/",
            headers={**_modern_headers("tools/list"), "Mcp-Session-Id": "legacy-session"},
            json=_modern_request(30, "tools/list"),
        )
        assert modern_with_session.status_code == 400
        assert modern_with_session.json()["error"]["code"] == -32600

        legacy_with_modern_header = await client.post(
            "/",
            headers=_modern_headers("initialize"),
            json={
                "jsonrpc": "2.0",
                "id": 31,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "legacy-test", "version": "1"},
                },
            },
        )
        assert legacy_with_modern_header.status_code == 400
        assert legacy_with_modern_header.json()["error"]["code"] in {-32022, -32602}

        mismatch = await client.post(
            "/",
            headers=_modern_headers("tools/list"),
            json=_modern_request(32, "tools/call", {"name": "akb_help", "arguments": {}}),
        )
        assert mismatch.status_code == 400
        assert mismatch.json()["error"]["code"] != 0

        name_mismatch = await client.post(
            "/",
            headers=_modern_headers("tools/call", name="akb_put"),
            json=_modern_request(33, "tools/call", {"name": "akb_help", "arguments": {}}),
        )
        assert name_mismatch.status_code == 400
        assert name_mismatch.json()["error"]["code"] == -32020

        unsupported = await client.post(
            "/",
            headers=_modern_headers("tools/list", version="2099-01-01"),
            json=_modern_request(34, "tools/list", meta=_modern_meta("2099-01-01")),
        )
        assert unsupported.status_code == 400
        assert unsupported.json()["error"]["code"] == -32022
        assert dispatches == 0


@pytest.mark.asyncio
async def test_backend_auth_failure_is_not_hidden_by_protocol_adapter(monkeypatch):
    async with running_mcp(monkeypatch) as (_, client):
        response = await client.post(
            "/",
            headers={**_modern_headers("server/discover"), "Authorization": "Bearer invalid"},
            json=_modern_request(40, "server/discover"),
        )
        assert response.status_code == 401
        assert "WWW-Authenticate" in response.headers
