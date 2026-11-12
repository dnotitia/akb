"""PostgreSQL proof that open event tail streams leave the request pool usable.

Each stream used to keep one pooled connection for its whole life, so a
client that opened a stream per Vault held the entire pool. An ordinary
checkout, the readiness probe's among them, then waited until it timed out
while the database itself was idle.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json

import asyncpg
import pytest

from app.repositories.events_repo import list_vault_events
from app.services import event_tail_service
from app.services.event_tail_service import EventCursorCodec
from tests.test_event_tail_postgres import _database_dsn, _fresh_database, _insert_event, _listening_hub, _make_vault


pytestmark = pytest.mark.asyncio

_POOL_SIZE = 2
_STREAMS = 6
# The readiness probe's whole budget for reaching the database.
_PROBE_BUDGET_SECONDS = 2.0


class _Request:
    async def is_disconnected(self) -> bool:
        return False


async def _until(predicate: Callable[[], bool], *, timeout: float = 10.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout)


async def test_more_open_streams_than_pool_connections_leave_the_pool_usable(monkeypatch):
    async with _fresh_database() as setup_pool:
        database = await setup_pool.fetchval("SELECT current_database()")
        vault_id = await _make_vault(setup_pool, "event-pool")
        pool = await asyncpg.create_pool(_database_dsn(database), min_size=0, max_size=_POOL_SIZE)
        hub = await _listening_hub(setup_pool)
        reads = 0

        async def get_pool() -> asyncpg.Pool:
            return pool

        async def counting_list_vault_events(conn, *args, **kwargs):
            nonlocal reads
            reads += 1
            return await list_vault_events(conn, *args, **kwargs)

        async def check_vault_access(*_args, _conn, **_kwargs) -> dict:
            # The real access check queries the connection the batch was read on.
            await _conn.fetchval("SELECT 1")
            return {"vault_id": vault_id}

        monkeypatch.setattr(event_tail_service, "get_pool", get_pool)
        monkeypatch.setattr(event_tail_service, "get_event_tail_hub", lambda: hub)
        monkeypatch.setattr(event_tail_service, "list_vault_events", counting_list_vault_events)
        monkeypatch.setattr(event_tail_service, "check_vault_access", check_vault_access)
        monkeypatch.setattr(event_tail_service, "HEARTBEAT_INTERVAL_SECONDS", 60.0)
        codec = EventCursorCodec("cursor-secret")
        streams = [
            event_tail_service._stream_events(
                _Request(),
                user_id="user",
                vault="event-pool",
                vault_id=vault_id,
                kinds=(),
                position=0,
                codec=codec,
            )
            for _ in range(_STREAMS)
        ]
        first_frames = [asyncio.create_task(anext(stream)) for stream in streams]
        try:
            # Every stream has subscribed, read its empty Vault, and is waiting.
            await _until(lambda: hub.connected and hub.subscriber_count == _STREAMS and reads >= _STREAMS)
            await _until(lambda: pool.get_idle_size() == pool.get_size())

            # An ordinary request gets a connection within the probe's budget;
            # in fact it can hold every connection the pool has.
            held = [await pool.acquire(timeout=_PROBE_BUDGET_SECONDS) for _ in range(_POOL_SIZE)]
            try:
                assert [await conn.fetchval("SELECT 1") for conn in held] == [1] * _POOL_SIZE
            finally:
                for conn in held:
                    await pool.release(conn)

            # A commit still reaches every stream through the LISTEN hub.
            event_id = await _insert_event(setup_pool, vault_id, "document.put", "alice")
            frames = await asyncio.wait_for(asyncio.gather(*first_frames), timeout=10)
        finally:
            for task in first_frames:
                task.cancel()
            # A generator can only be closed once its task has let go of it.
            await asyncio.gather(*first_frames, return_exceptions=True)
            for stream in streams:
                await stream.aclose()
            await hub.close()
            await pool.close()

        assert hub.subscriber_count == 0
        async with setup_pool.acquire() as conn:
            (row,) = await list_vault_events(conn, vault_id, after_id=0)
        cursor = codec.encode(vault_id, (), event_id)
        body = {
            "version": 1,
            "cursor": cursor,
            "occurred_at": row["occurred_at"].isoformat().replace("+00:00", "Z"),
            "vault": "event-pool",
            "kind": "document.put",
            "resource_uri": "akb://event-test/document.put",
            "actor": "alice",
            "payload": {"kind": "document.put"},
        }
        # The wire frame is unchanged: event type, cursor id, compact JSON body.
        expected = f"event: change\nid: {cursor}\ndata: {json.dumps(body, separators=(',', ':'))}\n\n"
        assert frames == [expected] * _STREAMS
