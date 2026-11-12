"""Real-PostgreSQL proof that a saturated request pool does not fail readiness.

A client burst can check out every connection in the request pool on every
replica at once. Readiness used to queue its `SELECT 1` behind that, time out
after two seconds and answer 503 on every replica together, which left the
Service with no endpoints while the database was fine. Here the pool is
really saturated, the probe connection is really terminated, and the
database really disappears.

Each test owns a disposable database. Set AKB_TEST_DSN to a test-cluster
admin DSN; with REQUIRE_REAL_PG=1 an unreachable server is a failure.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
import uuid
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

import asyncpg
import httpx
import pytest

from app.config import Settings
from app.db import postgres

pytestmark = pytest.mark.asyncio

_ADMIN_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",  # pragma: allowlist secret
)
_POOL_SIZE = 2


async def _can_connect() -> bool:
    try:
        conn = await asyncpg.connect(_ADMIN_DSN, timeout=2)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


def _settings(name: str) -> Settings:
    parsed = urlsplit(_ADMIN_DSN)
    return Settings(
        document_revision_backend="bare_git",
        db_host=parsed.hostname or "localhost",
        db_port=parsed.port or 5432,
        db_name=name,
        db_user=unquote(parsed.username or "akb"),
        db_password=unquote(parsed.password or ""),
    )


class _SharedPoolVectorStore:
    """Health-checks like pgvector does on the main database: through the pool."""

    def __init__(self) -> None:
        self.calls = 0

    async def health(self) -> bool:
        self.calls += 1
        pool = await postgres.get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval("SELECT 1") == 1


@pytest.fixture
async def ready_db(monkeypatch, tmp_path):
    if not await _can_connect():
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"Required PostgreSQL is not reachable at {_ADMIN_DSN}")
        pytest.skip(f"PostgreSQL is not reachable at {_ADMIN_DSN}")

    from app.config import settings as process_settings

    monkeypatch.setattr(process_settings, "git_storage_path", str(tmp_path / "vaults"))
    from app import main

    admin = await asyncpg.connect(_ADMIN_DSN)
    name = f"akb_readyz_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    configured = _settings(name)
    pool = await asyncpg.create_pool(dsn=configured.asyncpg_dsn, min_size=_POOL_SIZE, max_size=_POOL_SIZE)
    store = _SharedPoolVectorStore()
    monkeypatch.setattr(postgres, "settings", configured)
    monkeypatch.setattr(postgres, "_pool", pool)
    monkeypatch.setattr(main, "_ready_state", main._ReadyState())
    monkeypatch.setattr(main, "_ready_lock", asyncio.Lock())
    monkeypatch.setattr(main, "get_vector_store", lambda: store)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://akb.test")
    try:
        yield SimpleNamespace(main=main, admin=admin, name=name, pool=pool, store=store, client=client)
    finally:
        await client.aclose()
        await postgres.close_readiness_connection()
        pool.terminate()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


@contextlib.asynccontextmanager
async def _every_connection_checked_out(pool: asyncpg.Pool):
    """Other tasks hold every connection the pool may open, as a client burst does."""
    release = asyncio.Event()
    holding = 0

    async def hold() -> None:
        nonlocal holding
        async with pool.acquire():
            holding += 1
            await release.wait()

    tasks = [asyncio.create_task(hold()) for _ in range(pool.get_max_size())]
    try:
        async with asyncio.timeout(5):
            while holding < pool.get_max_size():
                await asyncio.sleep(0.01)
        assert pool.get_size() == pool.get_max_size()
        assert pool.get_idle_size() == 0
        yield
    finally:
        release.set()
        await asyncio.gather(*tasks)


async def _probe_backend_pid(db) -> int | None:
    return await db.admin.fetchval(
        "SELECT pid FROM pg_stat_activity WHERE datname = $1 AND application_name = $2",
        db.name,
        postgres.READINESS_APPLICATION_NAME,
    )


async def test_saturated_pool_keeps_readyz_200_within_the_probe_budget(ready_db):
    async with _every_connection_checked_out(ready_db.pool):
        started = time.monotonic()
        response = await ready_db.client.get("/readyz")
        elapsed = time.monotonic() - started

    assert response.status_code == 200, response.text
    assert elapsed < 2.0
    detail = response.json()["detail"]
    assert detail["db"] == "ok"
    assert detail["pool"]["size"] == detail["pool"]["max"] == _POOL_SIZE
    assert detail["pool"]["idle"] == 0
    assert detail["pool"]["saturated_for_secs"] is not None
    # The soft vector check would borrow from the same saturated pool.
    assert detail["vector_store"] == "skipped:pool_saturated"
    assert ready_db.store.calls == 0


async def test_saturation_past_the_grace_answers_503_and_recovers(ready_db, monkeypatch):
    main = ready_db.main
    grace = main.settings.readyz_pool_saturation_grace_secs
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(main, "time", SimpleNamespace(monotonic=lambda: clock.now))

    async with _every_connection_checked_out(ready_db.pool):
        first = await ready_db.client.get("/readyz")
        clock.now += grace - 1
        within = await ready_db.client.get("/readyz")
        clock.now += 2
        past = await ready_db.client.get("/readyz")

    assert first.status_code == 200, first.text
    assert within.status_code == 200, within.text
    assert within.json()["detail"]["pool"]["saturated_for_secs"] == grace - 1
    assert past.status_code == 503, past.text
    body = past.json()["detail"]
    assert body["status"] == "not_ready"
    assert body["detail"]["db"] == "ok"
    assert body["detail"]["pool"]["saturated_for_secs"] == grace + 1

    # Released: the very next probe is ready again (failures are not cached).
    recovered = await ready_db.client.get("/readyz")
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["detail"]["pool"]["saturated_for_secs"] is None
    assert recovered.json()["detail"]["vector_store"] == "ok"


async def test_database_gone_is_not_ready_at_once(ready_db):
    assert (await ready_db.client.get("/readyz")).status_code == 200
    assert await _probe_backend_pid(ready_db) is not None

    await ready_db.admin.execute(f'DROP DATABASE "{ready_db.name}" WITH (FORCE)')
    ready_db.main._ready_state.ts -= 31  # past the success cache

    started = time.monotonic()
    response = await ready_db.client.get("/readyz")
    elapsed = time.monotonic() - started

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["detail"]["db"] == "error:unavailable"
    assert elapsed < 2.0
    assert ready_db.name not in response.text


async def test_terminated_probe_connection_is_reopened_by_the_next_probe(ready_db):
    main = ready_db.main
    assert (await main._probe_ready())[0] is True
    first_pid = await _probe_backend_pid(ready_db)
    assert first_pid is not None
    conn = postgres._readiness_conn

    # As a server restart or an administrator would end it.
    assert await ready_db.admin.fetchval("SELECT pg_terminate_backend($1)", first_pid)
    async with asyncio.timeout(2):
        while not conn.is_closed():
            await asyncio.sleep(0.01)

    ok, detail = await main._probe_ready()
    assert ok is True, detail
    second_pid = await _probe_backend_pid(ready_db)
    assert second_pid is not None and second_pid != first_pid


async def test_probe_connection_is_separate_from_the_pool(ready_db):
    assert (await ready_db.main._probe_ready())[0] is True

    names = await ready_db.admin.fetch(
        "SELECT application_name FROM pg_stat_activity WHERE datname = $1",
        ready_db.name,
    )
    probe_sessions = [row for row in names if row["application_name"] == postgres.READINESS_APPLICATION_NAME]
    assert len(probe_sessions) == 1
    # The pool keeps its own connections; the probe borrowed none of them.
    assert ready_db.pool.get_idle_size() == _POOL_SIZE


async def test_shutdown_closes_the_probe_connection(ready_db, monkeypatch):
    from app.services import lifecycle

    assert (await ready_db.main._probe_ready())[0] is True
    pid = await _probe_backend_pid(ready_db)
    conn = postgres._readiness_conn
    assert pid is not None and conn is not None

    order: list[str] = []
    real_close_probe, real_close_pool = lifecycle.close_readiness_connection, lifecycle.close_pool

    async def close_probe() -> None:
        order.append("readiness_connection")
        await real_close_probe()

    async def close_pool() -> None:
        order.append("pool")
        await real_close_pool()

    monkeypatch.setattr(lifecycle.settings, "keycloak_enabled", False)
    # Another test's HTTP client, if any, is not this test's to close.
    monkeypatch.setattr(lifecycle.http_pool, "close_client", lambda: asyncio.sleep(0))
    monkeypatch.setattr(lifecycle, "close_readiness_connection", close_probe)
    monkeypatch.setattr(lifecycle, "close_pool", close_pool)

    await lifecycle.shutdown_storage()

    assert order == ["readiness_connection", "pool"]
    assert conn.is_closed()
    assert postgres._readiness_conn is None
    async with asyncio.timeout(2):
        while await _probe_backend_pid(ready_db) is not None:
            await asyncio.sleep(0.01)
