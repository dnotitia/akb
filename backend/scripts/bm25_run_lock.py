"""Serialize cooperating BM25 migration commands per database and schema."""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

import asyncpg

from app.config import settings
from app.services.bm25_maintenance import (
    BM25_BULK_CONTROL_LOCK_KEY,
    BM25_BULK_OWNER_LOCK_KEY,
    BM25_RECOMPUTE_LOCK_KEY,
)


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


@asynccontextmanager
async def _hold_bulk_guard(pool, key: int, lost):
    """Hold one bulk owner guard and one shared recompute guard on a session."""
    async with pool.acquire() as conn:
        terminated = False

        def disconnected(_connection):
            nonlocal terminated
            terminated = True
            if not lost.done():
                lost.set_result(None)

        conn.add_termination_listener(disconnected)
        bulk_acquired = False
        shared_acquired = False
        try:
            bulk_acquired = bool(await conn.fetchval(
                "SELECT pg_try_advisory_lock($1::bigint)", key,
            ))
            if not bulk_acquired:
                raise RuntimeError("another BM25 bulk maintenance run is active")

            shared_acquired = bool(await conn.fetchval(
                "SELECT pg_try_advisory_lock_shared($1::bigint)",
                BM25_RECOMPUTE_LOCK_KEY,
            ))
            if not shared_acquired:
                raise RuntimeError("BM25 stats recompute is active")
            yield conn
        finally:
            # Session termination releases both locks. When the session
            # survives, release the shared legacy lock before its bulk guard.
            # The sibling session remains held until the operation has drained.
            if not terminated:
                conn.remove_termination_listener(disconnected)
                if not conn.is_closed():
                    if shared_acquired:
                        await conn.fetchval(
                            "SELECT pg_advisory_unlock_shared($1::bigint)",
                            BM25_RECOMPUTE_LOCK_KEY,
                        )
                    if bulk_acquired:
                        await conn.fetchval(
                            "SELECT pg_advisory_unlock($1::bigint)", key,
                        )


async def _run_until_guard_loss(
    lost,
    operation: Callable[[], Awaitable[None]],
    *,
    label: str = "BM25 maintenance",
) -> None:
    if lost.done():
        raise RuntimeError(f"{label} lock connection lost before start")
    task = asyncio.create_task(operation())
    try:
        await asyncio.wait((task, lost), return_when=asyncio.FIRST_COMPLETED)
        if lost.done():
            raise RuntimeError(f"{label} lock connection lost; operation stopped")
        await task
    finally:
        await _cancel_and_drain(task)


async def _cancel_and_drain(task: asyncio.Task) -> None:
    """Cancel once, then resist wrapper cancellation until child cleanup ends."""
    if not task.done():
        task.cancel()

    deferred_cancellation = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            current = asyncio.current_task()
            wrapper_is_cancelling = current is not None and current.cancelling()
            if (not task.done() or wrapper_is_cancelling) and deferred_cancellation is None:
                deferred_cancellation = error
            if task.done():
                break
            # A containing guard was cancelled while this child was cleaning
            # up. Remember that cancellation, but do not let it pierce the
            # shield and release the surviving advisory-lock session early.
        except BaseException:
            # Child failures are propagated by the surrounding runner's
            # existing control flow. Here the only obligation is to drain it.
            break

    # Retrieve a terminal exception so an operation cancelled because a lock
    # was lost cannot produce an unobserved-task warning.
    try:
        task.exception()
    except asyncio.CancelledError:
        pass

    if deferred_cancellation is not None:
        raise deferred_cancellation


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
                await _run_until_guard_loss(lost, operation, label="BM25 migration")
    finally:
        if not lost.done():
            lost.cancel()


async def run_bulk_exclusive(operation: Callable[[], Awaitable[None]]) -> None:
    """Exclude stats recompute and peer bulk work through two main-DB sessions.

    Each independent session holds a shared form of the recompute's legacy
    exclusive lock plus its own corpus-wide bulk lock. Losing either session
    cancels and drains the wrapped vector-store-owned operation while the
    surviving session still excludes both recompute and another bulk run.
    """
    pool = await asyncpg.create_pool(
        dsn=settings.asyncpg_dsn,
        min_size=2,
        max_size=2,
        command_timeout=None,
    )
    lost = asyncio.get_running_loop().create_future()
    try:
        async with _hold_bulk_guard(pool, BM25_BULK_OWNER_LOCK_KEY, lost):
            async with _hold_bulk_guard(pool, BM25_BULK_CONTROL_LOCK_KEY, lost):
                await _run_until_guard_loss(lost, operation)
    finally:
        if not lost.done():
            lost.cancel()
        await pool.close()
