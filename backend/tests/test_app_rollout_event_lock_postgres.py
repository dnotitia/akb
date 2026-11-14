"""An app rollout must not hold its Vault's event lock across later steps' DDL.

The rollout worker applies every step of a target in one transaction. Each
`alter_table` step used to insert its `table.alter` event at once, so from the
first step on the worker held the Vault's event lock (migration 132) while
later steps waited for their tables. Every event-writing transaction of the
Vault then waited behind the rollout's DDL. The worker now collects the step
events and inserts them when the target transaction ends, on every exit.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid

import asyncpg
import pytest

from app.repositories import table_data_repo
from app.services import app_resource_service as resources
from app.services import app_rollout_service as rollout
from app.services import app_rollout_worker as worker
from tests.test_events_commit_order_postgres import (
    _UNBLOCKED_SECONDS,
    _OpenTransaction,
    _emit,
    _fresh_database,
)

pytestmark = pytest.mark.asyncio

_VAULT = "rollout-order"
# A same-Vault writer that is not held back finishes in milliseconds.
_WRITER_LIMIT_SECONDS = 2.0


def _table(name: str, columns: list[str]) -> dict:
    # Manifest validation checksums the normalized step, which sorts columns.
    return {
        "name": name,
        "columns": [{"name": column, "type": "text"} for column in sorted(columns)],
        "unique_keys": [],
        "indexes": [],
    }


def _checksum(step: dict) -> str:
    return hashlib.sha256(
        json.dumps(step, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _step(step_id: str, phase: str, operation: str, payload: dict) -> dict:
    step = {"id": step_id, "phase": phase, "operation": operation, "payload": payload}
    step["checksum"] = _checksum(step)
    return step


def _manifest() -> tuple[dict, str, str]:
    current = [_table("orders", ["flag"]), _table("items", ["note"])]
    desired = [_table("orders", ["flag", "extra"]), _table("items", ["note", "extra"])]
    upgrade = [
        _step("add_orders_extra", "expand", "add_column",
              {"table": "orders", "column": {"name": "extra", "type": "text"}}),
        _step("add_items_extra", "expand", "add_column",
              {"table": "items", "column": {"name": "extra", "type": "text"}}),
    ]
    fresh = [
        _step(f"create_{table['name']}", "expand", "create_table", {
            "table": table["name"], "columns": table["columns"], "unique_keys": [], "indexes": [],
        })
        for table in desired
    ]
    current_fingerprint = resources.canonical_table_fingerprint(current)
    manifest = {
        "manifest_version": 2,
        "app_key": "rollout-order",
        "source_revision": "a" * 40,
        "image_digest": "sha256:" + "b" * 64,
        "schema_version": 3,
        "schema": {"tables": desired, "fingerprint": resources.canonical_table_fingerprint(desired)},
        "transition_plans": [
            {"source": "fresh", "steps": fresh},
            {
                "source": {"release_version": "1.0.0", "schema_fingerprint": current_fingerprint},
                "steps": upgrade,
            },
        ],
    }
    return manifest, rollout.manifest_checksum(manifest, version="2.0.0"), current_fingerprint


async def _installation(pool: asyncpg.Pool, *, own_items: bool) -> tuple[uuid.UUID, uuid.UUID, str]:
    """An installed app owning `orders` and `items`, with a pending upgrade.

    Without `own_items` the app loses `items` after the upgrade is requested,
    so the worker's second step fails its ownership check.
    """
    manifest, checksum, current_fingerprint = _manifest()
    async with pool.acquire() as conn:
        app_id = await conn.fetchval(
            "INSERT INTO app_definitions(app_key) VALUES('rollout-order') RETURNING id",
        )
        old_id, new_id = [
            await conn.fetchval(
                "INSERT INTO app_releases(app_id,version,manifest,manifest_checksum) "
                "VALUES($1,$2,$3::jsonb,$4) RETURNING id",
                app_id, version, json.dumps(manifest), checksum,
            )
            for version in ("1.0.0", "2.0.0")
        ]
        vault_id = await conn.fetchval(
            "INSERT INTO vaults(name,git_path) VALUES($1,$2) RETURNING id", _VAULT, f"/tmp/{_VAULT}.git",
        )
        installation_id = await conn.fetchval(
            "INSERT INTO vault_app_installations(app_id,vault_id,desired_release_id,current_release_id,lifecycle) "
            "VALUES($1,$2,$3,$3,'active') RETURNING id",
            app_id, vault_id, old_id,
        )
        await conn.execute(
            "INSERT INTO installation_grants(installation_id,generation,capabilities,issuer) "
            "VALUES($1,1,$2,'test')",
            installation_id, ["rollout:read", "rollout:request"],
        )
        for name, column in (("orders", "flag"), ("items", "note")):
            columns = [{"name": column, "type": "text"}]
            await table_data_repo.create_dynamic_table(
                conn,
                table_data_repo.pg_table_name(_VAULT, name),
                columns,
                vault_name=_VAULT,
                vault_id=vault_id,
                resource_uri=f"akb://{_VAULT}/table/{name}",
            )
            await conn.execute(
                "INSERT INTO vault_tables(id,vault_id,name,columns,unique_keys,indexes) "
                "VALUES($1,$2,$3,$4::jsonb,'[]'::jsonb,'[]'::jsonb)",
                uuid.uuid4(), vault_id, name, json.dumps(columns),
            )
            await conn.execute(
                "INSERT INTO app_owned_resources(installation_id,vault_id,resource_kind,resource_key) "
                "VALUES($1,$2,'table',$3)",
                installation_id, vault_id, name,
            )
        await conn.execute(
            "INSERT INTO app_installation_observed_states(installation_id,app_id,vault_id,observed_generation,"
            "observed_at,observed_release_id,observed_release_version,schema_fingerprint,observed_grant_generation) "
            "VALUES($1,$2,$3,1,NOW(),$4,'1.0.0',$5,1)",
            installation_id, app_id, vault_id, old_id, current_fingerprint,
        )
    requested = await rollout.request_rollout(
        app_id, release_id=new_id, manifest_checksum_value=checksum,
        idempotency_key=str(uuid.uuid4()), requested_by_kind="admin",
        correlation_id="test", actor="test", actor_id="test",
    )
    if not own_items:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE app_owned_resources SET status = 'retained' WHERE resource_key = 'items'",
            )
    return vault_id, uuid.UUID(requested["job_id"]), table_data_repo.pg_table_name(_VAULT, "items")


async def _events(pool: asyncpg.Pool, vault_id: uuid.UUID) -> list[tuple[int, str, str | None]]:
    async with pool.acquire() as conn:
        return [
            (row["id"], row["kind"], row["resource_uri"])
            for row in await conn.fetch(
                "SELECT id, kind, resource_uri FROM events WHERE vault_id = $1 ORDER BY id", vault_id,
            )
        ]


async def _wait_until_relation_lock_wait(observer: asyncpg.Connection, relation: str) -> None:
    async def poll() -> None:
        while not await observer.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE relation = $1::regclass AND NOT granted "
            "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()))",
            relation,
        ):
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), timeout=_UNBLOCKED_SECONDS)


async def test_rollout_events_follow_its_last_ddl_and_do_not_block_vault_writers():
    async with _fresh_database() as pool:
        vault_id, job_id, items = await _installation(pool, own_items=True)
        run: asyncio.Task | None = None
        async with (
            pool.acquire() as holder,
            pool.acquire() as writer,
            pool.acquire() as observer,
        ):
            try:
                async with _OpenTransaction(holder) as holder_transaction:
                    # A writer of `items` holds its table lock, so the rollout
                    # alters `orders` and then waits at its second step.
                    await holder.execute(f"LOCK TABLE {items} IN ROW EXCLUSIVE MODE")
                    run = asyncio.create_task(worker.run_once())
                    await _wait_until_relation_lock_wait(observer, items)

                    started = time.monotonic()
                    try:
                        async with writer.transaction():
                            writer_event = await asyncio.wait_for(
                                _emit(writer, vault_id, "during-rollout"),
                                timeout=_WRITER_LIMIT_SECONDS,
                            )
                    except TimeoutError:
                        pytest.fail(
                            f"a same-Vault writer waited over {_WRITER_LIMIT_SECONDS}s "
                            "behind the rollout's DDL"
                        )
                    waited = time.monotonic() - started
                    # The table writer's own row-change event must not deadlock.
                    await asyncio.wait_for(
                        holder.execute(f"INSERT INTO {items} (note) VALUES ('row')"),
                        timeout=_UNBLOCKED_SECONDS,
                    )
                    await holder_transaction.commit()
                assert await asyncio.wait_for(run, timeout=_UNBLOCKED_SECONDS) == 1
            finally:
                if run is not None and not run.done():
                    await asyncio.wait_for(run, timeout=_UNBLOCKED_SECONDS)

        assert waited < _WRITER_LIMIT_SECONDS
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT status FROM app_rollout_jobs WHERE id = $1", job_id) == "applied"
        events = await _events(pool, vault_id)
        assert events[0][0] == writer_event
        assert [(kind, uri) for _, kind, uri in events] == [
            ("document.put", None),
            ("table.rows_changed", f"akb://{_VAULT}/table/items"),
            ("table.alter", f"akb://{_VAULT}/table/orders"),
            ("table.alter", f"akb://{_VAULT}/table/items"),
        ]


async def test_blocked_rollout_still_records_the_steps_it_applied():
    async with _fresh_database() as pool:
        # The second step fails its ownership check; the first step's DDL and
        # its event commit with the blocked target, the second leaves none.
        vault_id, job_id, items = await _installation(pool, own_items=False)
        assert await worker.run_once() == 1

        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT status FROM app_rollout_jobs WHERE id = $1", job_id) == "blocked"
            orders = table_data_repo.pg_table_name(_VAULT, "orders")
            assert await conn.fetchval(
                "SELECT count(*) FROM information_schema.columns WHERE table_name = $1 AND column_name = 'extra'",
                orders,
            ) == 1
            assert await conn.fetchval(
                "SELECT count(*) FROM information_schema.columns WHERE table_name = $1 AND column_name = 'extra'",
                items,
            ) == 0
        assert [(kind, uri) for _, kind, uri in await _events(pool, vault_id)] == [
            ("table.alter", f"akb://{_VAULT}/table/orders"),
        ]
