"""PostgreSQL proof for the event tail LISTEN hub's own connection.

The hub must open its connection from the configured DSN rather than borrow
one from the request pool, wake on a committed event, and recover when the
server drops that connection.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from urllib.parse import unquote, urlsplit

import pytest

from app.config import settings
from app.services.event_tail_hub import EventTailHub
from tests.test_event_tail_postgres import _database_dsn, _fresh_database, _insert_event, _make_vault


pytestmark = pytest.mark.asyncio

_LISTENER_APPLICATION_NAME = "akb-event-tail-listener"


async def _until(predicate: Callable[[], bool], *, timeout: float = 10.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout)


def _point_settings_at(monkeypatch, dsn: str) -> None:
    parts = urlsplit(dsn)
    monkeypatch.setattr(settings, "db_host", parts.hostname)
    monkeypatch.setattr(settings, "db_port", parts.port or 5432)
    monkeypatch.setattr(settings, "db_user", unquote(parts.username or ""))
    monkeypatch.setattr(settings, "db_password", unquote(parts.password or ""))
    monkeypatch.setattr(settings, "db_name", parts.path.lstrip("/"))


async def _listener_pids(pool, database: str) -> list[int]:
    rows = await pool.fetch(
        "SELECT pid FROM pg_stat_activity WHERE application_name = $1 AND datname = $2",
        _LISTENER_APPLICATION_NAME,
        database,
    )
    return [row["pid"] for row in rows]


async def test_hub_listens_on_its_own_connection_and_recovers_after_the_server_drops_it(monkeypatch):
    async with _fresh_database() as pool:
        database = await pool.fetchval("SELECT current_database()")
        _point_settings_at(monkeypatch, _database_dsn(database))
        vault_id = await _make_vault(pool, "event-hub")
        pool_size = pool.get_size()
        hub = EventTailHub(reconnect_initial_delay=0.05, close_timeout=2.0)
        try:
            subscription = hub.subscribe()
            await _until(lambda: hub.connected)
            pids = await _listener_pids(pool, database)
            assert len(pids) == 1
            # The listener is not a request-pool connection.
            assert pool.get_size() == pool_size

            await asyncio.sleep(0.05)
            subscription.wakeup.clear()
            await _insert_event(pool, vault_id, "document.put", "alice")
            await asyncio.wait_for(subscription.wakeup.wait(), 5)

            subscription.wakeup.clear()
            assert await pool.fetchval("SELECT pg_terminate_backend($1)", pids[0])
            # Losing the connection wakes the stream so it re-reads by cursor.
            await asyncio.wait_for(subscription.wakeup.wait(), 5)
            await _until(lambda: hub.connected)
            replacement = await _listener_pids(pool, database)
            assert len(replacement) == 1
            assert replacement != pids

            await asyncio.sleep(0.05)
            subscription.wakeup.clear()
            await _insert_event(pool, vault_id, "document.update", "alice")
            await asyncio.wait_for(subscription.wakeup.wait(), 5)
        finally:
            await hub.close()

        assert not hub.connected
        for _ in range(100):
            if not await _listener_pids(pool, database):
                break
            await asyncio.sleep(0.05)
        assert await _listener_pids(pool, database) == []
