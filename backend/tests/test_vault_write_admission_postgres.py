"""PostgreSQL checks of the per-process Vault write admission.

A transaction that writes a Vault's events waits for the Vault's event lock
(migration 132), and used to wait holding its pool connection. A burst of
writes to one Vault then took every connection, and a request for another
Vault waited for one until the writers' 5-second lock deadline freed them.
Every event-writing path now waits for admission before it takes a
connection; at most `vault_write_concurrency` transactions of a Vault hold one.
"""

from __future__ import annotations

import asyncio
import time

import asyncpg

from app.config import settings
from app.db import postgres as postgres_module
from app.repositories.events_repo import emit_event
from app.services.collection_service import CollectionService
from tests.test_events_commit_order_postgres import (
    _OpenTransaction,
    _database_dsn,
    _fresh_database,
    _make_vault,
)

# The application pool of the storm test, and the writers sent at one Vault.
_POOL_SIZE = 8
_WRITERS = 24
# How long another transaction holds the busy Vault's event lock.
_HOLD_SECONDS = 3.0
# The longest another Vault's request may wait for a connection meanwhile.
_OTHER_VAULT_WAIT_BOUND = 1.0


async def test_a_write_storm_on_one_vault_leaves_connections_for_another_vault(monkeypatch):
    """While a transaction holds one Vault's event lock, 24 collection creates
    go to that Vault through a pool of 8 connections, and a reader of another
    Vault takes a connection every 0.1 s. Before admission the creates took
    all 8 connections to wait for the lock, and the reader waited until they
    gave up at the lock's deadline. Now two of them wait with a connection and
    the reader is served at once; once the lock is free, every create commits.
    """
    monkeypatch.setattr(settings, "vault_write_concurrency", 2, raising=False)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 30.0, raising=False)
    async with _fresh_database() as admin_pool:
        storm = await _make_vault(admin_pool, "storm")
        quiet = await _make_vault(admin_pool, "quiet")
        database = await admin_pool.fetchval("SELECT current_database()")
        app_pool = await asyncpg.create_pool(_database_dsn(database), min_size=1, max_size=_POOL_SIZE)
        previous_pool = postgres_module._pool
        postgres_module._pool = app_pool
        collections = CollectionService(git=object())
        writers: list[asyncio.Task] = []
        waits: list[float] = []
        try:
            async with admin_pool.acquire() as holder:
                async with _OpenTransaction(holder):
                    await emit_event(holder, "document.put", vault_id=storm, payload={"marker": "holder"})
                    writers = [
                        asyncio.create_task(collections.create(
                            vault="storm", path=f"burst-{i}", summary=None, agent_id="storm",
                        ))
                        for i in range(_WRITERS)
                    ]
                    await asyncio.sleep(0.5)
                    until = time.monotonic() + _HOLD_SECONDS
                    while time.monotonic() < until:
                        started = time.monotonic()
                        async with app_pool.acquire() as reader:
                            await reader.fetchval(
                                "SELECT count(*) FROM collections WHERE vault_id = $1", quiet,
                            )
                        waits.append(time.monotonic() - started)
                        await asyncio.sleep(0.1)
                # The holder rolled back; the storm can commit.
            results = await asyncio.wait_for(
                asyncio.gather(*writers, return_exceptions=True), timeout=60,
            )
        finally:
            for task in writers:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*writers, return_exceptions=True)
            postgres_module._pool = previous_pool
            await app_pool.close()

        failures = [type(r).__name__ for r in results if isinstance(r, BaseException)]
        print(
            f"\nother-Vault pool wait: max {max(waits):.3f}s over {len(waits)} reads; "
            f"storm writes failed: {len(failures)} of {_WRITERS} {sorted(set(failures))}"
        )
        assert max(waits) < _OTHER_VAULT_WAIT_BOUND, waits
        assert failures == []
        assert await admin_pool.fetchval(
            "SELECT count(*) FROM collections WHERE vault_id = $1", storm,
        ) == _WRITERS
