"""Live PostgreSQL proof for the AKB-126 rollout ledger and worker."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import pytest

from app.services import app_rollout_service as rollout
from app.services import app_rollout_worker as worker
from app.services import app_resource_service as resources
from app.repositories.table_data_repo import pg_table_name

pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[1]
_INIT_SQL = (_BACKEND / "app" / "db" / "init.sql").read_text()
_MIGRATIONS = [
    _BACKEND / "app" / "db" / "migrations" / "047_app_registry.py",
    _BACKEND / "app" / "db" / "migrations" / "051_app_credentials.py",
    _BACKEND / "app" / "db" / "migrations" / "052_app_inventory.py",
    _BACKEND / "app" / "db" / "migrations" / "062_app_rollout.py",
    _BACKEND / "app" / "db" / "migrations" / "073_app_rollout_resume.py",
    _BACKEND / "app" / "db" / "migrations" / "086_dynamic_table_rows_changed.py",
    _BACKEND / "app" / "db" / "migrations" / "095_app_release_manifest_v2.py",
]
_DSN = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@localhost:15432/akb")


def _load_migration(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


@asynccontextmanager
async def _fresh_database():
    if not await _can_connect():
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"Required PostgreSQL is not reachable at {_DSN}")
        pytest.skip(f"Postgres not reachable at {_DSN}")
    admin = await asyncpg.connect(_DSN)
    name = f"akb_app_rollout_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    pool = await asyncpg.create_pool(_database_dsn(name), min_size=1, max_size=8)
    try:
        async with pool.acquire() as conn:
            await conn.execute(_INIT_SQL)
            for path in _MIGRATIONS:
                await _load_migration(path).migrate(conn=conn)
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


def _manifest(app_key: str = "rollout-test") -> tuple[dict, str]:
    table = {
        "name": "orders",
        "columns": [{"name": "flag", "type": "text"}],
        "unique_keys": [],
        "indexes": [],
    }
    source_fingerprint = resources.canonical_table_fingerprint([table])
    step = {
        "id": "backfill_flag",
        "phase": "backfill",
        "operation": "backfill_column",
        "payload": {
            "table": "orders",
            "column": "flag",
            "primary_key": "id",
            "where_null": True,
            "batch_size": 1,
            "value": "ready",
        },
    }
    step["checksum"] = hashlib.sha256(json.dumps(step, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    create = {
        "id": "create_orders",
        "phase": "expand",
        "operation": "create_table",
        "payload": {"table": "orders", "columns": table["columns"], "unique_keys": [], "indexes": []},
    }
    create["checksum"] = hashlib.sha256(
        json.dumps(create, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    manifest = {
        "manifest_version": 2,
        "app_key": app_key,
        "source_revision": "a" * 40,
        "image_digest": "sha256:" + "b" * 64,
        "schema_version": 3,
        "schema": {"tables": [table], "fingerprint": source_fingerprint},
        "transition_plans": [
            {"source": "fresh", "steps": [create]},
            {
                "source": {
                    "release_version": "1.0.0",
                    "schema_fingerprint": source_fingerprint,
                },
                "steps": [step],
            },
        ],
    }
    checksum = rollout.manifest_checksum(manifest, version="2.0.0")
    return manifest, checksum


async def test_rollout_request_is_idempotent_and_worker_resumes_backfill(monkeypatch):
    async with _fresh_database() as pool:
        monkeypatch.setattr(rollout, "get_pool", lambda: pool)
        monkeypatch.setattr(worker, "get_pool", lambda: pool)
        manifest, checksum = _manifest("rollout-test")
        async with pool.acquire() as conn:
            app_id = await conn.fetchval("INSERT INTO app_definitions(app_key) VALUES($1) RETURNING id", "rollout-test")
            old_id = await conn.fetchval("INSERT INTO app_releases(app_id,version,manifest,manifest_checksum) VALUES($1,'1.0.0',$2::jsonb,$3) RETURNING id", app_id, json.dumps(manifest), checksum)
            new_id = await conn.fetchval("INSERT INTO app_releases(app_id,version,manifest,manifest_checksum) VALUES($1,'2.0.0',$2::jsonb,$3) RETURNING id", app_id, json.dumps(manifest), checksum)
            vault_id = await conn.fetchval("INSERT INTO vaults(name,git_path) VALUES('rollout',$1) RETURNING id", "/tmp/rollout.git")
            installation_id = await conn.fetchval("INSERT INTO vault_app_installations(app_id,vault_id,desired_release_id,current_release_id,lifecycle) VALUES($1,$2,$3,$3,'active') RETURNING id", app_id, vault_id, old_id)
            await conn.execute("INSERT INTO installation_grants(installation_id,generation,capabilities,issuer) VALUES($1,1,$2,'test')", installation_id, ["rollout:read", "rollout:request"])
            await conn.execute("CREATE TABLE vt_rollout__orders (id UUID PRIMARY KEY DEFAULT uuid_generate_v4(), flag TEXT)")
            table_id = uuid.uuid4()
            await conn.execute("INSERT INTO vault_tables(id,vault_id,name,columns,unique_keys,indexes) VALUES($1,$2,'orders',$3::jsonb,'[]'::jsonb,'[]'::jsonb)", table_id, vault_id, json.dumps([{"name": "flag", "type": "text"}]))
            await conn.execute("INSERT INTO vt_rollout__orders(flag) VALUES(NULL),(NULL)")
            await conn.execute("INSERT INTO app_owned_resources(installation_id,vault_id,resource_kind,resource_key) VALUES($1,$2,'table','orders')", installation_id, vault_id)
            await conn.execute("INSERT INTO app_installation_observed_states(installation_id,app_id,vault_id,observed_generation,observed_at,observed_release_id,observed_release_version,schema_fingerprint,observed_grant_generation) VALUES($1,$2,$3,1,NOW(),$4,'1.0.0',$5,1)", installation_id, app_id, vault_id, old_id, resources.canonical_table_fingerprint([{"name": "orders", "columns": [{"name": "flag", "type": "text"}], "unique_keys": [], "indexes": []}]))
        key = str(uuid.uuid4())
        first = await rollout.request_rollout(app_id, release_id=new_id, manifest_checksum_value=checksum, idempotency_key=key, requested_by_kind="admin", correlation_id="test", actor="test", actor_id="test")
        replay = await rollout.request_rollout(app_id, release_id=new_id, manifest_checksum_value=checksum, idempotency_key=key, requested_by_kind="admin", correlation_id="test", actor="test", actor_id="test")
        assert replay["replayed"] is True
        assert replay["job_id"] == first["job_id"]
        await worker.run_once()
        async with pool.acquire() as conn:
            checkpoint = await conn.fetchval(
                "SELECT checkpoint FROM app_rollout_steps WHERE job_id=$1",
                uuid.UUID(first["job_id"]),
            )
            if isinstance(checkpoint, str):
                checkpoint = json.loads(checkpoint)
            assert checkpoint["completed"] == 1
            assert checkpoint["total"] == 1
        for _ in range(5):
            await worker.run_once()
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT status FROM app_rollout_jobs WHERE id=$1", uuid.UUID(first["job_id"])) == "applied"
            assert await conn.fetchval("SELECT count(*) FROM vt_rollout__orders WHERE flag IS NULL") == 0
            assert await conn.fetchval("SELECT lifecycle FROM vault_app_installations WHERE id=$1", installation_id) == "active"


async def test_fresh_plan_creates_complete_descriptor_and_postflight_fingerprint(monkeypatch):
    async with _fresh_database() as pool:
        monkeypatch.setattr(rollout, "get_pool", lambda: pool)
        monkeypatch.setattr(worker, "get_pool", lambda: pool)
        class _RoleSync:
            async def grant_table_in_conn(self, *_args):
                return None

        monkeypatch.setattr(worker, "get_role_sync", lambda: _RoleSync())
        table = {
            "name": "fresh_orders",
            "columns": [
                {"name": "amount", "type": "numeric"},
                {"name": "email", "type": "text", "required": True},
            ],
            "unique_keys": [{"columns": ["email"]}],
            "indexes": [{"columns": [{"name": "amount", "order": "desc"}]}],
        }
        create_step = {
            "id": "create_fresh_orders",
            "phase": "expand",
            "operation": "create_table",
            "payload": {
                "table": table["name"],
                "columns": table["columns"],
                "unique_keys": table["unique_keys"],
                "indexes": table["indexes"],
            },
        }
        create_step["checksum"] = hashlib.sha256(
            json.dumps(
                create_step,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        manifest = {
            "manifest_version": 2,
            "app_key": "fresh-test",
            "source_revision": "a" * 40,
            "image_digest": "sha256:" + "b" * 64,
            "schema_version": 3,
            "schema": {
                "tables": [table],
                "fingerprint": resources.canonical_table_fingerprint([table]),
            },
            "transition_plans": [
                {"source": "fresh", "steps": [create_step]}
            ],
        }
        checksum = rollout.manifest_checksum(manifest, version="1.0.0")
        async with pool.acquire() as conn:
            app_id = await conn.fetchval(
                "INSERT INTO app_definitions(app_key) VALUES($1) RETURNING id",
                "fresh-test",
            )
            release_id = await conn.fetchval(
                """
                INSERT INTO app_releases(app_id,version,manifest,manifest_checksum)
                VALUES($1,'1.0.0',$2::jsonb,$3) RETURNING id
                """,
                app_id,
                json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
                checksum,
            )
            vault_id = await conn.fetchval(
                "INSERT INTO vaults(name,git_path) VALUES($1,$2) RETURNING id",
                "fresh-vault",
                "/tmp/fresh-vault.git",
            )
            installation_id = await conn.fetchval(
                """
                INSERT INTO vault_app_installations(
                    app_id,vault_id,desired_release_id,current_release_id,lifecycle
                ) VALUES($1,$2,$3,NULL,'installing') RETURNING id
                """,
                app_id,
                vault_id,
                release_id,
            )
            await conn.execute(
                """
                INSERT INTO installation_grants(
                    installation_id,generation,capabilities,issuer
                ) VALUES($1,1,$2,'test')
                """,
                installation_id,
                ["rollout:read", "rollout:request"],
            )

        requested = await rollout.request_rollout(
            app_id,
            release_id=release_id,
            manifest_checksum_value=checksum,
            idempotency_key=str(uuid.uuid4()),
            requested_by_kind="admin",
            correlation_id="fresh-test",
            actor="test",
            actor_id="test",
        )
        assert requested["status"] == "pending"
        await worker.run_once()

        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT status FROM app_rollout_jobs WHERE id=$1",
                uuid.UUID(requested["job_id"]),
            ) == "applied"
            assert await conn.fetchval(
                "SELECT lifecycle FROM vault_app_installations WHERE id=$1",
                installation_id,
            ) == "active"
            assert await conn.fetchval(
                "SELECT current_release_id FROM vault_app_installations WHERE id=$1",
                installation_id,
            ) == release_id
            row = await conn.fetchrow(
                "SELECT columns, unique_keys, indexes FROM vault_tables WHERE vault_id=$1 AND name='fresh_orders'",
                vault_id,
            )
            assert row is not None
            columns = row["columns"]
            unique_keys = row["unique_keys"]
            indexes = row["indexes"]
            if isinstance(columns, str):
                columns = json.loads(columns)
            if isinstance(unique_keys, str):
                unique_keys = json.loads(unique_keys)
            if isinstance(indexes, str):
                indexes = json.loads(indexes)
            assert {column["name"] for column in columns} == {"email", "amount"}
            assert next(column for column in columns if column["name"] == "email")["required"] is True
            assert len(unique_keys) == 1
            assert unique_keys[0]["columns"] == ["email"]
            assert len(indexes) == 1
            assert indexes[0]["columns"] == [{"name": "amount", "order": "desc"}]
            assert await conn.fetchval(
                "SELECT schema_fingerprint FROM app_installation_observed_states WHERE installation_id=$1",
                installation_id,
            ) == resources.canonical_table_fingerprint(
                [{"name": "fresh_orders", "columns": columns, "unique_keys": unique_keys, "indexes": indexes}]
            )
            assert await conn.fetchval(
                f"SELECT COUNT(*) FROM {pg_table_name('fresh-vault', 'fresh_orders')}"
            ) == 0


async def test_missing_exact_source_plan_rejects_before_desired_state_mutation(monkeypatch):
    async with _fresh_database() as pool:
        monkeypatch.setattr(rollout, "get_pool", lambda: pool)
        manifest, _ = _manifest("source-plan-test")
        manifest["transition_plans"] = [manifest["transition_plans"][0]]
        checksum = rollout.manifest_checksum(manifest, version="2.0.0")
        source_fingerprint = resources.canonical_table_fingerprint(
            [{"name": "orders", "columns": [{"name": "flag", "type": "text"}], "unique_keys": [], "indexes": []}]
        )
        async with pool.acquire() as conn:
            app_id = await conn.fetchval(
                "INSERT INTO app_definitions(app_key) VALUES($1) RETURNING id",
                "source-plan-test",
            )
            old_release = await conn.fetchval(
                """
                INSERT INTO app_releases(app_id,version,manifest,manifest_checksum)
                VALUES($1,'1.0.0',$2::jsonb,$3) RETURNING id
                """,
                app_id,
                json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
                checksum,
            )
            release_id = await conn.fetchval(
                """
                INSERT INTO app_releases(app_id,version,manifest,manifest_checksum)
                VALUES($1,'2.0.0',$2::jsonb,$3) RETURNING id
                """,
                app_id,
                json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
                checksum,
            )
            vault_id = await conn.fetchval(
                "INSERT INTO vaults(name,git_path) VALUES('source-plan-vault',$1) RETURNING id",
                "/tmp/source-plan-vault.git",
            )
            installation_id = await conn.fetchval(
                """
                INSERT INTO vault_app_installations(
                    app_id,vault_id,desired_release_id,current_release_id,lifecycle
                ) VALUES($1,$2,$3,$3,'active') RETURNING id
                """,
                app_id,
                vault_id,
                old_release,
            )
            await conn.execute(
                "INSERT INTO installation_grants(installation_id,generation,capabilities,issuer) VALUES($1,1,$2,'test')",
                installation_id,
                ["rollout:read", "rollout:request"],
            )
            await conn.execute(
                """
                INSERT INTO app_installation_observed_states(
                    installation_id,app_id,vault_id,observed_generation,
                    observed_at,observed_release_id,observed_release_version,
                    schema_fingerprint,observed_grant_generation
                ) VALUES($1,$2,$3,1,NOW(),$4,'1.0.0',$5,1)
                """,
                installation_id,
                app_id,
                vault_id,
                old_release,
                source_fingerprint,
            )

        with pytest.raises(rollout.ConflictError):
            await rollout.request_rollout(
                app_id,
                release_id=release_id,
                manifest_checksum_value=checksum,
                idempotency_key=str(uuid.uuid4()),
                requested_by_kind="admin",
                correlation_id="source-plan-test",
                actor="test",
                actor_id="test",
            )
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT desired_release_id FROM vault_app_installations WHERE id=$1",
                installation_id,
            ) == old_release
            assert await conn.fetchval(
                "SELECT count(*) FROM app_rollout_jobs WHERE app_id=$1", app_id
            ) == 0


async def test_blocked_resume_creates_immutable_new_attempt_and_replays(monkeypatch):
    """A recovery retry never rewrites the blocked source ledger."""
    async with _fresh_database() as pool:
        monkeypatch.setattr(rollout, "get_pool", lambda: pool)
        manifest, checksum = _manifest("resume-test")
        async with pool.acquire() as conn:
            app_id = await conn.fetchval(
                "INSERT INTO app_definitions(app_key) VALUES($1) RETURNING id",
                "resume-test",
            )
            old_release = await conn.fetchval(
                "INSERT INTO app_releases(app_id,version,manifest,manifest_checksum) VALUES($1,'1.0.0',$2::jsonb,$3) RETURNING id",
                app_id,
                json.dumps(manifest),
                checksum,
            )
            new_release = await conn.fetchval(
                "INSERT INTO app_releases(app_id,version,manifest,manifest_checksum) VALUES($1,'2.0.0',$2::jsonb,$3) RETURNING id",
                app_id,
                json.dumps(manifest),
                checksum,
            )
            vault_id = await conn.fetchval(
                "INSERT INTO vaults(name,git_path) VALUES($1,$2) RETURNING id",
                f"resume-{uuid.uuid4().hex}",
                "/tmp/resume.git",
            )
            installation_id = await conn.fetchval(
                """
                INSERT INTO vault_app_installations(
                    app_id,vault_id,desired_release_id,current_release_id,
                    lifecycle,blocked_reason
                ) VALUES($1,$2,$3,$4,'blocked','step_failed') RETURNING id
                """,
                app_id,
                vault_id,
                new_release,
                old_release,
            )
            await conn.execute(
                "INSERT INTO installation_grants(installation_id,generation,capabilities,issuer) VALUES($1,1,$2,'test')",
                installation_id,
                ["rollout:read", "rollout:request"],
            )
            await conn.execute(
                "CREATE TABLE vt_resume__orders (id UUID PRIMARY KEY DEFAULT uuid_generate_v4(), flag TEXT)"
            )
            await conn.execute(
                "INSERT INTO vault_tables(id,vault_id,name,columns,unique_keys,indexes) VALUES($1,$2,'orders',$3::jsonb,'[]'::jsonb,'[]'::jsonb)",
                uuid.uuid4(),
                vault_id,
                json.dumps([{"name": "flag", "type": "text"}]),
            )
            await conn.execute(
                "INSERT INTO app_owned_resources(installation_id,vault_id,resource_kind,resource_key) VALUES($1,$2,'table','orders')",
                installation_id,
                vault_id,
            )
            await conn.execute(
                """
                INSERT INTO app_installation_observed_states(
                    installation_id,app_id,vault_id,observed_generation,
                    observed_at,observed_release_id,observed_release_version,
                    schema_fingerprint,observed_grant_generation
                ) VALUES($1,$2,$3,1,NOW(),$4,'1.0.0',$5,1)
                """,
                installation_id,
                app_id,
                vault_id,
                old_release,
                resources.canonical_table_fingerprint(
                    [{"name": "orders", "columns": [{"name": "flag", "type": "text"}], "unique_keys": [], "indexes": []}]
                ),
            )
            source_snapshot = await conn.fetchval(
                "INSERT INTO app_rollout_snapshots(app_id) VALUES($1) RETURNING id",
                app_id,
            )
            source_snapshot_target = await conn.fetchval(
                """
                INSERT INTO app_rollout_snapshot_targets(
                    snapshot_id,app_id,installation_id,vault_id,
                    desired_release_id,current_release_id,baseline_grant_generation,
                    state,reason_code
                ) VALUES($1,$2,$3,$4,$5,$6,1,'skipped','step_failed') RETURNING id
                """,
                source_snapshot,
                app_id,
                installation_id,
                vault_id,
                new_release,
                old_release,
            )
            await conn.execute(
                "UPDATE app_rollout_snapshots SET sealed_at=NOW() WHERE id=$1",
                source_snapshot,
            )
            source_id = await conn.fetchval(
                """
                INSERT INTO app_rollout_jobs(
                    app_id,release_id,manifest_checksum,idempotency_key,
                    snapshot_id,requested_by_kind,status,blocked_reason,completed_at
                ) VALUES($1,$2,$3,$4,$5,'admin','blocked','step_failed',NOW()) RETURNING id
                """,
                app_id,
                new_release,
                checksum,
                uuid.uuid4(),
                source_snapshot,
            )
            source_target_id = await conn.fetchval(
                """
                INSERT INTO app_rollout_targets(
                    job_id,app_id,installation_id,snapshot_target_id,vault_id,
                    release_id,ordinal,batch_no,is_canary,state,reason_code
                ) VALUES($1,$2,$3,$4,$5,$6,0,0,TRUE,'blocked','step_failed') RETURNING id
                """,
                source_id,
                app_id,
                installation_id,
                source_snapshot_target,
                vault_id,
                new_release,
            )
            step_checksum = hashlib.sha256(
                json.dumps(
                    manifest["transition_plans"][1]["steps"][0],
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            await conn.execute(
                """
                INSERT INTO app_rollout_steps(
                    job_id,target_id,installation_id,release_id,step_id,step_order,
                    step_checksum,operation,state,checkpoint,reason_code
                ) VALUES($1,$2,$3,$4,'backfill_flag',0,$5,'backfill_column','blocked',$6::jsonb,'step_failed')
                """,
                source_id,
                source_target_id,
                installation_id,
                new_release,
                step_checksum,
                json.dumps({"cursor": "immutable"}),
            )
            before = await conn.fetchrow(
                "SELECT status, blocked_reason FROM app_rollout_jobs WHERE id=$1",
                source_id,
            )
            before_step = await conn.fetchrow(
                "SELECT state, checkpoint FROM app_rollout_steps WHERE job_id=$1",
                source_id,
            )
        key = str(uuid.uuid4())
        first = await rollout.resume_rollout(
            app_id,
            source_id,
            release_id=new_release,
            manifest_checksum_value=checksum,
            idempotency_key=key,
            requested_by_kind="admin",
            correlation_id="resume-test",
            actor="test",
            actor_id="test",
        )
        replay = await rollout.resume_rollout(
            app_id,
            source_id,
            release_id=new_release,
            manifest_checksum_value=checksum,
            idempotency_key=key,
            requested_by_kind="admin",
            correlation_id="resume-test",
            actor="test",
            actor_id="test",
        )
        assert first["replayed"] is False
        assert replay["replayed"] is True
        assert replay["job_id"] == first["job_id"]
        assert first["source_rollout_id"] == str(source_id)
        async with pool.acquire() as conn:
            after = await conn.fetchrow(
                "SELECT status, blocked_reason FROM app_rollout_jobs WHERE id=$1",
                source_id,
            )
            after_step = await conn.fetchrow(
                "SELECT state, checkpoint FROM app_rollout_steps WHERE job_id=$1",
                source_id,
            )
            assert after == before
            assert after_step == before_step
            assert await conn.fetchval(
                "SELECT source_rollout_id FROM app_rollout_jobs WHERE id=$1",
                uuid.UUID(first["job_id"]),
            ) == source_id
            assert await conn.fetchval(
                "SELECT count(*) FROM app_rollout_resume_attempts WHERE source_rollout_id=$1",
                source_id,
            ) == 1
            assert await conn.fetchval(
                "SELECT count(*) FROM app_rollout_jobs WHERE source_rollout_id=$1",
                source_id,
            ) == 1


async def test_blocked_resume_accepts_mixed_source_target_lifecycles(monkeypatch):
    """A blocked source may leave failed and untouched targets in mixed states."""
    async with _fresh_database() as pool:
        monkeypatch.setattr(rollout, "get_pool", lambda: pool)
        manifest, checksum = _manifest("resume-mixed-test")
        async with pool.acquire() as conn:
            app_id = await conn.fetchval(
                "INSERT INTO app_definitions(app_key) VALUES($1) RETURNING id",
                "resume-mixed-test",
            )
            old_release = await conn.fetchval(
                """
                INSERT INTO app_releases(app_id,version,manifest,manifest_checksum)
                VALUES($1,'1.0.0',$2::jsonb,$3) RETURNING id
                """,
                app_id,
                json.dumps(manifest),
                checksum,
            )
            new_release = await conn.fetchval(
                """
                INSERT INTO app_releases(app_id,version,manifest,manifest_checksum)
                VALUES($1,'2.0.0',$2::jsonb,$3) RETURNING id
                """,
                app_id,
                json.dumps(manifest),
                checksum,
            )
            source_snapshot = await conn.fetchval(
                "INSERT INTO app_rollout_snapshots(app_id) VALUES($1) RETURNING id",
                app_id,
            )
            source_targets: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]] = []
            for ordinal in range(13):
                vault_id = await conn.fetchval(
                    "INSERT INTO vaults(name,git_path) VALUES($1,$2) RETURNING id",
                    f"resume-mixed-{uuid.uuid4().hex}",
                    f"/tmp/resume-mixed-{uuid.uuid4().hex}.git",
                )
                lifecycle = "blocked" if ordinal == 0 else "upgrading"
                installation_id = await conn.fetchval(
                    """
                    INSERT INTO vault_app_installations(
                        app_id,vault_id,desired_release_id,current_release_id,
                        lifecycle,blocked_reason
                    ) VALUES($1,$2,$3,$4,$5,$6) RETURNING id
                    """,
                    app_id,
                    vault_id,
                    new_release,
                    old_release,
                    lifecycle,
                    "step_failed" if ordinal == 0 else None,
                )
                await conn.execute(
                    """
                    INSERT INTO installation_grants(
                        installation_id,generation,capabilities,issuer
                    ) VALUES($1,1,$2,'test')
                    """,
                    installation_id,
                    ["rollout:read", "rollout:request"],
                )
                await conn.execute(
                    """
                    INSERT INTO app_owned_resources(
                        installation_id,vault_id,resource_kind,resource_key,status
                    ) VALUES($1,$2,'table','orders','owned')
                    """,
                    installation_id,
                    vault_id,
                )
                await conn.execute(
                    """
                    INSERT INTO app_installation_observed_states(
                        installation_id,app_id,vault_id,observed_generation,
                        observed_at,observed_release_id,observed_release_version,
                        schema_fingerprint,observed_grant_generation
                    ) VALUES($1,$2,$3,1,NOW(),$4,'1.0.0',$5,1)
                    """,
                    installation_id,
                    app_id,
                    vault_id,
                    old_release,
                    resources.canonical_table_fingerprint(
                        [{"name": "orders", "columns": [{"name": "flag", "type": "text"}], "unique_keys": [], "indexes": []}]
                    ),
                )
                snapshot_target = await conn.fetchval(
                    """
                    INSERT INTO app_rollout_snapshot_targets(
                        snapshot_id,app_id,installation_id,vault_id,
                        desired_release_id,current_release_id,
                        baseline_grant_generation,state,reason_code
                    ) VALUES($1,$2,$3,$4,$5,$6,1,$7,'step_failed') RETURNING id
                    """,
                    source_snapshot,
                    app_id,
                    installation_id,
                    vault_id,
                    new_release,
                    old_release,
                    "failed" if ordinal == 0 else "skipped",
                )
                source_targets.append((installation_id, vault_id, snapshot_target))

            await conn.execute(
                "UPDATE app_rollout_snapshots SET sealed_at=NOW() WHERE id=$1",
                source_snapshot,
            )
            source_id = await conn.fetchval(
                """
                INSERT INTO app_rollout_jobs(
                    app_id,release_id,manifest_checksum,idempotency_key,
                    snapshot_id,requested_by_kind,status,blocked_reason,completed_at
                ) VALUES($1,$2,$3,$4,$5,'admin','blocked','step_failed',NOW()) RETURNING id
                """,
                app_id,
                new_release,
                checksum,
                uuid.uuid4(),
                source_snapshot,
            )
            for ordinal, (installation_id, vault_id, snapshot_target) in enumerate(source_targets):
                await conn.execute(
                    """
                    INSERT INTO app_rollout_targets(
                        job_id,app_id,installation_id,snapshot_target_id,vault_id,
                        release_id,ordinal,batch_no,is_canary,state,reason_code
                    ) VALUES($1,$2,$3,$4,$5,$6,$7,0,$8,'blocked','step_failed')
                    """,
                    source_id,
                    app_id,
                    installation_id,
                    snapshot_target,
                    vault_id,
                    new_release,
                    ordinal,
                    ordinal == 0,
                )
            before = await conn.fetchrow(
                "SELECT status, blocked_reason FROM app_rollout_jobs WHERE id=$1",
                source_id,
            )

        key = str(uuid.uuid4())
        first = await rollout.resume_rollout(
            app_id,
            source_id,
            release_id=new_release,
            manifest_checksum_value=checksum,
            idempotency_key=key,
            requested_by_kind="admin",
            correlation_id="resume-mixed-test",
            actor="test",
            actor_id="test",
        )
        replay = await rollout.resume_rollout(
            app_id,
            source_id,
            release_id=new_release,
            manifest_checksum_value=checksum,
            idempotency_key=key,
            requested_by_kind="admin",
            correlation_id="resume-mixed-test",
            actor="test",
            actor_id="test",
        )

        assert first["replayed"] is False
        assert replay["replayed"] is True
        assert replay["job_id"] == first["job_id"]
        async with pool.acquire() as conn:
            assert await conn.fetchrow(
                "SELECT status, blocked_reason FROM app_rollout_jobs WHERE id=$1",
                source_id,
            ) == before
            assert await conn.fetchval(
                "SELECT count(*) FROM app_rollout_targets WHERE job_id=$1",
                uuid.UUID(first["job_id"]),
            ) == 13
            assert await conn.fetchval(
                "SELECT count(*) FROM app_rollout_resume_attempts WHERE source_rollout_id=$1",
                source_id,
            ) == 1
