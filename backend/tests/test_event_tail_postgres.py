"""PostgreSQL proof for retained event ordering and Vault isolation."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock
import uuid

import asyncpg
import pytest

from app.repositories.events_repo import get_vault_event_bounds, list_vault_events
from app.services.event_tail_hub import EventTailHub
from app.services.event_tail_service import EventBounds, EventCursorCodec, EventGapError, validate_event_gap


pytestmark = pytest.mark.asyncio

_DSN = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@localhost:15432/akb")


async def _can_connect(dsn: str) -> bool:
    try:
        conn = await asyncpg.connect(dsn, timeout=2)
    except OSError, asyncpg.PostgresError:
        return False
    await conn.close()
    return True


def _database_dsn(name: str) -> str:
    base, _ = _DSN.rsplit("/", 1)
    return f"{base}/{name}"


@contextlib.asynccontextmanager
async def _fresh_database():
    if not await _can_connect(_DSN):
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"Required PostgreSQL is not reachable at {_DSN}")
        pytest.skip(f"Postgres not reachable at {_DSN}")

    admin = await asyncpg.connect(_DSN)
    name = f"akb_event_tail_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    pool = await asyncpg.create_pool(_database_dsn(name), min_size=1, max_size=4)
    previous_pool = None
    try:
        init_sql = (Path(__file__).resolve().parents[1] / "app" / "db" / "init.sql").read_text()
        async with pool.acquire() as conn:
            await conn.execute(init_sql)
        from app.db import postgres as postgres_module

        previous_pool = postgres_module._pool
        postgres_module._pool = pool
        await postgres_module._apply_migrations()
        yield pool
    finally:
        from app.db import postgres as postgres_module

        postgres_module._pool = previous_pool
        await pool.close()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


async def _listening_hub(pool: asyncpg.Pool) -> EventTailHub:
    """A LISTEN hub on the test database, not the configured one."""
    dsn = _database_dsn(await pool.fetchval("SELECT current_database()"))
    return EventTailHub(lambda: asyncpg.connect(dsn), reconnect_initial_delay=0.05, close_timeout=2.0)


async def _make_vault(pool: asyncpg.Pool, name: str) -> uuid.UUID:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO vaults (name, git_path) VALUES ($1, $2) RETURNING id",
            name,
            f"/tmp/{name}.git",
        )


async def _insert_event(
    pool: asyncpg.Pool,
    vault_id: uuid.UUID,
    kind: str,
    actor: str,
) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO events (vault_id, kind, resource_uri, actor_id, payload)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            RETURNING id
            """,
            vault_id,
            kind,
            f"akb://event-test/{kind}",
            actor,
            json.dumps({"kind": kind}),
        )


async def test_event_repository_orders_and_isolates_vault_rows():
    async with _fresh_database() as pool:
        vault_id = await _make_vault(pool, "event-tail-a")
        other_vault_id = await _make_vault(pool, "event-tail-b")
        first_id = await _insert_event(pool, vault_id, "document.put", "alice")
        second_id = await _insert_event(pool, other_vault_id, "document.put", "bob")
        third_id = await _insert_event(pool, vault_id, "table.rows_changed", "alice")

        async with pool.acquire() as conn:
            # Nothing was deleted, so the Vault has no retention watermark yet.
            assert await get_vault_event_bounds(conn, vault_id) == (first_id, third_id, 0)
            rows = await list_vault_events(conn, vault_id, after_id=0)
            assert [row["id"] for row in rows] == [first_id, third_id]
            assert all(row["vault_id"] == vault_id for row in rows)

        assert second_id not in {first_id, third_id}


async def test_retention_bounds_drive_gap_recovery_without_skipping(monkeypatch):
    async with _fresh_database() as pool:
        vault_id = await _make_vault(pool, "event-gap")
        first_id = await _insert_event(pool, vault_id, "document.put", "alice")
        second_id = await _insert_event(pool, vault_id, "document.update", "alice")
        latest_id = await _insert_event(pool, vault_id, "document.delete", "alice")

        # The first event was published long enough ago for the sweeper.
        await pool.execute(
            "UPDATE events SET redis_published_at = NOW() - INTERVAL '8 days' WHERE id = $1", first_id
        )
        await _sweep(monkeypatch, pool)
        async with pool.acquire() as conn:
            earliest_id, current_latest_id, deleted_through = await get_vault_event_bounds(conn, vault_id)

        assert (earliest_id, current_latest_id, deleted_through) == (second_id, latest_id, first_id)
        codec = EventCursorCodec("cursor-secret")
        bounds = EventBounds(earliest_id, current_latest_id, deleted_through)
        with pytest.raises(EventGapError) as exc_info:
            validate_event_gap(codec, vault_id, (), position=first_id - 1, bounds=bounds)
        # Resuming at the watermark skips nothing that is still retained.
        assert codec.decode(exc_info.value.details["earliest_cursor"]) == (vault_id, (), first_id)
        assert codec.decode(exc_info.value.details["latest_cursor"]) == (vault_id, (), latest_id)
        validate_event_gap(codec, vault_id, (), position=first_id, bounds=bounds)


