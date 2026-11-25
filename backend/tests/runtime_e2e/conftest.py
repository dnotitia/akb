"""Fixtures for pytest cases requiring the live repository-owned API."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest

from tests.helpers.required_outcomes import (
    pytest_make_collect_report as pytest_make_collect_report,
    pytest_runtest_makereport as pytest_runtest_makereport,
)

from .fixtures import Actor


@pytest.fixture
def api_origin() -> str:
    origin = os.environ.get("AKB_URL", "")
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        pytest.fail("Set AKB_URL to the HTTP(S) origin of a disposable AKB runtime")
    return origin


@pytest.fixture
async def api(api_origin: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=api_origin,
        timeout=httpx.Timeout(12.0, read=22.0),
        trust_env=False,
    ) as client:
        yield client


@pytest.fixture
def create_actor(api):
    async def create(label: str) -> Actor:
        username = f"{label}-{uuid4().hex[:12]}"
        password = uuid4().hex
        registration = await api.post(
            "/api/v1/auth/register",
            json={"username": username, "password": password, "email": f"{username}@example.test"},
        )
        assert registration.status_code == 200
        login = await api.post("/api/v1/auth/login", json={"username": username, "password": password})
        assert login.status_code == 200
        jwt = login.json()["token"]
        token = await api.post(
            "/api/v1/auth/tokens",
            headers={"Authorization": f"Bearer {jwt}"},
            json={"name": f"{label}-e2e"},
        )
        assert token.status_code == 200
        pat = token.json()["token"]
        assert jwt and pat.startswith("akb_"), "test actor did not receive both auth credentials"
        return Actor(UUID(registration.json()["user_id"]), username, jwt, pat)

    return create


@pytest.fixture
async def create_vault(api):
    vaults = []

    async def create(headers: dict[str, str], label: str) -> str:
        name = f"{label}-{uuid4().hex[:12]}"
        created = await api.post("/api/v1/vaults", params={"name": name}, headers=headers)
        assert created.status_code == 200
        vaults.append((name, headers))
        UUID(created.json()["vault_id"])
        return name

    yield create

    for name, headers in reversed(vaults):
        deleted = await api.delete(f"/api/v1/vaults/{name}", headers=headers)
        assert deleted.status_code in (200, 404)
        if deleted.status_code == 200:
            assert deleted.json()["deleted"] is True


@pytest.fixture
async def pg_connection():
    dsn = os.environ.get("AKB_E2E_DATABASE_URL")
    if not dsn:
        pytest.fail("Set AKB_E2E_DATABASE_URL to the disposable AKB runtime's PostgreSQL DSN")
    try:
        conn = await asyncpg.connect(dsn, timeout=10, command_timeout=10)
    except (OSError, ValueError, asyncpg.PostgresError) as exc:
        pytest.fail(f"Could not connect to runtime PostgreSQL ({type(exc).__name__})")
    try:
        yield conn
    finally:
        await conn.close()
