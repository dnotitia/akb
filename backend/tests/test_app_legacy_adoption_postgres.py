"""Live PostgreSQL proof for metadata-only legacy adoption and table fencing."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
import uuid

import asyncpg
import pytest

from app.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.services import access_service
from app.services import app_identity_service
from app.services import app_installation_service as installation
from app.services import app_legacy_adoption_service as adoption
from app.services import app_inventory_service as inventory
from app.services import app_rollout_service as rollout
from app.services import table_migration_service
from app.services import table_service
from app.services.app_resource_service import canonical_table_fingerprint
from app.services.auth_service import AuthenticatedUser
from app.repositories.table_data_repo import pg_table_name

pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[1]
_INIT_SQL = (_BACKEND / "app" / "db" / "init.sql").read_text()
_MIGRATIONS = [
    _BACKEND / "app" / "db" / "migrations" / "042_vault_migrations.py",
    _BACKEND / "app" / "db" / "migrations" / "044_vault_write_policy.py",
    _BACKEND / "app" / "db" / "migrations" / "047_app_registry.py",
    _BACKEND / "app" / "db" / "migrations" / "052_app_inventory.py",
    _BACKEND / "app" / "db" / "migrations" / "077_legacy_adoptions.py",
    _BACKEND / "app" / "db" / "migrations" / "095_app_release_manifest_v2.py",
    _BACKEND / "app" / "db" / "migrations" / "115_legacy_adoption_history.py",
]
_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",  # pragma: allowlist secret
)


async def _can_connect() -> bool:
    try:
        conn = await asyncpg.connect(_DSN, timeout=2)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


def _database_dsn(name: str) -> str:
    base, _ = _DSN.rsplit("/", 1)
    return f"{base}/{name}"


def _load_migration(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@asynccontextmanager
async def _fresh_database(*, history_migration=True):
    if not await _can_connect():
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"Required PostgreSQL is not reachable at {_DSN}")
        pytest.skip(f"Postgres not reachable at {_DSN}")
    admin = await asyncpg.connect(_DSN)
    name = f"akb_legacy_adoption_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    pool = await asyncpg.create_pool(_database_dsn(name), min_size=1, max_size=8)
    try:
        async with pool.acquire() as conn:
            await conn.execute(_INIT_SQL)
            for path in _MIGRATIONS:
                if not history_migration and path.name == "115_legacy_adoption_history.py":
                    continue
                await _load_migration(path).migrate(conn=conn)
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


def _admin() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=str(uuid.uuid4()),
        username="system-operator",
        email="operator@example.invalid",
        display_name=None,
        is_admin=True,
        auth_method="jwt",
    )


async def _fixture(pool, *, label: str):
    app_id = uuid.uuid4()
    vault_id = uuid.uuid4()
    table_id = uuid.uuid4()
    table_name = "orders"
    vault_name = f"legacy-{label}-{uuid.uuid4().hex[:8]}"
    columns = [{"name": "amount", "type": "numeric"}]
    descriptor = {
        "name": table_name,
        "columns": columns,
        "unique_keys": [],
        "indexes": [],
    }
    actual_fingerprint = canonical_table_fingerprint([descriptor])
    app_key = f"legacy-{label}-{uuid.uuid4().hex}"
    create_step = {
        "id": "create_orders",
        "phase": "expand",
        "operation": "create_table",
        "payload": {
            "table": table_name,
            "columns": columns,
            "unique_keys": [],
            "indexes": [],
        },
    }
    create_step["checksum"] = hashlib.sha256(
        json.dumps(create_step, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    manifest_body = {
        "manifest_version": 2,
        "app_key": app_key,
        "source_revision": "a" * 40,
        "image_digest": "sha256:" + "b" * 64,
        "schema_version": 3,
        "schema": {"tables": [descriptor]},
        "transition_plans": [{"source": "fresh", "steps": [create_step]}],
    }
    checksum = rollout.manifest_checksum(manifest_body, version="1.0.0")
    normalized_manifest = rollout.validate_manifest(
        manifest_body, checksum, version="1.0.0"
    )
    encoded = json.dumps(
        rollout.manifest_storage_projection(normalized_manifest),
        separators=(",", ":"),
        ensure_ascii=False,
    )
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO app_definitions(id, app_key) VALUES($1, $2)",
            app_id,
            app_key,
        )
        release_id = await conn.fetchval(
            """
            INSERT INTO app_releases(app_id, version, manifest, manifest_checksum)
            VALUES($1, '1.0.0', $2::jsonb, $3)
            RETURNING id
            """,
            app_id,
            encoded,
            checksum,
        )
        await conn.execute(
            "INSERT INTO vaults(id, name, git_path) VALUES($1, $2, $3)",
            vault_id,
            vault_name,
            f"/tmp/{vault_name}.git",
        )
        physical = pg_table_name(vault_name, table_name)
        await conn.execute(
            f"""
            CREATE TABLE {physical} (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                amount NUMERIC,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        await conn.execute(
            """
            INSERT INTO vault_tables(
                id, vault_id, name, description, columns, unique_keys, indexes, created_by
            ) VALUES($1, $2, $3, '', $4::jsonb, '[]'::jsonb, '[]'::jsonb, 'fixture')
            """,
            table_id,
            vault_id,
            table_name,
            json.dumps(columns, separators=(",", ":")),
        )
        row_id = await conn.fetchval(
            f"INSERT INTO {physical}(amount) VALUES(10) RETURNING id"
        )
    return {
        "app_id": app_id,
        "release_id": release_id,
        "vault_id": vault_id,
        "vault_name": vault_name,
        "table_name": table_name,
        "physical": physical,
        "row_id": row_id,
        "fingerprint": actual_fingerprint,
        "expected": actual_fingerprint,
    }


async def _insert_vault_without_table(pool, *, label: str) -> tuple[uuid.UUID, str]:
    vault_id = uuid.uuid4()
    vault_name = f"legacy-{label}-{uuid.uuid4().hex[:8]}"
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO vaults(id, name, git_path) VALUES($1, $2, $3)",
            vault_id,
            vault_name,
            f"/tmp/{vault_name}.git",
        )
    return vault_id, vault_name


