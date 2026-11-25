"""Check authenticated MCP SQL write policy over the application's ASGI routes."""

from contextlib import asynccontextmanager
from functools import partial
from secrets import token_urlsafe

import httpx
import pytest

from ..helpers.sql_write_policy import (
    assert_managed_sql_token_grant_and_revoke,
    assert_ordinary_archived_and_revoked_sql,
)
from .helpers import register_user
from .mcp_client import mcp_session

pytestmark = pytest.mark.live_services


@pytest.fixture
async def operator(api, pg_pool):
    password = token_urlsafe(24)
    actor = await register_user(api, label="sql-owner", password=password)
    # Match the isolated runtime's seeded system operator.
    await pg_pool.execute("UPDATE users SET is_admin = TRUE WHERE id = $1", actor.id)
    return actor, password


@asynccontextmanager
async def _admin_session(api, asgi_app, username, password):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi_app), base_url=api.base_url) as admin:
        login = await admin.post("/api/v1/auth/login", json={"username": username, "password": password})
        login.raise_for_status()
        admin.headers["Authorization"] = "Bearer " + login.json()["token"]
        yield admin


async def test_ordinary_archived_and_revoked_sql(api, pg_pool, asgi_app, operator):
    actor, _ = operator
    member = await register_user(api, label="sql-member")
    async with mcp_session(asgi_app, actor.pat) as owner:
        await assert_ordinary_archived_and_revoked_sql(
            owner, pg_pool, partial(mcp_session, asgi_app), member.username, member.pat,
        )


async def test_managed_sql_token_grant_and_revoke(api, pg_pool, asgi_app, operator):
    actor, password = operator
    member = await register_user(api, label="sql-member")
    async with mcp_session(asgi_app, actor.pat) as owner:
        await assert_managed_sql_token_grant_and_revoke(
            owner, pg_pool, partial(mcp_session, asgi_app), member.username, member.pat,
            partial(_admin_session, api, asgi_app, actor.username, password),
        )
