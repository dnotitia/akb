"""Shared setup and wire helpers for API authorization tests."""

import json
from typing import Any
from uuid import UUID

import asyncpg
import httpx

MCP_PATH = "/mcp/"
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
MCP_PROTOCOL_VERSION = "2025-03-26"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def make_admin(pg_pool: asyncpg.Pool, user_id: UUID) -> None:
    assert await pg_pool.fetchval(
        "UPDATE users SET is_admin = true WHERE id = $1 RETURNING is_admin", user_id
    ) is True


async def initialize_mcp(api: httpx.AsyncClient, token: str) -> httpx.Response:
    headers = {**MCP_HEADERS, **bearer(token)}
    response = await api.post(
        MCP_PATH,
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "akb-api-tests", "version": "1"},
            },
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["result"]["protocolVersion"] == MCP_PROTOCOL_VERSION
    ready = await api.post(
        MCP_PATH,
        headers={**headers, "MCP-Protocol-Version": MCP_PROTOCOL_VERSION},
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert ready.status_code == 202, ready.text
    return response


async def mcp_call(
    api: httpx.AsyncClient, token: str, tool: str, /, **arguments: Any
) -> dict[str, Any]:
    response = await api.post(
        MCP_PATH,
        headers={**MCP_HEADERS, **bearer(token), "MCP-Protocol-Version": MCP_PROTOCOL_VERSION},
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def mcp_json(
    api: httpx.AsyncClient, token: str, tool: str, /, **arguments: Any
) -> dict[str, Any]:
    reply = await mcp_call(api, token, tool, **arguments)
    assert "result" in reply, reply
    content, = reply["result"]["content"]
    assert content["type"] == "text", content
    return json.loads(content["text"])