async def _insert_orders_table(
    pool,
    *,
    vault_id: uuid.UUID,
    vault_name: str,
    table_name: str = "orders",
) -> str:
    columns = [{"name": "amount", "type": "numeric"}]
    physical = pg_table_name(vault_name, table_name)
    async with pool.acquire() as conn:
        await conn.execute(
            f"""
            CREATE TABLE {physical} (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                amount NUMERIC,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        await conn.execute(
            """
            INSERT INTO vault_tables(
                id, vault_id, name, description, columns, unique_keys, indexes, created_by
            ) VALUES($1, $2, $3, '', $4::jsonb, '[]'::jsonb, '[]'::jsonb, 'fixture')
            """,
            uuid.uuid4(),
            vault_id,
            table_name,
            json.dumps(columns, separators=(",", ":")),
        )
    return canonical_table_fingerprint(
        [{"name": table_name, "columns": columns, "unique_keys": [], "indexes": []}]
    )


@pytest.mark.parametrize("state", ["planned", "applied", "blocked"])
async def test_vault_deletion_retains_adoption_history_without_replay(monkeypatch, state):
    async with _fresh_database() as pool:
        monkeypatch.setattr(adoption, "get_pool", lambda: pool)
        fixture = await _fixture(pool, label=f"delete-{state}")
        user = _admin()
        targets = [{"vault_id": str(fixture["vault_id"]), "table_allowlist": ["orders"]}]
        if state == "blocked":
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM vault_tables WHERE vault_id=$1", fixture["vault_id"])
        key = str(uuid.uuid4())
        kwargs = dict(
            baseline_release_id=fixture["release_id"], idempotency_key=key,
            targets=targets, user=user, correlation_id="delete-history",
        )
        plan = await adoption.create_legacy_adoption(fixture["app_id"], **kwargs)
        adoption_id = uuid.UUID(plan["adoption_id"])
        if state == "applied":
            plan = await adoption.apply_legacy_adoption(
                fixture["app_id"], adoption_id, user=user, correlation_id="before-delete",
            )
        assert plan["targets"][0]["state"] == state
        async with pool.acquire() as conn:
            before = await conn.fetch("SELECT * FROM app_legacy_adoption_audit ORDER BY id")
            # The production delete service locks and removes this same parent;
            # its table/object cleanup is exercised separately through HTTP.
            await conn.execute("DELETE FROM vaults WHERE id=$1", fixture["vault_id"])
            assert await conn.fetch("SELECT * FROM app_legacy_adoption_audit ORDER BY id") == before
            assert await conn.fetchval("SELECT count(*) FROM vault_app_installations") == 0
        retained = await adoption.get_legacy_adoption(
            fixture["app_id"], adoption_id, user=user, correlation_id="read-history",
        )
        assert retained["input_digest"] == plan["input_digest"]
        assert retained["targets"][0] == {**plan["targets"][0], "vault_deleted": True}
        for _ in range(2):
            with pytest.raises(ConflictError, match="deleted.*new adoption plan"):
                await adoption.apply_legacy_adoption(
                    fixture["app_id"], adoption_id, user=user, correlation_id="stale-apply",
                )
            with pytest.raises(ConflictError, match="deleted.*new adoption plan"):
                await adoption.create_legacy_adoption(fixture["app_id"], **kwargs)
        async with pool.acquire() as conn:
            assert await conn.fetch("SELECT * FROM app_legacy_adoption_audit ORDER BY id") == before
            # The original IDs must never be rebound to a same-named new Vault.
            new_id = await conn.fetchval(
                "INSERT INTO vaults(name,git_path) VALUES($1,$2) RETURNING id",
                fixture["vault_name"], f"/tmp/{fixture['vault_name']}.git",
            )
            assert new_id != fixture["vault_id"]
        again = await adoption.get_legacy_adoption(
            fixture["app_id"], adoption_id, user=user, correlation_id="same-name-history",
        )
        assert again["targets"] == retained["targets"]
        with pytest.raises(NotFoundError):
            await adoption.get_legacy_adoption(
                fixture["app_id"], adoption_id,
                user=AuthenticatedUser(
                    user_id=str(uuid.uuid4()), username="former-member",
                    email="former@example.invalid", display_name=None,
                    is_admin=False, auth_method="jwt",
                ), correlation_id="history-not-public",
            )


async def test_history_upgrade_preserves_rows_and_validates_new_live_references(monkeypatch):
    async with _fresh_database(history_migration=False) as pool:
        monkeypatch.setattr(adoption, "get_pool", lambda: pool)
        fixture = await _fixture(pool, label="history-upgrade")
        other = await _fixture(pool, label="history-other")
        plans = []
        for item in (fixture, other):
            plan = await adoption.create_legacy_adoption(
                item["app_id"], baseline_release_id=item["release_id"],
                idempotency_key=str(uuid.uuid4()), user=_admin(), correlation_id="upgrade",
                targets=[{"vault_id": str(item["vault_id"]), "table_allowlist": ["orders"]}],
            )
            plans.append(await adoption.apply_legacy_adoption(
                item["app_id"], plan["adoption_id"], user=_admin(), correlation_id="upgrade",
            ))
        async with pool.acquire() as conn:
            before = {table: await conn.fetch(f"SELECT * FROM {table} ORDER BY id") for table in (
                "app_legacy_adoption_plans", "app_legacy_adoption_targets", "app_legacy_adoption_audit",
            )}
            for _ in range(2):
                await _load_migration(_MIGRATIONS[-1]).migrate(conn=conn)
            for table, rows in before.items():
                assert await conn.fetch(f"SELECT * FROM {table} ORDER BY id") == rows
            aid = uuid.UUID(plans[0]["adoption_id"])
            tid = uuid.UUID(plans[0]["targets"][0]["target_id"])
            iid = uuid.UUID(plans[0]["targets"][0]["installation_id"])
            other_iid = uuid.UUID(plans[1]["targets"][0]["installation_id"])

            async def append(vault_id, installation_id):
                await adoption._record_ledger_audit(
                    conn, app_id=fixture["app_id"], adoption_id=aid, target_id=tid,
                    installation_id=installation_id, vault_id=vault_id,
                    release_id=fixture["release_id"], action="target_blocked", outcome="error",
                    reason_code="ownership_conflict", correlation_id="new-reference",
                )

            for vid, install in ((uuid.uuid4(), None), (fixture["vault_id"], uuid.uuid4()),
                                 (other["vault_id"], iid), (None, uuid.uuid4())):
                with pytest.raises(asyncpg.ForeignKeyViolationError):
                    await append(vid, install)
            # Preserve nullable audit references and cross-app conflict evidence.
            await append(None, iid)
            await append(other["vault_id"], other_iid)
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute(
                    "UPDATE app_legacy_adoption_targets SET installation_id=$2 WHERE id=$1",
                    tid, other_iid,
                )
            for query in (
                "UPDATE app_legacy_adoption_targets SET vault_id=uuid_generate_v4()",
                "DELETE FROM app_legacy_adoption_targets", "DELETE FROM app_legacy_adoption_plans",
                "DELETE FROM app_legacy_adoption_audit", "UPDATE app_legacy_adoption_audit SET outcome='ok'",
            ):
                with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
                    await conn.execute(query)
            await conn.execute("DELETE FROM vaults WHERE id=ANY($1::uuid[])",
                               [fixture["vault_id"], other["vault_id"]])
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await append(fixture["vault_id"], iid)


@pytest.mark.parametrize("operation", ["apply", "create_replay", "fresh_create"])
async def test_vault_delete_serializes_with_adoption_writes(monkeypatch, operation):
    async with _fresh_database() as pool:
        monkeypatch.setattr(adoption, "get_pool", lambda: pool)
        fixture = await _fixture(pool, label="delete-race")
        args = dict(
            baseline_release_id=fixture["release_id"], idempotency_key=str(uuid.uuid4()),
            targets=[{"vault_id": str(fixture["vault_id"]), "table_allowlist": ["orders"]}],
            user=_admin(), correlation_id="delete-race",
        )
        plan = None if operation == "fresh_create" else await adoption.create_legacy_adoption(
            fixture["app_id"], **args,
        )
        async with pool.acquire() as deleting:
            async with deleting.transaction():
                await deleting.execute("SELECT id FROM vaults WHERE id=$1 FOR UPDATE", fixture["vault_id"])
                if operation == "apply":
                    assert plan is not None
                    task = asyncio.create_task(adoption.apply_legacy_adoption(
                        fixture["app_id"], plan["adoption_id"], user=_admin(), correlation_id="race-apply",
                    ))
                else:
                    task = asyncio.create_task(adoption.create_legacy_adoption(fixture["app_id"], **args))
                try:
                    async with asyncio.timeout(5):
                        while True:
                            async with pool.acquire() as observer:
                                waiting = await observer.fetchval(
                                    "SELECT EXISTS(SELECT FROM pg_stat_activity WHERE datname=current_database() "
                                    "AND $1::int=ANY(pg_blocking_pids(pid)))", deleting.get_server_pid(),
                                )
                            if waiting:
                                break
                            await asyncio.sleep(0.01)
                    assert not task.done()
                    await deleting.execute("DELETE FROM vaults WHERE id=$1", fixture["vault_id"])
                except BaseException:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise
            expected = NotFoundError if operation == "fresh_create" else ConflictError
            with pytest.raises(expected):
                await asyncio.wait_for(task, timeout=5)
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM vault_app_installations") == 0
            assert await conn.fetchval("SELECT count(*) FROM app_legacy_adoption_plans") == (
                0 if operation == "fresh_create" else 1
            )


@pytest.mark.parametrize("operation", ["apply", "create_replay", "fresh_create"])
async def test_adoption_holds_parent_until_commit_then_allows_delete(monkeypatch, operation):
    async with _fresh_database() as pool:
        monkeypatch.setattr(adoption, "get_pool", lambda: pool)
        fixture = await _fixture(pool, label="adopt-first")
        user = _admin()
        args = dict(
            baseline_release_id=fixture["release_id"], idempotency_key=str(uuid.uuid4()),
            targets=[{"vault_id": str(fixture["vault_id"]), "table_allowlist": ["orders"]}],
            user=user, correlation_id="adopt-first",
        )
        plan = None if operation == "fresh_create" else await adoption.create_legacy_adoption(
            fixture["app_id"], **args,
        )
        ready, release = asyncio.Event(), asyncio.Event()
        writer_pid = None
        record = adoption._record_ledger_audit

        async def pause_before_commit(conn, **kwargs):
            nonlocal writer_pid
            await record(conn, **kwargs)
            if kwargs["action"] in {"plan_created", "plan_replayed", "target_applied"}:
                writer_pid = conn.get_server_pid()
                ready.set()
                await release.wait()

        monkeypatch.setattr(adoption, "_record_ledger_audit", pause_before_commit)
        if operation == "apply":
            assert plan is not None
            call = adoption.apply_legacy_adoption(
                fixture["app_id"], plan["adoption_id"], user=user, correlation_id="adopt-first",
            )
        else:
            call = adoption.create_legacy_adoption(fixture["app_id"], **args)
        writing = asyncio.create_task(call)
        deleting = None
        try:
            await asyncio.wait_for(ready.wait(), timeout=5)
            async with pool.acquire() as delete_conn:
                deleting = asyncio.create_task(delete_conn.execute(
                    "DELETE FROM vaults WHERE id=$1", fixture["vault_id"],
                ))
                async with asyncio.timeout(5):
                    while True:
                        async with pool.acquire() as observer:
                            blocked = await observer.fetchval(
                                "SELECT $1::int=ANY(pg_blocking_pids($2))",
                                writer_pid, delete_conn.get_server_pid(),
                            )
                        if blocked:
                            break
                        await asyncio.sleep(0.01)
                assert not deleting.done()
                release.set()
                result, _ = await asyncio.wait_for(asyncio.gather(writing, deleting), timeout=5)
        finally:
            release.set()
            tasks = [task for task in (writing, deleting) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        retained = await adoption.get_legacy_adoption(
            fixture["app_id"], result["adoption_id"], user=user, correlation_id="after-delete",
        )
        assert retained["targets"][0]["vault_deleted"] is True
        assert retained["input_digest"] == result["input_digest"]
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM vault_app_installations") == 0


async def test_mixed_deleted_targets_preserve_per_target_progress(monkeypatch):
    # Apply is deliberately resumable per target, not a whole-plan transaction.
    async with _fresh_database() as pool:
        monkeypatch.setattr(adoption, "get_pool", lambda: pool)
        fixtures = [await _fixture(pool, label=label) for label in ("live", "deleted")]
        user = _admin()
        app = fixtures[0]
        plan = await adoption.create_legacy_adoption(
            app["app_id"], baseline_release_id=app["release_id"],
            idempotency_key=str(uuid.uuid4()), user=user, correlation_id="mixed-plan",
            targets=[{"vault_id": str(item["vault_id"]), "table_allowlist": ["orders"]}
                     for item in fixtures],
        )
        live, deleted = plan["targets"]  # canonical Vault UUID order
        async with pool.acquire() as conn:
            before = await conn.fetch("SELECT * FROM app_legacy_adoption_audit ORDER BY id")
            await conn.execute("DELETE FROM vaults WHERE id=$1", uuid.UUID(deleted["vault_id"]))
        with pytest.raises(ConflictError, match="deleted.*new adoption plan"):
            await adoption.apply_legacy_adoption(
                app["app_id"], plan["adoption_id"], user=user, correlation_id="mixed-apply",
            )
        retained = await adoption.get_legacy_adoption(
            app["app_id"], plan["adoption_id"], user=user, correlation_id="mixed-read",
        )
        assert retained["status"] == "partial"
        assert retained["targets"][0]["state"] == "applied"
        assert retained["targets"][1] == {**deleted, "vault_deleted": True}
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT array_agg(vault_id) FROM vault_app_installations",
            ) == [uuid.UUID(live["vault_id"])]
            after = await conn.fetch("SELECT * FROM app_legacy_adoption_audit ORDER BY id")
            assert all(row in after for row in before)
            assert all(row["vault_id"] == uuid.UUID(live["vault_id"])
                       for row in after if row not in before)


async def test_migration_reapply_plan_read_only_apply_and_owned_table_protection(monkeypatch):
    async with _fresh_database() as pool:
        # A second invocation exercises fresh + reapply idempotency for 077.
        async with pool.acquire() as conn:
            await _load_migration(_MIGRATIONS[-1]).migrate(conn=conn)
        monkeypatch.setattr(adoption, "get_pool", lambda: pool)
        monkeypatch.setattr(table_service, "get_pool", lambda: pool)
        monkeypatch.setattr(table_migration_service, "get_pool", lambda: pool)

        fixture = await _fixture(pool, label="apply")
        user = _admin()
        target = {
            "vault_id": str(fixture["vault_id"]),
            "table_allowlist": [fixture["table_name"]],
        }
        key = str(uuid.uuid4())

        async with pool.acquire() as conn:
            # Read-only preflight must not create control-plane metadata.
            before_counts = await conn.fetchrow(
                """
                SELECT
                    (SELECT count(*) FROM vault_app_installations) AS installations,
                    (SELECT count(*) FROM app_owned_resources) AS resources,
                    (SELECT count(*) FROM app_installation_observed_states) AS observed,
                    (SELECT count(*) FROM app_legacy_adoption_plans) AS plans
                """
            )
            before_row_count = await conn.fetchval(
                f"SELECT count(*) FROM {fixture['physical']}"
            )

        plan = await adoption.create_legacy_adoption(
            fixture["app_id"],
            baseline_release_id=fixture["release_id"],
            idempotency_key=key,
            targets=[target],
            user=user,
            correlation_id="live-adoption-plan",
        )
        assert plan["status"] == "planned"
        assert plan["targets"][0]["actual_schema_fingerprint"] == fixture["fingerprint"]
        assert plan["targets"][0]["state"] == "planned"

        async with pool.acquire() as conn:
            after_plan_counts = await conn.fetchrow(
                """
                SELECT
                    (SELECT count(*) FROM vault_app_installations) AS installations,
                    (SELECT count(*) FROM app_owned_resources) AS resources,
                    (SELECT count(*) FROM app_installation_observed_states) AS observed,
                    (SELECT count(*) FROM app_legacy_adoption_plans) AS plans
                """
            )
            assert after_plan_counts["installations"] == before_counts["installations"]
            assert after_plan_counts["resources"] == before_counts["resources"]
            assert after_plan_counts["observed"] == before_counts["observed"]
            assert after_plan_counts["plans"] == before_counts["plans"] + 1
            assert await conn.fetchval(
                f"SELECT count(*) FROM {fixture['physical']}"
            ) == before_row_count

        applied = await adoption.apply_legacy_adoption(
            fixture["app_id"],
            uuid.UUID(plan["adoption_id"]),
            user=user,
            correlation_id="live-adoption-apply",
        )
        assert applied["status"] == "applied"
        assert applied["targets"][0]["state"] == "applied"
        assert applied["targets"][0]["installation_id"] is not None

        replay = await adoption.apply_legacy_adoption(
            fixture["app_id"],
            uuid.UUID(plan["adoption_id"]),
            user=user,
            correlation_id="live-adoption-replay",
        )
        assert replay["status"] == "applied"
        assert replay["targets"][0]["state"] == "replayed"

        async with pool.acquire() as conn:
            installation = await conn.fetchrow(
                "SELECT * FROM vault_app_installations WHERE app_id=$1 AND vault_id=$2",
                fixture["app_id"],
                fixture["vault_id"],
            )
            assert installation["lifecycle"] == "active"
            assert installation["desired_release_id"] == fixture["release_id"]
            assert installation["current_release_id"] == fixture["release_id"]
            assert installation["grant_generation"] == 0
            assert await conn.fetchval(
                """
                SELECT count(*) FROM app_owned_resources
                 WHERE installation_id=$1 AND resource_kind='table'
                   AND resource_key=$2 AND status='owned'
                """,
                installation["id"],
                fixture["table_name"],
            ) == 1
            observed = await conn.fetchrow(
                "SELECT * FROM app_installation_observed_states WHERE installation_id=$1",
                installation["id"],
            )
            assert observed["observed_generation"] == 0
            assert observed["observed_grant_generation"] == 0
            assert observed["schema_fingerprint"] == fixture["fingerprint"]
            assert await conn.fetchval(
                f"SELECT count(*) FROM {fixture['physical']}"
            ) == before_row_count

            with pytest.raises(ConflictError):
                await table_service.alter_table(
                    fixture["vault_id"],
                    fixture["table_name"],
                    actor_id="ordinary-user",
                    add_columns=[{"name": "blocked", "type": "text"}],
                    _conn=conn,
                    _defer_index=True,
                )
            assert await conn.fetchval(
                """
                SELECT count(*) FROM information_schema.columns
                 WHERE table_name = $1 AND column_name = 'blocked'
                """,
                fixture["physical"],
            ) == 0

        with pytest.raises(ConflictError):
            await table_migration_service.apply_table_migration(
                fixture["vault_id"],
                actor_id="ordinary-user",
                idempotency_key=str(uuid.uuid4()),
                operations=[
                    {
                        "op": "add_column",
                        "table": fixture["table_name"],
                        "name": "blocked_again",
                        "type": "text",
                    }
                ],
            )


async def test_initial_grant_approval_preserves_adoption_and_rejects_drift_or_replay(
    monkeypatch,
):
    async with _fresh_database() as pool:
        monkeypatch.setattr(adoption, "get_pool", lambda: pool)
        monkeypatch.setattr(table_service, "get_pool", lambda: pool)
        monkeypatch.setattr(table_migration_service, "get_pool", lambda: pool)
        monkeypatch.setattr(installation, "get_pool", lambda: pool)
        monkeypatch.setattr(access_service, "get_pool", lambda: pool)

        fixture = await _fixture(pool, label="initial-grant")
        admin_id = uuid.uuid4()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO users(id, username, email, password_hash, is_admin)
                VALUES($1, $2, $3, 'unused', TRUE)
                """,
                admin_id,
                f"initial-grant-admin-{admin_id.hex}",
                f"initial-grant-{admin_id.hex}@example.invalid",
            )
        user = AuthenticatedUser(
            user_id=str(admin_id),
            username=f"initial-grant-admin-{admin_id.hex}",
            email=f"initial-grant-{admin_id.hex}@example.invalid",
            display_name=None,
            is_admin=True,
            auth_method="jwt",
        )

        principal = app_identity_service.AppPrincipal(
            app_id=fixture["app_id"],
            credential_id=uuid.uuid4(),
            credential_generation=1,
            deployment="fixture",
            token_id="fixture-token",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        target = {
            "vault_id": str(fixture["vault_id"]),
            "table_allowlist": [fixture["table_name"]],
        }
        plan = await adoption.create_legacy_adoption(
            fixture["app_id"],
            baseline_release_id=fixture["release_id"],
            idempotency_key=str(uuid.uuid4()),
            targets=[target],
            user=user,
            correlation_id="initial-grant-plan",
        )
        applied = await adoption.apply_legacy_adoption(
            fixture["app_id"],
            uuid.UUID(plan["adoption_id"]),
            user=user,
            correlation_id="initial-grant-apply",
        )
        assert applied["targets"][0]["state"] == "applied"

        with pytest.raises(ForbiddenError, match="App request denied"):
            await installation.get_app_installation_status(
                principal,
                fixture["vault_id"],
                correlation_id="initial-grant-before-status",
            )

        async with pool.acquire() as conn:
            before = await conn.fetchrow(
                """
                SELECT id, lifecycle, desired_release_id, current_release_id,
                       grant_generation
                  FROM vault_app_installations
                 WHERE app_id=$1 AND vault_id=$2
                """,
                fixture["app_id"],
                fixture["vault_id"],
            )
            observed_before = await conn.fetchrow(
                """
                SELECT observed_generation, observed_release_id,
                       observed_release_version, schema_fingerprint,
                       observed_grant_generation, checkpoint, recent_error
                  FROM app_installation_observed_states
                 WHERE installation_id=$1
                """,
                before["id"],
            )
            row_count_before = await conn.fetchval(
                f"SELECT count(*) FROM {fixture['physical']}"
            )
            assert before["lifecycle"] == "active"
            assert before["desired_release_id"] == fixture["release_id"]
            assert before["current_release_id"] == fixture["release_id"]
            assert before["grant_generation"] == 0
            assert observed_before["observed_generation"] == 0
            assert observed_before["observed_grant_generation"] == 0

            await conn.execute(
                """
                UPDATE vault_tables
                   SET columns='[{"name":"amount","type":"numeric"},
                                {"name":"drift","type":"text"}]'::jsonb
                 WHERE vault_id=$1 AND name=$2
                """,
                fixture["vault_id"],
                fixture["table_name"],
            )

        with pytest.raises(ConflictError, match="Initial grant approval"):
            await installation.approve_initial_installation_grant(
                fixture["app_id"],
                fixture["vault_id"],
                baseline_release_id=fixture["release_id"],
                capabilities=["installation:read"],
                user=user,
                correlation_id="initial-grant-drift-before-approval",
            )

        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM installation_grants WHERE installation_id=$1",
                before["id"],
            ) == 0
            assert await conn.fetchval(
                "SELECT grant_generation FROM vault_app_installations WHERE id=$1",
                before["id"],
            ) == 0
            await conn.execute(
                """
                UPDATE vault_tables
                   SET columns='[{"name":"amount","type":"numeric"}]'::jsonb
                 WHERE vault_id=$1 AND name=$2
                """,
                fixture["vault_id"],
                fixture["table_name"],
            )

        async def approve(correlation_id: str, capabilities: list[str] | None = None):
            return await installation.approve_initial_installation_grant(
                fixture["app_id"],
                fixture["vault_id"],
                baseline_release_id=fixture["release_id"],
                capabilities=capabilities or ["installation:read"],
                user=user,
                correlation_id=correlation_id,
            )

        results = await asyncio.gather(
            *(approve(f"initial-grant-concurrent-{index}") for index in range(8))
        )
        assert sum(result["command_status"] == "accepted" for result in results) == 1
        assert sum(result["command_status"] == "already_applied" for result in results) == 7
        assert {result["installation_id"] for result in results} == {str(before["id"])}
        assert {result["desired_grant_generation"] for result in results} == {1}

        status = await installation.get_app_installation_status(
            principal,
            fixture["vault_id"],
            correlation_id="initial-grant-after-status",
        )
        assert status["lifecycle"] == "active"
        assert status["desired_release"]["id"] == str(fixture["release_id"])
        assert status["current_release"]["id"] == str(fixture["release_id"])
        assert status["observed"]["generation"] == observed_before["observed_generation"]
        assert status["observed"]["grant_generation"] == 1
        assert status["active_grant"] == {
            "generation": 1,
            "status": "active",
            "capabilities": ["installation:read"],
        }

        with pytest.raises(ConflictError, match="conflicts with current grant"):
            await approve(
                "initial-grant-different-capability",
                ["installation:read", "inventory:read"],
            )

        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE vault_tables
                   SET columns='[{"name":"amount","type":"numeric"},
                                {"name":"drift","type":"text"}]'::jsonb
                 WHERE vault_id=$1 AND name=$2
                """,
                fixture["vault_id"],
                fixture["table_name"],
            )
        with pytest.raises(ConflictError, match="Initial grant approval"):
            await approve("initial-grant-drift-after-approval")

        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM installation_grants WHERE installation_id=$1",
                before["id"],
            ) == 1
            assert await conn.fetchval(
                f"SELECT count(*) FROM {fixture['physical']}"
            ) == row_count_before
            observed_after = await conn.fetchrow(
                "SELECT * FROM app_installation_observed_states WHERE installation_id=$1",
                before["id"],
            )
            assert observed_after["observed_generation"] == observed_before["observed_generation"]
            assert observed_after["observed_release_id"] == observed_before["observed_release_id"]
            assert observed_after["observed_release_version"] == observed_before["observed_release_version"]
            assert observed_after["schema_fingerprint"] == observed_before["schema_fingerprint"]
            assert observed_after["checkpoint"] == observed_before["checkpoint"]
            assert observed_after["recent_error"] == observed_before["recent_error"]
            await conn.execute(
                """
                UPDATE vault_tables
                   SET columns='[{"name":"amount","type":"numeric"}]'::jsonb
                 WHERE vault_id=$1 AND name=$2
                """,
                fixture["vault_id"],
                fixture["table_name"],
            )

        uninstalled = await installation.uninstall_installation(
            fixture["app_id"],
            fixture["vault_id"],
            user=user,
            correlation_id="initial-grant-uninstall",
        )
        assert uninstalled["lifecycle"] == "uninstalled"
        assert uninstalled["desired_grant_generation"] == 1
        with pytest.raises(ConflictError, match="Initial grant approval"):
            await approve("initial-grant-replay-after-uninstall")

        async with pool.acquire() as conn:
            final = await conn.fetchrow(
                """
                SELECT i.lifecycle, i.grant_generation,
                       g.generation, g.status
                  FROM vault_app_installations AS i
                  JOIN installation_grants AS g ON g.installation_id=i.id
                 WHERE i.id=$1
                """,
                before["id"],
            )
            assert dict(final) == {
                "lifecycle": "uninstalled",
                "grant_generation": 1,
                "generation": 1,
                "status": "revoked",
            }
            assert await conn.fetchval(
                "SELECT count(*) FROM app_owned_resources WHERE installation_id=$1 AND status='owned'",
                before["id"],
            ) == 0
            assert await conn.fetchval(
                "SELECT count(*) FROM app_owned_resources WHERE installation_id=$1 AND status='retained'",
                before["id"],
            ) == 1

        with pytest.raises(ForbiddenError, match="App request denied"):
            await installation.get_app_installation_status(
                principal,
                fixture["vault_id"],
                correlation_id="initial-grant-after-uninstall-status",
            )


async def test_post_plan_schema_drift_blocks_without_partial_metadata(monkeypatch):
    async with _fresh_database() as pool:
        monkeypatch.setattr(adoption, "get_pool", lambda: pool)
        fixture = await _fixture(pool, label="drift")
        user = _admin()
        plan = await adoption.create_legacy_adoption(
            fixture["app_id"],
            baseline_release_id=fixture["release_id"],
            idempotency_key=str(uuid.uuid4()),
            targets=[
                {
                    "vault_id": str(fixture["vault_id"]),
                    "table_allowlist": [fixture["table_name"]],
                }
            ],
            user=user,
            correlation_id="live-adoption-drift-plan",
        )
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE vault_tables
                   SET columns='[{\"name\":\"amount\",\"type\":\"numeric\"},{\"name\":\"drift\",\"type\":\"text\"}]'::jsonb
                 WHERE vault_id=$1 AND name=$2
                """,
                fixture["vault_id"],
                fixture["table_name"],
            )

        result = await adoption.apply_legacy_adoption(
            fixture["app_id"],
            uuid.UUID(plan["adoption_id"]),
            user=user,
            correlation_id="live-adoption-drift-apply",
        )
        assert result["status"] == "blocked"
        assert result["targets"][0]["state"] == "blocked"
        assert result["targets"][0]["reason_code"] == "fingerprint_changed"

        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM vault_app_installations WHERE app_id=$1",
                fixture["app_id"],
            ) == 0
            assert await conn.fetchval(
                "SELECT count(*) FROM app_owned_resources WHERE vault_id=$1",
                fixture["vault_id"],
            ) == 0


