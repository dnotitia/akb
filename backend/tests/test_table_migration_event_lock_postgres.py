"""A multi-table migration must not hold the Vault event lock across its DDL.

An event insert holds its Vault's event lock until commit (migration 132). A
table migration used to insert each operation's `table.alter` event right after
that operation, so from the first operation on it held the lock while later
operations waited for their tables. A writer of such a table, which took its
table lock first and inserts its row-change event at the end of its statement,
then waited for the migration while the migration waited for it: a deadlock
that PostgreSQL broke by rolling one of them back. The migration now writes its
events after its last operation.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import asyncpg
import pytest

from app.repositories import table_data_repo
from app.services import table_migration_service
from tests.test_events_commit_order_postgres import (
    _UNBLOCKED_SECONDS,
    _OpenTransaction,
    _fresh_database,
    _make_vault,
)

pytestmark = pytest.mark.asyncio

_VAULT = "order-migration"


async def _create_table(pool: asyncpg.Pool, vault_id: uuid.UUID, name: str) -> str:
    pg_name = table_data_repo.pg_table_name(_VAULT, name)
    columns = [{"name": "value", "type": "text"}]
    async with pool.acquire() as conn:
        await table_data_repo.create_dynamic_table(
            conn,
            pg_name,
            columns,
            vault_name=_VAULT,
            vault_id=vault_id,
            resource_uri=f"akb://{_VAULT}/table/{name}",
        )
        await conn.execute(
            "INSERT INTO vault_tables (id, vault_id, name, description, columns, unique_keys, indexes) "
            "VALUES ($1, $2, $3, '', $4::jsonb, '[]'::jsonb, '[]'::jsonb)",
            uuid.uuid4(),
            vault_id,
            name,
            json.dumps(columns),
        )
    return pg_name


async def _wait_until_lock_wait(observer: asyncpg.Connection, relation: str) -> None:
    async def waiting() -> None:
        while not await observer.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE relation = $1::regclass AND NOT granted)",
            relation,
        ):
            await asyncio.sleep(0.02)

    await asyncio.wait_for(waiting(), timeout=_UNBLOCKED_SECONDS)


async def test_migration_does_not_hold_the_event_lock_while_waiting_for_a_table():
    async with _fresh_database() as pool:
        vault_id = await _make_vault(pool, _VAULT)
        await _create_table(pool, vault_id, "alpha")
        beta = await _create_table(pool, vault_id, "beta")

        async with pool.acquire() as writer, pool.acquire() as observer:
            migration: asyncio.Task[dict] | None = None
            try:
                async with _OpenTransaction(writer) as writer_transaction:
                    # A writing statement takes its table lock when it starts and
                    # inserts its row-change event when it ends.
                    await writer.execute(f"LOCK TABLE {beta} IN ROW EXCLUSIVE MODE")
                    migration = asyncio.create_task(
                        table_migration_service.apply_table_migration(
                            vault_id,
                            actor_id="tester",
                            idempotency_key=str(uuid.uuid4()),
                            operations=[
                                {"op": "add_column", "table": "alpha", "name": "extra", "type": "text"},
                                {"op": "add_column", "table": "beta", "name": "extra", "type": "text"},
                            ],
                        )
                    )
                    # The migration has altered alpha and now waits for beta.
                    await _wait_until_lock_wait(observer, beta)
                    await asyncio.wait_for(
                        writer.execute(f"INSERT INTO {beta} (value) VALUES ('row')"),
                        timeout=_UNBLOCKED_SECONDS,
                    )
                    await writer_transaction.commit()
                result = await asyncio.wait_for(migration, timeout=_UNBLOCKED_SECONDS)
            finally:
                if migration is not None and not migration.done():
                    migration.cancel()

        assert result["applied"] is True
        async with pool.acquire() as conn:
            events = [
                (row["kind"], row["resource_uri"])
                for row in await conn.fetch(
                    "SELECT kind, resource_uri FROM events WHERE vault_id = $1 ORDER BY id",
                    vault_id,
                )
            ]
        assert events == [
            ("table.rows_changed", f"akb://{_VAULT}/table/beta"),
            ("table.alter", f"akb://{_VAULT}/table/alpha"),
            ("table.alter", f"akb://{_VAULT}/table/beta"),
            ("table.migration", f"akb://{_VAULT}"),
        ]
