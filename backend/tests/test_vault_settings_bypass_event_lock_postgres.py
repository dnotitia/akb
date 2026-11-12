"""Vault settings changed by an admin override must not hold the event lock early.

`set_public_access` and `update_vault_metadata` check the caller's access on
their own transaction. On a write-managed Vault an unscoped system admin
passes that check by an audited override, and the check used to insert the
`vault.write_policy_admin_bypass` event right there — before the settings
transaction updated the Vault row. The event insert holds the Vault's event
lock until commit (migration 132). A writer that held the Vault row (as the
SQL policy check does, `FOR SHARE`) and then wrote an event deadlocked with
it. The override event is now collected and inserted at the end.
"""

from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest

from app.repositories import vault_write_policy_repo
from app.services import access_service
from tests.test_events_commit_order_postgres import (
    _UNBLOCKED_SECONDS,
    _OpenTransaction,
    _emit,
    _fresh_database,
    _make_vault,
)

pytestmark = pytest.mark.asyncio


class _RoleSync:
    async def on_public_access_change_in_conn(self, conn, vault_id, level) -> None:
        return None


async def _wait_for_row_lock_wait(observer: asyncpg.Connection) -> None:
    async def poll() -> None:
        while not await observer.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_locks AS l JOIN pg_stat_activity AS a "
            "ON a.pid = l.pid WHERE a.datname = current_database() AND NOT l.granted "
            "AND l.locktype IN ('transactionid', 'tuple'))"
        ):
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), timeout=_UNBLOCKED_SECONDS)


@pytest.mark.parametrize("change", ["set_public_access", "update_vault_metadata"])
async def test_admin_override_on_vault_settings_does_not_deadlock_a_vault_writer(
    monkeypatch, change,
):
    monkeypatch.setattr(access_service, "get_role_sync", lambda: _RoleSync())
    async with _fresh_database() as pool:
        name = f"settings-{change.replace('_', '-')}"
        vault_id = await _make_vault(pool, name)
        admin_id = uuid.uuid4()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (id, username, email, password_hash, is_admin) "
                "VALUES ($1, $2, $3, '!fixture!', TRUE)",
                admin_id,
                f"{name}-admin",
                f"{name}-admin@example.com",
            )
            await vault_write_policy_repo.set_policy(vault_id, "collector:test", "tester", conn=conn)

        if change == "set_public_access":
            settings = access_service.set_public_access(str(admin_id), name, "reader")
        else:
            settings = access_service.update_vault_metadata(
                str(admin_id), name, description="changed by an administrator",
            )

        task: asyncio.Task | None = None
        async with pool.acquire() as writer, pool.acquire() as observer:
            try:
                async with _OpenTransaction(writer) as writer_transaction:
                    # Hold the Vault row the way the SQL policy check does,
                    # then write an event of the Vault.
                    await writer.execute("SELECT id FROM vaults WHERE id = $1 FOR SHARE", vault_id)
                    task = asyncio.create_task(settings)
                    await _wait_for_row_lock_wait(observer)
                    writer_event = await asyncio.wait_for(
                        _emit(writer, vault_id, "while-settings-wait"),
                        timeout=_UNBLOCKED_SECONDS,
                    )
                    await writer_transaction.commit()
                await asyncio.wait_for(task, timeout=_UNBLOCKED_SECONDS)
            finally:
                if task is not None and not task.done():
                    task.cancel()
                else:
                    settings.close()

        async with pool.acquire() as conn:
            events = [
                (row["id"], row["kind"])
                for row in await conn.fetch(
                    "SELECT id, kind FROM events WHERE vault_id = $1 ORDER BY id", vault_id,
                )
            ]
            if change == "set_public_access":
                assert await conn.fetchval(
                    "SELECT public_access FROM vaults WHERE id = $1", vault_id,
                ) == "reader"
        expected = ["document.put", "vault.write_policy_admin_bypass"]
        if change == "set_public_access":
            expected.append("vault.public_access")
        assert [kind for _, kind in events] == expected
        assert events[0][0] == writer_event
