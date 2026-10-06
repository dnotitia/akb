"""Live PostgreSQL proof for dynamic-table row-change wake-up events."""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import asyncpg
import pytest

from app.repositories import table_data_repo
from app.services.user_sql_executor import UserSqlExecutor

pytestmark = pytest.mark.asyncio

_DSN = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@localhost:5433/akb")


async def _can_connect(dsn: str) -> bool:
    try:
        conn = await asyncpg.connect(dsn, timeout=2)
    except (OSError, asyncpg.PostgresError):
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
    name = f"akb_rows_changed_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    pool = await asyncpg.create_pool(_database_dsn(name), min_size=1, max_size=8)
    previous_pool = None
    try:
        init_sql = (
            Path(__file__).resolve().parents[1] / "app" / "db" / "init.sql"
        ).read_text()
        async with pool.acquire() as conn:
            await conn.execute(init_sql)
        from app.db import postgres as postgres_module

        previous_pool = postgres_module._pool
        postgres_module._pool = pool
        yield pool, postgres_module
    finally:
        from app.db import postgres as postgres_module

        postgres_module._pool = previous_pool
        await pool.close()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


async def _event_rows(conn: Any, vault_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT vault_id, kind, resource_uri, actor_id,
               payload->>'operation' AS operation
          FROM events
         WHERE vault_id = $1 AND kind = 'table.rows_changed'
         ORDER BY id
        """,
        vault_id,
    )
    return [dict(row) for row in rows]


async def _seed_legacy_table(pool: asyncpg.Pool, vault_id: uuid.UUID, name: str) -> None:
    pg_name = table_data_repo.pg_table_name("rows-changed", name)
    async with pool.acquire() as conn:
        await conn.execute(
            f"CREATE TABLE {pg_name} ("
            "id UUID PRIMARY KEY DEFAULT uuid_generate_v4(), "
            "value TEXT, created_by TEXT, "
            "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
            "updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
        )
        await conn.execute(
            "INSERT INTO vault_tables (id, vault_id, name, description, columns, unique_keys, indexes) "
            "VALUES ($1, $2, $3, '', $4::jsonb, '[]'::jsonb, '[]'::jsonb)",
            uuid.uuid4(),
            vault_id,
            name,
            json.dumps([{"name": "value", "type": "text"}]),
        )


async def _make_vault(pool: asyncpg.Pool) -> uuid.UUID:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO vaults (name, git_path) VALUES ('rows-changed', $1) RETURNING id",
            "/tmp/rows-changed.git",
        )


async def _assert_mutation_contract(
    pool: asyncpg.Pool,
    vault_id: uuid.UUID,
    table_name: str,
    *,
    actor: str,
) -> None:
    pg_name = table_data_repo.pg_table_name("rows-changed", table_name)
    uri = f"akb://rows-changed/table/{table_name}"
    executor = UserSqlExecutor(pool)

    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM events WHERE vault_id = $1 AND kind = 'table.rows_changed'",
            vault_id,
        )

    await executor.execute(
        user_id="user-id",
        actor_id=actor,
        sql=f"INSERT INTO {pg_name} (value) VALUES ('one')",
        is_admin=True,
        vault_names=["rows-changed"],
    )
    await executor.execute(
        user_id="user-id",
        actor_id=actor,
        sql=f"INSERT INTO {pg_name} (value) VALUES ('bulk-a'), ('bulk-b')",
        is_admin=True,
        vault_names=["rows-changed"],
    )
    await executor.execute(
        user_id="user-id",
        actor_id=actor,
        sql=f"UPDATE {pg_name} SET value = 'updated' WHERE value LIKE 'bulk-%'",
        is_admin=True,
        vault_names=["rows-changed"],
    )

    async with pool.acquire() as conn:
        rows = await _event_rows(conn, vault_id)
        assert [row["operation"] for row in rows] == ["insert", "insert", "update"]
        assert all(
            row["kind"] == "table.rows_changed"
            and row["vault_id"] == vault_id
            and row["resource_uri"] == uri
            and row["actor_id"] == actor
            for row in rows
        )

    await executor.execute(
        user_id="user-id",
        actor_id=actor,
        sql=f"UPDATE {pg_name} SET value = 'missing' WHERE value = 'does-not-exist'",
        is_admin=True,
        vault_names=["rows-changed"],
    )

    async with pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        await conn.execute(
            "SELECT set_config('akb.actor_id', $1, true)",
            actor,
        )
        await conn.execute(f"DELETE FROM {pg_name} WHERE value = 'one'")
        await tx.rollback()

    await executor.execute(
        user_id="user-id",
        actor_id=actor,
        sql=f"DELETE FROM {pg_name} WHERE value = 'updated'",
        is_admin=True,
        vault_names=["rows-changed"],
    )

    async with pool.acquire() as conn:
        rows = await _event_rows(conn, vault_id)
        assert [row["operation"] for row in rows] == [
            "insert",
            "insert",
            "update",
            "delete",
        ]
        assert await conn.fetchval(f"SELECT COUNT(*) FROM {pg_name}") == 1


async def test_existing_and_new_dynamic_tables_emit_bounded_committed_events():
    async with _fresh_database() as (pool, postgres_module):
        vault_id = await _make_vault(pool)
        await _seed_legacy_table(pool, vault_id, "legacy")
        await postgres_module._apply_migrations()

        new_name = "fresh"
        new_pg_name = table_data_repo.pg_table_name("rows-changed", new_name)
        async with pool.acquire() as conn:
            await table_data_repo.create_dynamic_table(
                conn,
                new_pg_name,
                [{"name": "value", "type": "text"}],
                vault_name="rows-changed",
                vault_id=vault_id,
                resource_uri=f"akb://rows-changed/table/{new_name}",
            )
            await conn.execute(
                "INSERT INTO vault_tables (id, vault_id, name, description, columns, unique_keys, indexes) "
                "VALUES ($1, $2, $3, '', $4::jsonb, '[]'::jsonb, '[]'::jsonb)",
                uuid.uuid4(),
                vault_id,
                new_name,
                json.dumps([{"name": "value", "type": "text"}]),
            )

        await _assert_mutation_contract(pool, vault_id, "legacy", actor="alice")
        await _assert_mutation_contract(pool, vault_id, new_name, actor="alice")


async def test_publisher_fanout_preserves_rows_changed_envelope(monkeypatch):
    async with _fresh_database() as (pool, postgres_module):
        await postgres_module._apply_migrations()
        vault_id = await _make_vault(pool)
        table_name = "streamed"
        pg_name = table_data_repo.pg_table_name("rows-changed", table_name)
        async with pool.acquire() as conn:
            await table_data_repo.create_dynamic_table(
                conn,
                pg_name,
                [{"name": "value", "type": "text"}],
                vault_name="rows-changed",
                vault_id=vault_id,
                resource_uri=f"akb://rows-changed/table/{table_name}",
            )

        await UserSqlExecutor(pool).execute(
            user_id="user-id",
            actor_id="alice",
            sql=f"INSERT INTO {pg_name} (value) VALUES ('one')",
            is_admin=True,
            vault_names=["rows-changed"],
        )

        from app.services import events_publisher

        class _Redis:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[Any, Any]]] = []

            async def xadd(self, stream: str, fields: dict[Any, Any], **_: Any) -> str:
                self.calls.append((stream, fields))
                return "1-0"

        redis = _Redis()
        monkeypatch.setattr(events_publisher.settings, "redis_url", "redis://test")
        monkeypatch.setattr(events_publisher, "get_pool", lambda: pool)
        monkeypatch.setattr(events_publisher, "_client", AsyncMock(return_value=redis))

        assert await events_publisher._process_once() == 1
        assert len(redis.calls) == 1
        stream, fields = redis.calls[0]
        assert stream == events_publisher.settings.redis_event_stream
        assert fields[b"kind"] == b"table.rows_changed"
        assert fields[b"vault_id"] == str(vault_id).encode()
        assert fields[b"resource_uri"] == b"akb://rows-changed/table/streamed"
        assert fields[b"actor_id"] == b"alice"
        assert json.loads(fields[b"payload"]) == {"operation": "insert"}

        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT redis_published_at IS NOT NULL FROM events "
                "WHERE vault_id = $1 AND kind = 'table.rows_changed'",
                vault_id,
            ) is True


async def _make_named_vault(pool: asyncpg.Pool, name: str) -> uuid.UUID:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO vaults (name, git_path) VALUES ($1, $2) RETURNING id",
            name,
            f"/tmp/{name}.git",
        )


async def test_publisher_suppresses_gardener_bookkeeping_without_xadd(monkeypatch):
    """sweep_log / gardener_event_log / gardener_kv rows_changed events in a
    gdn-* vault are marked published WITHOUT an XADD (self-feedback
    suppression, SKH 2026-09-30: the daemon's own bookkeeping flooded
    akb:events at ~60%). A user vault's same-named tables still publish."""
    async with _fresh_database() as (pool, postgres_module):
        await postgres_module._apply_migrations()
        gdn_vault_id = await _make_named_vault(pool, "gdn-state")
        user_vault_id = await _make_named_vault(pool, "user-vault")
        for vault_name, vault_id in (("gdn-state", gdn_vault_id), ("user-vault", user_vault_id)):
            for table_name in ("sweep_log", "gardener_event_log", "gardener_kv", "nodes"):
                pg_name = table_data_repo.pg_table_name(vault_name, table_name)
                async with pool.acquire() as conn:
                    await table_data_repo.create_dynamic_table(
                        conn,
                        pg_name,
                        [{"name": "value", "type": "text"}],
                        vault_name=vault_name,
                        vault_id=vault_id,
                        resource_uri=f"akb://{vault_name}/table/{table_name}",
                    )

        executor = UserSqlExecutor(pool)
        for vault_name, vault_id in (("gdn-state", gdn_vault_id), ("user-vault", user_vault_id)):
            for table_name in ("sweep_log", "gardener_event_log", "gardener_kv", "nodes"):
                pg_name = table_data_repo.pg_table_name(vault_name, table_name)
                await executor.execute(
                    user_id="user-id",
                    actor_id="alice",
                    sql=f"INSERT INTO {pg_name} (value) VALUES ('one')",
                    is_admin=True,
                    vault_names=[vault_name],
                )

        from app.services import events_publisher

        class _Redis:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[Any, Any]]] = []

            async def xadd(self, stream: str, fields: dict[Any, Any], **_: Any) -> str:
                self.calls.append((stream, fields))
                return "1-0"

        redis = _Redis()
        monkeypatch.setattr(events_publisher.settings, "redis_url", "redis://test")
        monkeypatch.setattr(events_publisher, "get_pool", lambda: pool)
        monkeypatch.setattr(events_publisher, "_client", AsyncMock(return_value=redis))

        # 8 rows claimed across two batches of 4; the gdn-state bookkeeping
        # trio is suppressed, everything else (user-vault trio + both `nodes`)
        # reaches the stream.
        total = 0
        for _ in range(4):
            total += await events_publisher._process_once()
        assert total == 8
        assert len(redis.calls) == 5
        uris = sorted(call[1][b"resource_uri"].decode() for call in redis.calls)
        assert uris == [
            "akb://gdn-state/table/nodes",
            "akb://user-vault/table/gardener_event_log",
            "akb://user-vault/table/gardener_kv",
            "akb://user-vault/table/nodes",
            "akb://user-vault/table/sweep_log",
        ]

        async with pool.acquire() as conn:
            # All eight drained (published), none left pending.
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM events WHERE redis_published_at IS NULL",
            ) == 0

        # A non-rows_changed kind on a suppressed (vault, table) still publishes
        # (the filter is kind + identity, not identity alone).
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO events (vault_id, kind, resource_uri, actor_id, payload) "
                "VALUES ($1, 'document.put', 'akb://gdn-state/table/sweep_log', 'alice', '{}')",
                gdn_vault_id,
            )
        assert await events_publisher._process_once() == 1
        assert len(redis.calls) == 6


