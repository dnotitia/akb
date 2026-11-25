"""Run the API against a fresh PostgreSQL database and private Git directory."""

import asyncio
import os
from collections.abc import AsyncIterator
from contextvars import Context
from pathlib import Path
from secrets import token_urlsafe

import asyncpg
import httpx
import pytest
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import settings
from app.db import postgres
from app.services import role_sync, user_sql_executor
from app.services.role_sync import RoleSync, token_role_name, user_role_name, vault_group_role_name
from app.services.user_sql_executor import UserSqlExecutor
from tests.helpers.pg_database import TestDatabase, temporary_database

_BASE_URL = "http://testserver"


async def _database_roles(pool: asyncpg.Pool) -> list[str]:
    user_roles = [user_role_name(row["id"]) for row in await pool.fetch("SELECT id FROM users")]
    token_roles = [
        token_role_name(row["id"])
        for row in await pool.fetch("SELECT id FROM tokens WHERE vault_scope IS NOT NULL")
    ]
    vault_roles = [
        vault_group_role_name(row["id"], scope)
        for row in await pool.fetch("SELECT id FROM vaults")
        for scope in ("reader", "writer", "admin")
    ]
    return token_roles + user_roles + vault_roles


@pytest.fixture
async def pg_pool(
    monkeypatch: pytest.MonkeyPatch, application_schema_template: TestDatabase,
) -> AsyncIterator[asyncpg.Pool]:
    dsn = os.environ.get(
        "AKB_TEST_DSN", "postgresql://akb:akb@localhost:15432/akb",  # pragma: allowlist secret
    )
    roles: list[str] = []
    with monkeypatch.context() as runtime:
        try:
            async with (
                temporary_database(dsn, "akb_rest_", template=application_schema_template) as database,
                asyncpg.create_pool(database.dsn, min_size=1, max_size=8) as pool,
            ):
                runtime.setattr(postgres, "_pool", pool)
                runtime.setattr(role_sync, "_role_sync", RoleSync(pool))
                runtime.setattr(user_sql_executor, "_executor", UserSqlExecutor(pool))
                try:
                    yield pool
                finally:
                    roles = await _database_roles(pool)
        finally:
            # PostgreSQL roles outlive a database. Drop this database's roles
            # after DROP DATABASE has removed their table grants.
            if roles:
                conn = await asyncpg.connect(dsn)
                try:
                    for role in roles:
                        await conn.execute(f'DROP ROLE IF EXISTS "{role}"')
                finally:
                    await conn.close()


@pytest.fixture
async def asgi_app(
    pg_pool: asyncpg.Pool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[ASGIApp]:
    with monkeypatch.context() as runtime:
        runtime.setattr(settings, "auth_mode", "local")
        runtime.setattr(settings, "pat_enabled", True)
        runtime.setattr(settings, "public_base_url", _BASE_URL)
        runtime.setattr(settings, "system_hmac_secret", token_urlsafe(32))
        runtime.setattr(settings, "document_revision_backend", "bare_git")
        runtime.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))

        from app.api.routes import activity, collections, documents, help, knowledge_io
        from app.main import app, mcp_app
        from app.services import publication_service, revision_backend
        from app.services.collection_service import CollectionService
        from app.services.revision_backend import LegacyRevisionBackend
        from mcp_server import server

        # The API and MCP capture their storage services at import time.
        # Bind each entry point to the real Bare Git backend for this test.
        backend = LegacyRevisionBackend()
        runtime.setattr(revision_backend, "_revision_backend", backend)
        runtime.setattr(revision_backend, "_selected_backend", "bare_git")
        runtime.setattr(activity, "revision_backend", backend)
        runtime.setattr(server, "revision_backend", backend)
        for module in (collections, documents, help, knowledge_io, server):
            runtime.setattr(module, "doc_service", backend.document_service)
        runtime.setattr(collections, "collection_service", CollectionService(git=backend.document_service.git))
        runtime.setattr(publication_service, "_doc_service", None)

        async def handle_request(scope: Scope, receive: Receive, send: Send) -> None:
            # Give each request its own context, as the HTTP server does.
            await asyncio.create_task(app(scope, receive, send), context=Context())

        started: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        stop = asyncio.Event()

        async def run_mcp() -> None:
            async with mcp_app.run():
                started.set_result(None)
                await stop.wait()

        # The SDK's task group must enter and exit in the same task. Pytest
        # resumes async-generator fixtures in a new task during teardown.
        mcp_task = asyncio.create_task(run_mcp())
        try:
            await asyncio.wait((started, mcp_task), return_when=asyncio.FIRST_COMPLETED)
            if mcp_task.done():
                mcp_task.result()
            yield handle_request
        finally:
            stop.set()
            await mcp_task


@pytest.fixture
async def api(asgi_app: ASGIApp) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi_app), base_url=_BASE_URL) as client:
        yield client
