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
from app.services import table_migration_service, table_service
from tests.test_events_commit_order_postgres import (
    _UNBLOCKED_SECONDS,
    _OpenTransaction,
    _fresh_database,
    _make_vault,
)

pytestmark = pytest.mark.asyncio

_VAULT = "order-migration"


async def _create_table(
    pool: asyncpg.Pool, vault_id: uuid.UUID, name: str, columns: list[dict] | None = None,
) -> str:
    pg_name = table_data_repo.pg_table_name(_VAULT, name)
    columns = columns or [{"name": "value", "type": "text"}]
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
            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE relation = $1::regclass AND NOT granted "
            "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()))",
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


async def test_enum_rename_in_a_migration_does_not_hold_the_event_lock():
    """An enum value rename rewrites rows, and the table's row-change trigger
    used to insert its event at that UPDATE — mid-migration, before the later
    operations' DDL. Row-change events are now inserted at commit."""
    async with _fresh_database() as pool:
        vault_id = await _make_vault(pool, _VAULT)
        alpha = await _create_table(
            pool, vault_id, "alpha", [{"name": "state", "type": "enum", "enum": ["draft", "done"]}],
        )
        beta = await _create_table(pool, vault_id, "beta")
        async with pool.acquire() as conn:
            await conn.execute(f"INSERT INTO {alpha} (state) VALUES ('draft'), ('done')")
            baseline = await conn.fetchval("SELECT max(id) FROM events")
            before = await conn.fetchrow(f"SELECT row_commit, updated_at FROM {alpha} WHERE state = 'draft'")

        async with pool.acquire() as writer, pool.acquire() as observer:
            migration: asyncio.Task[dict] | None = None
            try:
                async with _OpenTransaction(writer) as writer_transaction:
                    await writer.execute(f"LOCK TABLE {beta} IN ROW EXCLUSIVE MODE")
                    migration = asyncio.create_task(
                        table_migration_service.apply_table_migration(
                            vault_id,
                            actor_id="tester",
                            idempotency_key=str(uuid.uuid4()),
                            operations=[
                                {
                                    "op": "alter_column",
                                    "table": "alpha",
                                    "name": "state",
                                    "enum": ["todo", "done"],
                                    "rename_enum_values": {"draft": "todo"},
                                },
                                {"op": "add_column", "table": "beta", "name": "extra", "type": "text"},
                            ],
                        )
                    )
                    # The rename is applied; the migration now waits for beta.
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
            assert sorted(
                row["state"] for row in await conn.fetch(f"SELECT state FROM {alpha}")
            ) == ["done", "todo"]
            after = await conn.fetchrow(f"SELECT row_commit, updated_at FROM {alpha} WHERE state = 'todo'")
            # The rename still runs the row triggers that version a row.
            assert after["row_commit"] != before["row_commit"]
            assert after["updated_at"] >= before["updated_at"]
            assert await conn.fetchval(
                "SELECT tgenabled::text FROM pg_trigger WHERE tgrelid = $1::regclass "
                "AND tgname = 'akb_rows_changed_update_trigger'",
                alpha,
            ) == "O"
            events = [
                (row["kind"], row["resource_uri"], row["actor_id"], row["payload"])
                for row in await conn.fetch(
                    "SELECT kind, resource_uri, actor_id, payload::text AS payload FROM events "
                    "WHERE vault_id = $1 AND id > $2 ORDER BY id",
                    vault_id,
                    baseline,
                )
            ]
        # The writer committed first. The migration's own events follow its
        # last DDL, and the rename's row-change event is inserted at commit.
        assert [(kind, uri) for kind, uri, _, _ in events] == [
            ("table.rows_changed", f"akb://{_VAULT}/table/beta"),
            ("table.alter", f"akb://{_VAULT}/table/alpha"),
            ("table.alter", f"akb://{_VAULT}/table/beta"),
            ("table.migration", f"akb://{_VAULT}"),
            ("table.rows_changed", f"akb://{_VAULT}/table/alpha"),
        ]
        assert events[-1][2] is None
        assert json.loads(events[-1][3]) == {"operation": "update"}


async def test_standalone_enum_rename_records_the_row_change_like_the_trigger():
    async with _fresh_database() as pool:
        vault_id = await _make_vault(pool, _VAULT)
        alpha = await _create_table(
            pool, vault_id, "alpha", [{"name": "state", "type": "enum", "enum": ["draft", "done", "old"]}],
        )
        async with pool.acquire() as conn:
            await conn.execute(f"INSERT INTO {alpha} (state) VALUES ('draft')")
            baseline = await conn.fetchval("SELECT max(id) FROM events")

        async def rename(enum: list[str], renames: dict[str, str]) -> list[tuple[str, str]]:
            await table_service.alter_table(
                vault_id,
                "alpha",
                actor_id="tester",
                alter_columns=[{"name": "state", "enum": enum, "rename_enum_values": renames}],
            )
            async with pool.acquire() as conn:
                return [
                    (row["kind"], row["payload"])
                    for row in await conn.fetch(
                        "SELECT kind, payload::text AS payload FROM events "
                        "WHERE vault_id = $1 AND id > $2 ORDER BY id",
                        vault_id,
                        baseline,
                    )
                ]

        changed = await rename(["todo", "done", "old"], {"draft": "todo"})
        assert [kind for kind, _ in changed] == ["table.alter", "table.rows_changed"]
        assert json.loads(changed[1][1]) == {"operation": "update"}
        # A rename that matches no row changes none, so no row-change event.
        unchanged = await rename(["todo", "done", "retired"], {"old": "retired"})
        assert [kind for kind, _ in unchanged[2:]] == ["table.alter"]