async def test_publisher_fanout_reaches_a_real_redis_stream(monkeypatch):
    redis_url = os.environ.get("AKB_TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("AKB_TEST_REDIS_URL not set")

    import redis.asyncio as redis_async

    async with _fresh_database() as (pool, postgres_module):
        await postgres_module._apply_migrations()
        vault_id = await _make_vault(pool)
        table_name = "redis_streamed"
        pg_name = table_data_repo.pg_table_name("rows-changed", table_name)
        async with pool.acquire() as conn:
            await table_data_repo.create_dynamic_table(
                conn,
                pg_name,
                [{"name": "value", "type": "text"}],
                vault_name="rows-changed",
                vault_id=vault_id,
                resource_uri=f"akb://rows-changed/table/{table_name}",
            )

        await UserSqlExecutor(pool).execute(
            user_id="user-id",
            actor_id="alice",
            sql=f"INSERT INTO {pg_name} (value) VALUES ('one')",
            is_admin=True,
            vault_names=["rows-changed"],
        )

        from app.services import events_publisher

        stream = f"akb:test:rows-changed:{uuid.uuid4().hex}"
        client = redis_async.from_url(redis_url, decode_responses=False)
        monkeypatch.setattr(events_publisher.settings, "redis_url", redis_url)
        monkeypatch.setattr(events_publisher.settings, "redis_event_stream", stream)
        monkeypatch.setattr(events_publisher, "get_pool", lambda: pool)
        monkeypatch.setattr(events_publisher, "_client", AsyncMock(return_value=client))
        try:
            assert await events_publisher._process_once() == 1
            messages = await client.xrange(stream)
            assert len(messages) == 1
            _message_id, fields = messages[0]
            assert fields[b"kind"] == b"table.rows_changed"
            assert fields[b"vault_id"] == str(vault_id).encode()
            assert fields[b"resource_uri"] == b"akb://rows-changed/table/redis_streamed"
            assert fields[b"actor_id"] == b"alice"
            assert json.loads(fields[b"payload"]) == {"operation": "insert"}
        finally:
            await client.delete(stream)
            await client.aclose()
