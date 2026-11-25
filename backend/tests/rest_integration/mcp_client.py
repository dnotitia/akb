"""Authenticated SDK calls against the real MCP routes over ASGI."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin
from uuid import uuid4

import httpx
import httpx2
import pytest
from mcp import Client, types
from mcp.client.streamable_http import streamable_http_client
from starlette.types import ASGIApp

from app.config import settings

from .helpers import Actor, register_user


@asynccontextmanager
async def mcp_session(asgi_app: ASGIApp, pat: str) -> AsyncIterator[Client]:
    started: asyncio.Future[Client] = asyncio.get_running_loop().create_future()
    stop = asyncio.Event()

    async def run_client() -> None:
        async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {pat}"},
            transport=httpx2.ASGITransport(app=asgi_app),
            trust_env=False,
        ) as http:
            transport = streamable_http_client(urljoin(settings.public_base_url, "/mcp/"), http_client=http)
            async with Client(transport, mode="auto", read_timeout_seconds=30, cache=None) as client:
                started.set_result(client)
                await stop.wait()

    # Pytest resumes async fixtures in a new task during teardown. Keep the
    # SDK's task group in one task from connection through close.
    task = asyncio.create_task(run_client())
    try:
        await asyncio.wait((started, task), return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            task.result()
        yield started.result()
    finally:
        stop.set()
        await task


@pytest.fixture
async def mcp_actor(api: httpx.AsyncClient) -> Actor:
    return await register_user(api, label="mcp-owner")


@pytest.fixture
async def mcp_client(asgi_app: ASGIApp, mcp_actor: Actor) -> AsyncIterator[Client]:
    async with mcp_session(asgi_app, mcp_actor.pat) as client:
        yield client


@dataclass(frozen=True)
class SecondaryMcpSession:
    client: Client
    actor: Actor

    @property
    def username(self) -> str:
        return self.actor.username


@pytest.fixture
async def secondary_mcp_client(api: httpx.AsyncClient, asgi_app: ASGIApp) -> AsyncIterator[SecondaryMcpSession]:
    actor = await register_user(api, label="mcp-member")
    async with mcp_session(asgi_app, actor.pat) as client:
        yield SecondaryMcpSession(client, actor)


async def call_json(
    client: Client, name: str, arguments: dict[str, Any], *, expect_error: bool = False,
) -> dict[str, Any]:
    result = await client.call_tool(name, arguments)
    text = next((item.text for item in result.content if isinstance(item, types.TextContent)), None)
    assert text is not None, f"{name} returned no JSON text"
    public = json.loads(text)
    assert isinstance(public, dict), f"{name} returned a non-object result"
    # Product errors use a JSON envelope independently of the SDK error flag.
    if expect_error:
        assert "error" in public, f"{name} returned no application error"
    else:
        assert not result.is_error, f"{name} returned an SDK error"
    return public


async def create_vault(
    client: Client, prefix: str, *, description: str = "MCP SDK behavior test",
) -> str:
    name = f"mcp-sdk-{prefix}-{uuid4().hex[:10]}"
    result = await call_json(client, "akb_create_vault", {"name": name, "description": description})
    assert result.get("name") == name
    assert isinstance(result.get("vault_id"), str)
    return name
