"""Check SQL write policy through the checkout's real stdio proxy."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

import asyncpg
import httpx
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

from ..helpers.sql_write_policy import (
    assert_managed_sql_token_grant_and_revoke,
    assert_ordinary_archived_and_revoked_sql,
)
from .conftest import _prepare_secondary_user
from .runtime import RuntimeContext


@asynccontextmanager
async def _stdio_session(runtime: RuntimeContext, pat: str):
    entry = Path(__file__).resolve().parents[3] / "packages/akb-mcp-client/bin/akb-mcp.mjs"
    proxy = StdioServerParameters(command="node", args=[str(entry)], env={
        "PATH": os.environ["PATH"], "AKB_MCP_URL": runtime.descriptor.mcp_url, "AKB_PAT": pat,
    })
    async with Client(stdio_client(proxy), mode="auto", cache=None, read_timeout_seconds=30) as client:
        yield client


@asynccontextmanager
async def _admin_session(runtime: RuntimeContext):
    descriptor = runtime.descriptor
    async with httpx.AsyncClient(base_url=descriptor.app_origin, trust_env=False) as admin:
        login = await admin.post(descriptor.login_path, json={
            "username": os.environ[descriptor.username_env], "password": os.environ[descriptor.password_env],
        })
        login.raise_for_status()
        admin.headers["Authorization"] = "Bearer " + login.json()["token"]
        yield admin


@pytest.fixture
async def database(runtime_session):
    conn = await asyncpg.connect(
        host="127.0.0.1", port=int(os.environ["AKB_E2E_POSTGRES_PORT"]),
        user="akb", password="akb", database="akb",  # pragma: allowlist secret
    )
    try:
        yield conn
    finally:
        await conn.close()


async def test_ordinary_archived_and_revoked_sql(mcp_client: Client, runtime_session: RuntimeContext, database):
    username, pat, _ = _prepare_secondary_user(runtime_session)
    await assert_ordinary_archived_and_revoked_sql(
        mcp_client, database, partial(_stdio_session, runtime_session), username, pat,
    )


async def test_managed_sql_token_grant_and_revoke(mcp_client: Client, runtime_session: RuntimeContext, database):
    username, pat, _ = _prepare_secondary_user(runtime_session)
    await assert_managed_sql_token_grant_and_revoke(
        mcp_client, database, partial(_stdio_session, runtime_session), username, pat,
        partial(_admin_session, runtime_session),
    )
