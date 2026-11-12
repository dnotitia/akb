"""An admin's `akb_sql` override must not hold the Vault event lock while it runs.

An unscoped human administrator may write a write-managed Vault through SQL;
each such statement records a `vault.write_policy_admin_bypass` event. That
event used to be inserted before the statement ran. Its insert takes the
Vault's event lock until commit (migration 132), so for the whole statement
every other event-writing transaction of the Vault waited, and a writer that
already held a row the statement needed deadlocked with it. The event is now
inserted after the statement, and it still records an attempt PostgreSQL
rejected.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import asyncpg
import pytest

from app.repositories import table_data_repo, vault_write_policy_repo
from app.services.user_sql_executor import PermissionDeniedError, UserSqlExecutor
from tests.test_events_commit_order_postgres import (
    _UNBLOCKED_SECONDS,
    _OpenTransaction,
    _emit,
    _fresh_database,
    _make_vault,
)

pytestmark = pytest.mark.asyncio

_BYPASS = "vault.write_policy_admin_bypass"
_SLOW_SECONDS = 1.5


async def _managed_vault(pool: asyncpg.Pool, name: str) -> tuple[uuid.UUID, uuid.UUID, str]:
    """A write-managed Vault, one table with one row, and a live human admin."""
    vault_id = await _make_vault(pool, name)
    admin_id = uuid.uuid4()
    pg_name = table_data_repo.pg_table_name(name, "items")
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, username, email, password_hash, is_admin) "
            "VALUES ($1, $2, $3, '!fixture!', TRUE)",
            admin_id,
            f"{name}-admin",
            f"{name}-admin@example.com",
        )
        await vault_write_policy_repo.set_policy(vault_id, "collector:test", "tester", conn=conn)
        await table_data_repo.create_dynamic_table(
            conn,
            pg_name,
            [{"name": "value", "type": "text"}],
            vault_name=name,
            vault_id=vault_id,
            resource_uri=f"akb://{name}/table/items",
        )
        await conn.execute(f"INSERT INTO {pg_name} (value) VALUES ('seed')")
    return vault_id, admin_id, pg_name


async def _admin_sql(pool: asyncpg.Pool, admin_id: uuid.UUID, vault: str, sql: str, **kwargs):
    return await UserSqlExecutor(pool).execute(
        user_id=str(admin_id),
        actor_id="admin",
        sql=sql,
        is_admin=True,
        vault_names=[vault],
        enforce_vault_write_policy=True,
        **kwargs,
    )


async def _vault_events(
    pool: asyncpg.Pool, vault_id: uuid.UUID, after_id: int = 0,
) -> list[tuple[int, str]]:
    async with pool.acquire() as conn:
        return [
            (row["id"], row["kind"])
            for row in await conn.fetch(
                "SELECT id, kind FROM events WHERE vault_id = $1 AND id > $2 ORDER BY id",
                vault_id,
                after_id,
            )
        ]


async def _last_event_id(pool: asyncpg.Pool) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT coalesce(max(id), 0) FROM events")


async def _wait_until(observer: asyncpg.Connection, sql: str, *args: object) -> None:
    async def poll() -> None:
        while not await observer.fetchval(sql, *args):
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), timeout=_UNBLOCKED_SECONDS)


async def test_running_override_statement_does_not_block_other_vault_writers():
    async with _fresh_database() as pool:
        name = "bypass-slow"
        vault_id, admin_id, pg_name = await _managed_vault(pool, name)
        baseline = await _last_event_id(pool)
        slow = asyncio.create_task(
            _admin_sql(
                pool, admin_id, name,
                f"UPDATE {pg_name} SET value = 'slow' FROM pg_sleep({_SLOW_SECONDS}) AS pause",
            )
        )
        try:
            async with pool.acquire() as observer, pool.acquire() as writer:
                await _wait_until(
                    observer,
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                    "WHERE datname = current_database() AND state = 'active' "
                    "AND query LIKE '%pg_sleep%' AND pid <> pg_backend_pid())",
                )
                started = time.monotonic()
                async with writer.transaction():
                    writer_event = await _emit(writer, vault_id, "while-override-runs")
                waited = time.monotonic() - started
            result = await asyncio.wait_for(slow, timeout=_UNBLOCKED_SECONDS)
        finally:
            if not slow.done():
                slow.cancel()

        assert waited < _SLOW_SECONDS / 2, (
            f"a same-Vault writer waited {waited:.2f}s behind the override statement"
        )
        assert result["affected_rows"] == 1
        events = await _vault_events(pool, vault_id, baseline)
        assert events[0] == (writer_event, "document.put")
        assert [kind for _, kind in events[1:]] == ["table.rows_changed", _BYPASS]


async def test_override_waiting_for_a_row_does_not_deadlock_with_its_holder():
    async with _fresh_database() as pool:
        name = "bypass-row"
        vault_id, admin_id, pg_name = await _managed_vault(pool, name)
        baseline = await _last_event_id(pool)
        override: asyncio.Task | None = None
        async with pool.acquire() as holder, pool.acquire() as observer:
            try:
                async with _OpenTransaction(holder) as holder_transaction:
                    # A writer holds the row first and writes it a moment later.
                    await holder.execute(f"SELECT id FROM {pg_name} FOR UPDATE")
                    override = asyncio.create_task(
                        _admin_sql(pool, admin_id, name, f"UPDATE {pg_name} SET value = 'admin'")
                    )
                    await _wait_until(
                        observer,
                        "SELECT EXISTS (SELECT 1 FROM pg_locks AS l JOIN pg_stat_activity AS a "
                        "ON a.pid = l.pid WHERE a.datname = current_database() "
                        "AND l.locktype = 'transactionid' AND NOT l.granted)",
                    )
                    await asyncio.wait_for(
                        holder.execute(f"UPDATE {pg_name} SET value = 'holder'"),
                        timeout=_UNBLOCKED_SECONDS,
                    )
                    await holder_transaction.commit()
                result = await asyncio.wait_for(override, timeout=_UNBLOCKED_SECONDS)
            finally:
                if override is not None and not override.done():
                    override.cancel()

        assert result["affected_rows"] == 1
        async with pool.acquire() as conn:
            assert await conn.fetchval(f"SELECT value FROM {pg_name}") == "admin"
        assert [kind for _, kind in await _vault_events(pool, vault_id, baseline)] == [
            "table.rows_changed",  # the holder's write, committed first
            "table.rows_changed",
            _BYPASS,
        ]


async def test_rejected_override_statement_still_records_the_override():
    async with _fresh_database() as pool:
        name = "bypass-rejected"
        vault_id, admin_id, pg_name = await _managed_vault(pool, name)
        async with pool.acquire() as conn:
            await conn.execute(f"ALTER TABLE {pg_name} ADD CONSTRAINT value_not_rejected CHECK (value <> 'rejected')")

        with pytest.raises(asyncpg.CheckViolationError):
            await _admin_sql(pool, admin_id, name, f"UPDATE {pg_name} SET value = 'rejected'")
        # A read-only call cannot write the event in its own transaction.
        with pytest.raises(PermissionDeniedError) as read_only:
            await _admin_sql(pool, admin_id, name, f"UPDATE {pg_name} SET value = 'blocked'", read_only=True)

        assert read_only.value.pg_sqlstate == "25006"
        async with pool.acquire() as conn:
            assert await conn.fetchval(f"SELECT value FROM {pg_name}") == "seed"
            bypasses = await conn.fetch(
                "SELECT actor_id, payload->>'vault' AS vault FROM events WHERE vault_id = $1 AND kind = $2",
                vault_id,
                _BYPASS,
            )
        assert [(row["actor_id"], row["vault"]) for row in bypasses] == [
            (str(admin_id), name),
            (str(admin_id), name),
        ]
