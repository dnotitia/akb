"""Exercise transport details the SDK client intentionally hides."""

import os
import ssl

import httpx
import pytest

LEGACY_VERSION = "2025-03-26"
MODERN_VERSION = "2026-07-28"
SESSION_HEADER = "MCP-Session-Id"
CLIENT_INFO = {"name": "wire-boundary-test", "version": "1"}


@pytest.fixture
def wire_client(request: pytest.FixtureRequest):
    if request.config.getoption("--runtime-descriptor"):
        runtime = request.getfixturevalue("runtime_session")
        url, pat = runtime.descriptor.mcp_url, runtime.pat
        verify = True
    else:
        origin = os.environ.get("AKB_URL")
        pat = os.environ.get("AKB_E2E_PAT")
        if not origin or not pat:
            pytest.fail("Pass --runtime-descriptor or set AKB_URL and AKB_E2E_PAT for a disposable endpoint")
        url = f"{origin.rstrip('/')}/mcp/"
        ca_file = os.environ.get("AKB_E2E_CA_FILE")
        verify = ssl.create_default_context(cafile=ca_file) if ca_file else True

    with httpx.Client(
        base_url=url,
        headers={
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {pat}",
        },
        limits=httpx.Limits(max_keepalive_connections=0),
        timeout=10,
        trust_env=False,
        verify=verify,
    ) as client:
        yield client


def _result(response: httpx.Response, request_id: int) -> dict:
    assert response.status_code == 200, response.text
    assert SESSION_HEADER not in response.headers
    body = response.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == request_id
    return body["result"]


def test_legacy_initialize_and_stateless_calls(wire_client: httpx.Client) -> None:
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": LEGACY_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        },
    }
    unauthenticated = wire_client.build_request("POST", "", json=initialize)
    del unauthenticated.headers["Authorization"]
    rejected = wire_client.send(unauthenticated)
    assert rejected.status_code == 401, rejected.text

    result = _result(wire_client.post("", json=initialize), request_id=1)
    assert result["protocolVersion"] == LEGACY_VERSION

    notification = wire_client.post(
        "", json={"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    assert notification.status_code == 202, notification.text
    assert SESSION_HEADER not in notification.headers

    tools = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    catalog = _result(wire_client.post("", json=tools), request_id=2)
    assert len(catalog["tools"]) >= 22

    # Each request uses a fresh connection, with no session header or new handshake.
    for request_id in range(3, 15):
        response = wire_client.post(
            "",
            headers={"MCP-Protocol-Version": LEGACY_VERSION},
            json={**tools, "id": request_id},
        )
        listed = _result(response, request_id=request_id)
        assert listed["tools"] == catalog["tools"]

    no_stream = wire_client.get("")
    assert no_stream.status_code == 404, no_stream.text
    assert no_stream.json() == {"error": "Invalid session"}

    closed = wire_client.delete("")
    assert closed.status_code == 200, closed.text
    assert SESSION_HEADER not in closed.headers
    assert closed.json() == {"terminated": True}


def test_modern_discovery_and_tools_list(wire_client: httpx.Client) -> None:
    wire_client.headers["MCP-Protocol-Version"] = MODERN_VERSION
    params = {"_meta": {
        "io.modelcontextprotocol/protocolVersion": MODERN_VERSION,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
    }}
    discover = wire_client.post(
        "",
        headers={"MCP-Method": "server/discover"},
        json={"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": params},
    )
    assert _result(discover, request_id=1)["supportedVersions"] == [MODERN_VERSION]

    catalog = wire_client.post(
        "",
        headers={"MCP-Method": "tools/list"},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": params},
    )
    result = _result(catalog, request_id=2)
    assert result["resultType"] == "complete"
    assert len(result["tools"]) >= 22