async def test_concurrent_apply_partial_resume_cross_app_conflict_and_audit_immutability(
    monkeypatch,
):
    async with _fresh_database() as pool:
        monkeypatch.setattr(adoption, "get_pool", lambda: pool)
        monkeypatch.setattr(inventory, "get_pool", lambda: pool)
        fixture = await _fixture(pool, label="partial")
        second_vault_id, second_vault_name = await _insert_vault_without_table(
            pool, label="partial-missing"
        )
        user = _admin()
        key = str(uuid.uuid4())
        targets = [
            {
                "vault_id": str(fixture["vault_id"]),
                "table_allowlist": [fixture["table_name"]],
            },
            {
                "vault_id": str(second_vault_id),
                "table_allowlist": [fixture["table_name"]],
            },
        ]
        plan = await adoption.create_legacy_adoption(
            fixture["app_id"],
            baseline_release_id=fixture["release_id"],
            idempotency_key=key,
            targets=targets,
            user=user,
            correlation_id="live-adoption-partial-plan",
        )
        assert plan["status"] == "planned"
        targets_by_vault = {target["vault_id"]: target for target in plan["targets"]}
        assert targets_by_vault[str(fixture["vault_id"])]["state"] == "planned"
        assert targets_by_vault[str(second_vault_id)]["state"] == "blocked"
        assert targets_by_vault[str(second_vault_id)]["reason_code"] == "missing_table"

        # The same immutable plan may be applied concurrently.  The pair lock
        # and target state transition leave one installation/resource baseline;
        # the second caller only replays the completed target.
        results = await asyncio.gather(
            adoption.apply_legacy_adoption(
                fixture["app_id"],
                uuid.UUID(plan["adoption_id"]),
                user=user,
                correlation_id="live-adoption-concurrent-a",
            ),
            adoption.apply_legacy_adoption(
                fixture["app_id"],
                uuid.UUID(plan["adoption_id"]),
                user=user,
                correlation_id="live-adoption-concurrent-b",
            ),
        )
        assert all(result["status"] == "partial" for result in results)

        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM vault_app_installations WHERE app_id=$1",
                fixture["app_id"],
            ) == 1
            assert await conn.fetchval(
                "SELECT count(*) FROM app_owned_resources WHERE vault_id=$1",
                fixture["vault_id"],
            ) == 1

        # Make only the blocked target eligible, then resume.  The first
        # target replays and the missing target is applied without duplication.
        await _insert_orders_table(
            pool,
            vault_id=second_vault_id,
            vault_name=second_vault_name,
        )
        resumed = await adoption.apply_legacy_adoption(
            fixture["app_id"],
            uuid.UUID(plan["adoption_id"]),
            user=user,
            correlation_id="live-adoption-partial-resume",
        )
        assert resumed["status"] == "applied"
        assert {target["state"] for target in resumed["targets"]} == {
            "applied",
            "replayed",
        }
        inventory_projection = await inventory.list_inventory(fixture["app_id"])
        adopted_item = next(
            item
            for item in inventory_projection["items"]
            if item["vault_id"] == str(fixture["vault_id"])
        )
        assert adopted_item["drift"]["overall"] == "in_sync"

        # A different app cannot adopt the already owned table, and the
        # immutable plan's idempotency key cannot be reused with new input.
        other_app_id = uuid.uuid4()
        other_app_key = f"legacy-other-{uuid.uuid4().hex}"
        other_table = {
            "name": fixture["table_name"],
            "columns": [{"name": "amount", "type": "numeric"}],
            "unique_keys": [],
            "indexes": [],
        }
        other_create_step = {
            "id": "create_orders",
            "phase": "expand",
            "operation": "create_table",
            "payload": {
                "table": fixture["table_name"],
                "columns": other_table["columns"],
                "unique_keys": [],
                "indexes": [],
            },
        }
        other_create_step["checksum"] = hashlib.sha256(
            json.dumps(
                other_create_step,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        manifest = {
            "manifest_version": 2,
            "app_key": other_app_key,
            "source_revision": "a" * 40,
            "image_digest": "sha256:" + "b" * 64,
            "schema_version": 3,
            "schema": {"tables": [other_table]},
            "transition_plans": [
                {"source": "fresh", "steps": [other_create_step]}
            ],
        }
        other_checksum = rollout.manifest_checksum(manifest, version="1.0.0")
        normalized_other_manifest = rollout.validate_manifest(
            manifest, other_checksum, version="1.0.0"
        )
        encoded_manifest = json.dumps(
            rollout.manifest_storage_projection(normalized_other_manifest),
            separators=(",", ":"),
            ensure_ascii=False,
        )
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO app_definitions(id, app_key) VALUES($1, $2)",
                other_app_id,
                other_app_key,
            )
            other_release_id = await conn.fetchval(
                """
                INSERT INTO app_releases(app_id, version, manifest, manifest_checksum)
                VALUES($1, '1.0.0', $2::jsonb, $3)
                RETURNING id
                """,
                other_app_id,
                encoded_manifest,
                other_checksum,
            )
        other_plan = await adoption.create_legacy_adoption(
            other_app_id,
            baseline_release_id=other_release_id,
            idempotency_key=str(uuid.uuid4()),
            targets=[targets[0]],
            user=user,
            correlation_id="live-adoption-cross-app-plan",
        )
        assert other_plan["status"] == "blocked"
        assert other_plan["targets"][0]["reason_code"] == "ownership_conflict"
        other_result = await adoption.apply_legacy_adoption(
            other_app_id,
            uuid.UUID(other_plan["adoption_id"]),
            user=user,
            correlation_id="live-adoption-cross-app-apply",
        )
        assert other_result["status"] == "blocked"
        async with pool.acquire() as conn:
            with pytest.raises(asyncpg.PostgresError):
                await conn.execute(
                    "UPDATE app_legacy_adoption_plans SET input_digest=$2 WHERE id=$1",
                    uuid.UUID(plan["adoption_id"]),
                    "b" * 64,
                )

        with pytest.raises(ConflictError):
            await adoption.create_legacy_adoption(
                fixture["app_id"],
                baseline_release_id=fixture["release_id"],
                idempotency_key=key,
                targets=[
                    {
                        **targets[0],
                        "table_allowlist": ["different_table"],
                    }
                ],
                user=user,
                correlation_id="live-adoption-idempotency-conflict",
            )

        async with pool.acquire() as conn:
            audit_id = await conn.fetchval(
                "SELECT id FROM app_legacy_adoption_audit ORDER BY created_at LIMIT 1"
            )
        assert audit_id is not None
        async with pool.acquire() as conn:
            with pytest.raises(asyncpg.PostgresError):
                async with conn.transaction():
                    await conn.execute(
                        "UPDATE app_legacy_adoption_audit SET reason_code='tampered' WHERE id=$1",
                        audit_id,
                    )
        async with pool.acquire() as conn:
            with pytest.raises(asyncpg.PostgresError):
                async with conn.transaction():
                    await conn.execute(
                        "DELETE FROM app_legacy_adoption_audit WHERE id=$1",
                        audit_id,
                    )
