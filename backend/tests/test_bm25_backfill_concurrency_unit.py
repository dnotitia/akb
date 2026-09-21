"""Failures must drain siblings before retry, return, or lock release."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import uuid

import asyncpg
import pytest

from scripts import backfill_bm25_vector as cli

pytestmark = pytest.mark.asyncio


class Pool:
    def __init__(self, rows=()):
        self.rows = rows

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def execute(self, sql, ids, *args, **kwargs):
        return f"UPDATE {len(ids)}"

    async def fetch(self, *args):
        rows, self.rows = self.rows, []
        return rows


def rows(n):
    return [{"chunk_id": uuid.uuid4(), "content": str(i), "indexed_at": datetime.now(timezone.utc)} for i in range(n)]


async def cancel(tasks):
    for task in list(tasks):
        task.cancel()
    await asyncio.gather(*list(tasks), return_exceptions=True)


async def test_deadlock_retry_has_no_surviving_first_attempt_encodes(monkeypatch):
    active, peak, attempt = 0, 0, 0
    tasks = set()
    four_started = asyncio.Event()
    original = cli._apply_once

    async def once(*args):
        nonlocal attempt
        attempt += 1
        return await original(*args)

    async def encode(content, gate):
        nonlocal active, peak
        task = asyncio.current_task()
        tasks.add(task)
        try:
            async with gate:
                active += 1
                peak = max(peak, active)
                if active == cli._CONCURRENCY:
                    four_started.set()
                try:
                    if attempt == 1:
                        if content == "0":
                            await four_started.wait()
                            raise asyncpg.DeadlockDetectedError("injected")
                        await asyncio.Event().wait()
                    return "{}"
                finally:
                    active -= 1
        finally:
            tasks.discard(task)

    monkeypatch.setattr(cli, "_apply_once", once)
    monkeypatch.setattr(cli, "_encode", encode)
    try:
        assert await asyncio.wait_for(cli._apply(Pool(), "v", rows(12)), 3) == 12
        assert attempt == 2
        assert active == 0
        assert peak <= cli._CONCURRENCY
        assert not tasks
    finally:
        await cancel(tasks)


async def test_encode_failure_waits_for_sibling_cleanup(monkeypatch):
    all_started, cleaned = asyncio.Event(), asyncio.Event()
    tasks = set()

    async def encode(content, gate):
        tasks.add(asyncio.current_task())
        try:
            if content == "0":
                await all_started.wait()
                raise ValueError("not a retryable failure")
            try:
                all_started.set()
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()
        finally:
            tasks.discard(asyncio.current_task())

    monkeypatch.setattr(cli, "_encode", encode)
    try:
        with pytest.raises(ValueError, match="not a retryable"):
            await cli._apply(Pool(), "v", rows(2))
        assert cleaned.is_set()
        assert not tasks
    finally:
        await cancel(tasks)


async def test_outer_cancellation_drains_encodes(monkeypatch):
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def encode(*args):
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    monkeypatch.setattr(cli, "_encode", encode)
    task = asyncio.create_task(cli._apply(Pool(), "v", rows(2)))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()


async def test_writer_failure_drains_other_writer_before_pass_returns(monkeypatch):
    started, cleaned = asyncio.Event(), asyncio.Event()
    tasks = set()

    async def apply(pool, schema, part):
        tasks.add(asyncio.current_task())
        try:
            if part[0]["content"] == "0":
                await started.wait()
                raise ValueError("writer failed")
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()
        finally:
            tasks.discard(asyncio.current_task())

    monkeypatch.setattr(cli, "_apply", apply)
    try:
        with pytest.raises(ValueError, match="writer failed"):
            await cli._pass(Pool(rows(4)), "v", None, writers=2)
        assert cleaned.is_set()
        assert not tasks
    finally:
        await cancel(tasks)


async def test_retry_exhaustion_does_not_swallow_deadlock(monkeypatch):
    attempts = 0

    async def fail(*args):
        nonlocal attempts
        attempts += 1
        raise asyncpg.DeadlockDetectedError("exhausted")

    monkeypatch.setattr(cli, "_apply_once", fail)
    with pytest.raises(asyncpg.DeadlockDetectedError, match="exhausted"):
        await cli._apply(Pool(), "v", [], attempts=2)
    assert attempts == 2
