"""A multi-level FK cascade must not take the Vault event lock between levels.

A dynamic table's statement-level trigger records `table.rows_changed`. In a
cascading delete, PostgreSQL fires the parent statement's trigger before the
next cascade level runs, because the child delete's own foreign-key actions
are queued after it. If that trigger inserted its event right away, the
transaction would hold the Vault's event lock (migration 132) while it still
waited for grandchild rows; a writer holding such a row and then inserting
its own event would deadlock with it. Row-change events are recorded when
their transaction commits, after every lock it takes.
"""

from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest

from app.repositories import table_data_repo
from tests.test_events_commit_order_postgres import (
    _UNBLOCKED_SECONDS,
    _OpenTransaction,
    _fresh_database,
    _make_vault,
)

pytestmark = pytest.mark.asyncio

_VAULT = "cascade-order"


async def _create(conn: asyncpg.Connection, vault_id: uuid.UUID, name: str, columns: list[dict]) -> str:
    pg_name = table_data_repo.pg_table_name(_VAULT, name)
    await table_data_repo.create_dynamic_table(
        conn,
        pg_name,
        columns,
        vault_name=_VAULT,
        vault_id=vault_id,
        resource_uri=f"akb://{_VAULT}/table/{name}",
    )
    return pg_name


async def _wait_until_blocked_by(observer: asyncpg.Connection, waiter: int, holder: int) -> None:
    async def poll() -> None:
        while not await observer.fetchval("SELECT $2 = ANY(pg_blocking_pids($1))", waiter, holder):
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), timeout=_UNBLOCKED_SECONDS)


async def test_cascade_delete_does_not_deadlock_with_a_grandchild_writer():
    async with _fresh_database() as pool:
        vault_id = await _make_vault(pool, _VAULT)
        async with pool.acquire() as conn:
            parent = await _create(conn, vault_id, "parent", [{"name": "label", "type": "text"}])
            child = await _create(conn, vault_id, "child", [
                {"name": "parent_id", "type": "uuid",
                 "references": {"table": "parent", "on_delete": "cascade"}},
            ])
            grandchild = await _create(conn, vault_id, "grandchild", [
                {"name": "child_id", "type": "uuid",
                 "references": {"table": "child", "on_delete": "cascade"}},
                {"name": "note", "type": "text"},
            ])
            parent_id = await conn.fetchval(f"INSERT INTO {parent} (label) VALUES ('p') RETURNING id")
            child_id = await conn.fetchval(
                f"INSERT INTO {child} (parent_id) VALUES ($1) RETURNING id", parent_id,
            )
            grandchild_id = await conn.fetchval(
                f"INSERT INTO {grandchild} (child_id, note) VALUES ($1, 'g') RETURNING id", child_id,
            )
            baseline = await conn.fetchval("SELECT max(id) FROM events")

        async with (
            pool.acquire() as writer,
            pool.acquire() as deleter,
            pool.acquire() as observer,
        ):
            delete: asyncio.Task | None = None
            try:
                async with _OpenTransaction(writer) as writer_transaction:
                    # A writer holds the grandchild row, as an UPDATE does
                    # before its statement-level trigger runs.
                    await writer.execute(f"SELECT id FROM {grandchild} WHERE id = $1 FOR UPDATE", grandchild_id)

                    async def delete_parent() -> None:
                        async with deleter.transaction():
                            await deleter.execute(f"DELETE FROM {parent} WHERE id = $1", parent_id)

                    delete = asyncio.create_task(delete_parent())
                    await _wait_until_blocked_by(
                        observer, deleter.get_server_pid(), writer.get_server_pid(),
                    )
                    # The writer's own event must not wait for the delete.
                    await asyncio.wait_for(
                        writer.execute(f"UPDATE {grandchild} SET note = 'writer' WHERE id = $1", grandchild_id),
                        timeout=_UNBLOCKED_SECONDS,
                    )
                    await writer_transaction.commit()
                await asyncio.wait_for(delete, timeout=_UNBLOCKED_SECONDS)
            finally:
                if delete is not None and not delete.done():
                    delete.cancel()

        async with pool.acquire() as conn:
            for table, row_id in ((parent, parent_id), (child, child_id), (grandchild, grandchild_id)):
                assert await conn.fetchval(f"SELECT count(*) FROM {table} WHERE id = $1", row_id) == 0
            events = [
                (row["kind"], row["resource_uri"], row["operation"])
                for row in await conn.fetch(
                    "SELECT kind, resource_uri, payload->>'operation' AS operation FROM events "
                    "WHERE vault_id = $1 AND id > $2 ORDER BY id",
                    vault_id,
                    baseline,
                )
            ]
        assert events[0] == ("table.rows_changed", f"akb://{_VAULT}/table/grandchild", "update")
        assert sorted(events[1:]) == [
            ("table.rows_changed", f"akb://{_VAULT}/table/{name}", "delete")
            for name in ("child", "grandchild", "parent")
        ]
