"""Serialize cooperating BM25 migration commands per database and schema."""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager


def _lock_key(schema: str, role: str = "owner") -> int:
    value = hashlib.blake2b(
        ("akb:bm25-backfill:" + role + ":" + schema).encode(), digest_size=8,
    ).digest()
    return int.from_bytes(value, "big", signed=True)


@asynccontextmanager
async def _hold(pool, key, lost):
    async with pool.acquire() as conn:
        acquired = await conn.fetchval("SELECT pg_try_advisory_lock($1::bigint)", key)
        if not acquired:
            raise RuntimeError("another BM25 migration is running for this schema")

        terminated = False

        def disconnected(_connection):
            nonlocal terminated
            terminated = True
            if not lost.done():
                lost.set_result(None)

        conn.add_termination_listener(disconnected)
        try:
            yield conn
        finally:
            # asyncpg detaches a pool proxy when its session terminates.
            if not terminated:
                conn.remove_termination_listener(disconnected)
                if not conn.is_closed():
                    await conn.fetchval("SELECT pg_advisory_unlock($1::bigint)", key)


async def run_exclusive(
    pool, schema: str, operation: Callable[[], Awaitable[None]], *, announce: bool = False,
) -> None:
    """Keep two independent session guards through operation cancellation.

    The CLI reserves two connections. Losing either cancels and drains its
    operation while the other still excludes another cooperating migration.
    The announced control session may be terminated by an operator; the owner
    guard remains held until the operation has drained. A database-wide loss
    still requires operator reconciliation, not blind automatic restart.
    These locks coordinate this CLI, not ordinary indexing writers.
    """
    lost = asyncio.get_running_loop().create_future()
    try:
        async with _hold(pool, _lock_key(schema), lost):
            control_key = _lock_key(schema, "control")
            async with _hold(pool, control_key, lost) as control:
                if announce:
                    name = "akb-bm25:" + uuid.uuid4().hex
                    await control.fetchval("SELECT set_config('application_name', $1, false)", name)
                    row = await control.fetchrow(
                        "SELECT pid, backend_start, datname FROM pg_stat_activity "
                        "WHERE pid = pg_backend_pid()"
                    )
                    print("BM25_LOCK " + json.dumps({
                        "pid": row["pid"], "backend_start": row["backend_start"].isoformat(),
                        "database": row["datname"], "application_name": name,
                        "schema": schema, "key": control_key,
                    }), flush=True)
                if lost.done():
                    raise RuntimeError("BM25 migration lock connection lost before start")
                task = asyncio.create_task(operation())
                try:
                    await asyncio.wait((task, lost), return_when=asyncio.FIRST_COMPLETED)
                    if lost.done():
                        raise RuntimeError("BM25 migration lock connection lost; operation stopped")
                    await task
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
    finally:
        if not lost.done():
            lost.cancel()