async def _sweep(monkeypatch, pool: asyncpg.Pool) -> int:
    """Run the events sweeper once against ``pool``, past its rate limit."""
    from app.services import events_publisher

    async def get_pool() -> asyncpg.Pool:
        return pool

    monkeypatch.setattr(events_publisher, "get_pool", get_pool)
    monkeypatch.setattr(events_publisher, "_last_sweep_at", float("-inf"))
    return await events_publisher._sweep_once()


async def test_postgres_tail_stream_preserves_order_filter_checkpoints_and_resume(monkeypatch):
    from app.services import event_tail_service

    class _Request:
        async def is_disconnected(self) -> bool:
            return False

    async with _fresh_database() as pool:
        vault_id = await _make_vault(pool, "event-stream")
        first_id = await _insert_event(pool, vault_id, "document.put", "alice")
        selected_id = await _insert_event(pool, vault_id, "table.rows_changed", "alice")
        skipped_id = await _insert_event(pool, vault_id, "document.update", "alice")
        codec = EventCursorCodec("cursor-secret")

        async def get_pool():
            return pool

        hub = await _listening_hub(pool)
        monkeypatch.setattr(event_tail_service, "get_pool", get_pool)
        monkeypatch.setattr(event_tail_service, "get_event_tail_hub", lambda: hub)
        monkeypatch.setattr(
            event_tail_service,
            "check_vault_access",
            AsyncMock(return_value={"vault_id": vault_id}),
        )

        stream = event_tail_service._stream_events(
            _Request(),
            user_id="user",
            vault="event-stream",
            vault_id=vault_id,
            kinds=("table.rows_changed",),
            position=first_id - 1,
            codec=codec,
            start_frame=event_tail_service._checkpoint_frame(
                codec.encode(vault_id, ("table.rows_changed",), first_id - 1)
            ),
            checked_at=asyncio.get_running_loop().time(),
        )
        start_frame = await anext(stream)
        first_frame = await anext(stream)
        selected_frame = await anext(stream)
        skipped_frame = await anext(stream)
        await stream.aclose()

        # Where the stream starts, then one frame per retained event.
        assert start_frame.startswith("event: checkpoint\n")
        assert codec.decode(start_frame.splitlines()[1][4:])[2] == first_id - 1
        assert first_frame.startswith("event: checkpoint\n")
        assert selected_frame.startswith("event: change\n")
        assert skipped_frame.startswith("event: checkpoint\n")
        assert codec.decode(first_frame.splitlines()[1][4:])[2] == first_id
        assert codec.decode(selected_frame.splitlines()[1][4:])[2] == selected_id
        assert codec.decode(skipped_frame.splitlines()[1][4:])[2] == skipped_id
        selected_body = json.loads(selected_frame.split("data: ", 1)[1])
        assert selected_body["kind"] == "table.rows_changed"
        assert "vault_id" not in selected_body

        resumed_id = await _insert_event(pool, vault_id, "table.rows_changed", "alice")
        resumed = event_tail_service._stream_events(
            _Request(),
            user_id="user",
            vault="event-stream",
            vault_id=vault_id,
            kinds=("table.rows_changed",),
            position=selected_id,
            codec=codec,
            start_frame=event_tail_service._checkpoint_frame(
                codec.encode(vault_id, ("table.rows_changed",), selected_id)
            ),
            checked_at=asyncio.get_running_loop().time(),
        )
        resumed_start = await anext(resumed)
        checkpoint_after_resume = await anext(resumed)
        next_change = await anext(resumed)
        await resumed.aclose()
        await hub.close()
        assert hub.subscriber_count == 0
        assert codec.decode(resumed_start.splitlines()[1][4:])[2] == selected_id
        assert checkpoint_after_resume.startswith("event: checkpoint\n")
        assert next_change.startswith("event: change\n")
        assert codec.decode(next_change.splitlines()[1][4:])[2] == resumed_id
