"""PostgreSQL proof that an event wakes only the streams of its own Vault.

The `events` insert trigger notified with the row id alone, so every server
process woke every open stream for every event, and each stream then queried
its own Vault whether or not the event was in it. Migration 131 puts the Vault
in the notification and the hub wakes only that Vault's streams.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable
from typing import Any

import asyncpg
import pytest

from app.db.postgres import _load_migration
from app.repositories.events_repo import list_vault_events
from app.services import event_tail_service
from app.services.event_tail_hub import EVENT_CHANNEL
from app.services.event_tail_service import EventCursorCodec
from tests.test_event_tail_postgres import _database_dsn, _fresh_database, _insert_event, _listening_hub, _make_vault


pytestmark = pytest.mark.asyncio

_MIGRATION = "131_events_notify_vault.py"


class _Request:
    async def is_disconnected(self) -> bool:
        return False


async def _until(predicate: Callable[[], bool], *, timeout: float = 10.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout)


async def _settled(read_count: Callable[[], int], busy: Callable[[], bool], *, quiet: float = 0.2) -> None:
    """Wait until no read is under way and none has started for ``quiet`` seconds."""

    async def poll() -> None:
        while True:
            seen = read_count()
            await asyncio.sleep(quiet)
            if read_count() == seen and not busy():
                return

    await asyncio.wait_for(poll(), 10.0)


async def test_an_insert_notifies_once_with_its_vault_and_id():
    async with _fresh_database() as pool:
        database = await pool.fetchval("SELECT current_database()")
        vault_id = await _make_vault(pool, "event-route-notify")
        listener = await asyncpg.connect(_database_dsn(database))
        payloads: list[str] = []
        try:
            await listener.add_listener(EVENT_CHANNEL, lambda *args: payloads.append(args[3]))
            event_id = await _insert_event(pool, vault_id, "document.put", "alice")
            vaultless_id = await _insert_event(pool, None, "vault.lifecycle", "alice")
            await _until(lambda: len(payloads) >= 2)
            # Nothing else follows: one notification per insert.
            await asyncio.sleep(0.2)
            assert payloads == [f"{vault_id}:{event_id}", f":{vaultless_id}"]

            # Applying the migration again changes nothing.
            migration = _load_migration(_MIGRATION)
            assert migration is not None
            async with pool.acquire() as conn:
                await migration.migrate(conn=conn)
            payloads.clear()
            again_id = await _insert_event(pool, vault_id, "document.update", "alice")
            await _until(lambda: bool(payloads))
            await asyncio.sleep(0.2)
            assert payloads == [f"{vault_id}:{again_id}"]
        finally:
            await listener.close()

        assert await pool.fetchval("SELECT count(*) FROM schema_migrations WHERE filename = $1", _MIGRATION) == 1
        # The trigger itself is the one migration 015 made.
        assert (
            await pool.fetchval(
                """
                SELECT count(*) FROM pg_trigger
                 WHERE tgrelid = 'events'::regclass AND tgname = 'akb_events_notify_trigger' AND NOT tgisinternal
                """
            )
            == 1
        )


async def test_an_event_wakes_only_the_streams_of_its_vault(monkeypatch):
    names = ["event-route-a", "event-route-b", "event-route-c"]
    async with _fresh_database() as pool:
        vault_ids = [await _make_vault(pool, name) for name in names]
        hub = await _listening_hub(pool)
        reads: Counter[str] = Counter()
        reading = 0

        async def counting_list_vault_events(conn: Any, vault_id: Any, **kwargs: Any) -> list[dict[str, Any]]:
            nonlocal reading
            reads[str(vault_id)] += 1
            reading += 1
            try:
                return await list_vault_events(conn, vault_id, **kwargs)
            finally:
                reading -= 1

        async def get_pool() -> asyncpg.Pool:
            return pool

        async def check_vault_access(_user_id: str, vault: str, **_kwargs: Any) -> dict[str, Any]:
            return {"vault_id": vault_ids[names.index(vault)]}

        monkeypatch.setattr(event_tail_service, "get_pool", get_pool)
        monkeypatch.setattr(event_tail_service, "get_event_tail_hub", lambda: hub)
        monkeypatch.setattr(event_tail_service, "list_vault_events", counting_list_vault_events)
        monkeypatch.setattr(event_tail_service, "check_vault_access", check_vault_access)
        monkeypatch.setattr(event_tail_service, "_db_budget", None)
        monkeypatch.setattr(event_tail_service, "HEARTBEAT_INTERVAL_SECONDS", 60.0)
        codec = EventCursorCodec("cursor-secret")
        streams = [
            event_tail_service._stream_events(
                _Request(),  # type: ignore[arg-type]
                user_id="user",
                vault=names[index % len(names)],
                vault_id=vault_ids[index % len(names)],
                kinds=(),
                position=0,
                codec=codec,
                checked_at=asyncio.get_running_loop().time(),
            )
            for index in range(2 * len(names))
        ]
        # Each stream first says where it starts; then it waits for its Vault.
        for stream in streams:
            assert (await anext(stream)).startswith("event: checkpoint\n")
        first_frames = [asyncio.create_task(anext(stream)) for stream in streams]
        try:
            await _until(lambda: hub.connected and hub.subscriber_count == len(streams))
            # Every stream has read its empty Vault, including after the
            # wake-up that establishing LISTEN sends to all of them.
            await _settled(lambda: sum(reads.values()), lambda: reading > 0)
            before = Counter(reads)

            event_id = await _insert_event(pool, vault_ids[0], "document.put", "alice")
            frames = await asyncio.wait_for(asyncio.gather(first_frames[0], first_frames[3]), timeout=10)
            # Time enough for a stream of another Vault to read, had it been woken.
            await asyncio.sleep(0.3)
            others_done = [task.done() for index, task in enumerate(first_frames) if index % len(names)]
        finally:
            for task in first_frames:
                task.cancel()
            # A generator can only be closed once its task has let go of it.
            await asyncio.gather(*first_frames, return_exceptions=True)
            for stream in streams:
                await stream.aclose()
            await hub.close()

        assert [codec.decode(frame.splitlines()[1][len("id: ") :])[2] for frame in frames] == [event_id, event_id]
        assert others_done == [False] * 4
        woken, *idle = (str(vault_id) for vault_id in vault_ids)
        # Each stream of the event's Vault read once more; no other stream read at all.
        assert reads[woken] - before[woken] >= 2
        assert [reads[vault] - before[vault] for vault in idle] == [0, 0]
