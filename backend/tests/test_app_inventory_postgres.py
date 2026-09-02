"""Live PostgreSQL proof for app inventory and sealed rollout snapshots."""

from __future__ import annotations

import importlib.util
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import pytest

from app.services import app_inventory_service as inventory
from app.services import app_resource_service as resources
from app.services import app_rollout_service as rollout

pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[1]
_INIT_SQL = (_BACKEND / "app" / "db" / "init.sql").read_text()
_MIGRATIONS = [
    _BACKEND / "app" / "db" / "migrations" / "047_app_registry.py",
    _BACKEND / "app" / "db" / "migrations" / "051_app_credentials.py",
    _BACKEND / "app" / "db" / "migrations" / "052_app_inventory.py",
    _BACKEND / "app" / "db" / "migrations" / "095_app_release_manifest_v2.py",
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
async def _fresh_database():
    if not await _can_connect():
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"Required PostgreSQL is not reachable at {_DSN}")
        pytest.skip(f"Postgres not reachable at {_DSN}")
    admin = await asyncpg.connect(_DSN)
    name = f"akb_app_inventory_{uuid.uuid4().hex[:12]}"
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


async def _apply_all(pool):
    async with pool.acquire() as conn:
        for path in _MIGRATIONS:
            await _load_migration(path).migrate(conn=conn)


async def _app(pool, label: str) -> uuid.UUID:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO app_definitions (app_key) VALUES ($1) RETURNING id",
            f"{label}-{uuid.uuid4().hex}",
        )


async def _release(pool, app_id: uuid.UUID, version: str = "1.0.0") -> uuid.UUID:
    async with pool.acquire() as conn:
        app_key = await conn.fetchval(
            "SELECT app_key FROM app_definitions WHERE id=$1", app_id
        )
    manifest = {
        "manifest_version": 2,
        "app_key": app_key,
        "source_revision": "a" * 40,
        "image_digest": "sha256:" + "b" * 64,
        "schema_version": 3,
        "schema": {"tables": []},
        "transition_plans": [{"source": "fresh", "steps": []}],
    }
    checksum = rollout.manifest_checksum(manifest, version=version)
    normalized = rollout.validate_manifest(manifest, checksum, version=version)
    stored_manifest = rollout.manifest_storage_projection(normalized)
    encoded = json.dumps(stored_manifest, separators=(",", ":"))
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO app_releases (app_id, version, manifest, manifest_checksum)
            VALUES ($1, $2, $3::jsonb, $4)
            RETURNING id
            """,
            app_id,
            version,
            encoded,
            checksum,
        )


async def _vault(pool, label: str) -> uuid.UUID:
    name = f"{label}-{uuid.uuid4().hex[:10]}"
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO vaults (name, git_path) VALUES ($1, $2) RETURNING id",
            name,
            f"/tmp/{name}.git",
        )


async def _installation(
    pool,
    app_id: uuid.UUID,
    vault_id: uuid.UUID,
    release_id: uuid.UUID,
) -> uuid.UUID:
    async with pool.acquire() as conn:
        installation_id = await conn.fetchval(
            """
            INSERT INTO vault_app_installations (
                app_id, vault_id, desired_release_id, current_release_id, lifecycle
            ) VALUES ($1, $2, $3, $3, 'active')
            RETURNING id
            """,
            app_id,
            vault_id,
            release_id,
        )
        await conn.execute(
            """
            INSERT INTO installation_grants (installation_id, generation, capabilities, issuer)
            VALUES ($1, 1, $2, 'fixture')
            """,
            installation_id,
            ["inventory:read", "rollout:read", "rollout:request"],
        )
    return installation_id


@pytest.fixture
async def inventory_pool(monkeypatch):
    async with _fresh_database() as pool:
        monkeypatch.setattr(inventory, "get_pool", lambda: pool)
        yield pool


async def test_migration_reapply_and_observed_state_is_monotonic(inventory_pool):
    await _apply_all(inventory_pool)
    app_id = await _app(inventory_pool, "observed")
    release_id = await _release(inventory_pool, app_id)
    installation_id = await _installation(
        inventory_pool,
        app_id,
        await _vault(inventory_pool, "observed"),
        release_id,
    )

    initial = await inventory.list_inventory(app_id)
    assert initial["items"][0]["drift"]["overall"] == "unknown"

    observed_at = datetime.now(timezone.utc)
    accepted = await inventory.report_observed_state(
        installation_id,
        observed_generation=2,
        observed_at=observed_at,
        observed_release_id=release_id,
        schema_fingerprint=resources.canonical_table_fingerprint([]),
        observed_grant_generation=1,
        checkpoint={"phase": "ready", "token": "secret-marker"},
        recent_error={"code": "none", "message": "secret-marker"},
    )
    assert accepted["accepted"] is True

    stale = await inventory.report_observed_state(
        installation_id,
        observed_generation=1,
        observed_at=observed_at - timedelta(seconds=1),
        observed_release_id=release_id,
        schema_fingerprint=resources.canonical_table_fingerprint([]),
    )
    assert stale["accepted"] is False

    current = await inventory.list_inventory(app_id)
    item = current["items"][0]
    assert item["drift"]["overall"] == "in_sync"
    assert "secret-marker" not in json.dumps(item)
    assert item["recent_error"] == {"code": "none"}


async def test_snapshot_seal_membership_and_stale_target_eligibility(inventory_pool):
    app_id = await _app(inventory_pool, "snapshot")
    release_id = await _release(inventory_pool, app_id)
    vault_id = await _vault(inventory_pool, "snapshot")
    installation_id = await _installation(inventory_pool, app_id, vault_id, release_id)
    await inventory.report_observed_state(
        installation_id,
        observed_generation=1,
        observed_release_id=release_id,
        observed_grant_generation=1,
    )
    unobserved_installation_id = await _installation(
        inventory_pool,
        app_id,
        await _vault(inventory_pool, "unobserved"),
        release_id,
    )

    created = await inventory.create_rollout_snapshot(app_id)
    snapshot = await inventory.get_rollout_snapshot(
        app_id,
        uuid.UUID(created["snapshot_id"]),
    )
    assert snapshot["sealed_at"] is not None
    assert snapshot["target_count"] == 2
    target_by_installation = {
        target["installation_id"]: target for target in snapshot["targets"]
    }
    assert target_by_installation[str(installation_id)]["state"] == "pending"
    assert target_by_installation[str(unobserved_installation_id)]["state"] == "skipped"
    assert target_by_installation[str(unobserved_installation_id)]["reason_code"] == (
        "observed_state_missing"
    )
    target_id = uuid.UUID(target_by_installation[str(installation_id)]["target_id"])

    late_release = await _release(inventory_pool, app_id, "2.0.0")
    await _installation(inventory_pool, app_id, await _vault(inventory_pool, "late"), late_release)
    after_late_install = await inventory.get_rollout_snapshot(
        app_id,
        uuid.UUID(created["snapshot_id"]),
    )
    assert after_late_install["target_count"] == 2

    async with inventory_pool.acquire() as conn:
        with pytest.raises(asyncpg.PostgresError) as exc:
            await conn.execute(
                "UPDATE app_rollout_snapshot_targets SET baseline_grant_generation = 9 WHERE id = $1",
                target_id,
            )
        assert exc.value.sqlstate == "55000"
        await conn.execute(
            "UPDATE installation_grants SET status = 'revoked', revoked_at = NOW() WHERE installation_id = $1",
            installation_id,
        )

    eligibility = await inventory.evaluate_rollout_target(
        app_id,
        uuid.UUID(created["snapshot_id"]),
        target_id,
    )
    assert eligibility["state"] == "denied"
    assert eligibility["reason_code"] == "grant_revoked_or_missing"


async def test_inventory_cursor_lineage_survives_late_insert_and_existing_update(
    inventory_pool,
):
    app_id = await _app(inventory_pool, "cursor-lineage")
    release_id = await _release(inventory_pool, app_id)
    initial_ids = [
        await _installation(
            inventory_pool,
            app_id,
            await _vault(inventory_pool, f"cursor-{index}"),
            release_id,
        )
        for index in range(65)
    ]

    first_page = await inventory.list_inventory(app_id, limit=20)
    assert len(first_page["items"]) == 20
    assert first_page["next_cursor"] is not None

    late_id = await _installation(
        inventory_pool,
        app_id,
        await _vault(inventory_pool, "cursor-late"),
        release_id,
    )
    existing_id = uuid.UUID(first_page["items"][0]["installation_id"])
    async with inventory_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE vault_app_installations
               SET lifecycle = 'blocked', blocked_reason = $2
             WHERE id = $1
            """,
            existing_id,
            "fixture_b1_updated",
        )

    original_pages = [first_page]
    cursor = first_page["next_cursor"]
    while cursor is not None:
        page = await inventory.list_inventory(app_id, limit=20, cursor=cursor)
        original_pages.append(page)
        cursor = page["next_cursor"]

    original_ids = [
        item["installation_id"]
        for page in original_pages
        for item in page["items"]
    ]
    assert len(original_pages) >= 3
    assert original_pages[-1]["next_cursor"] is None
    assert len(original_ids) == len(initial_ids) == 65
    assert len(set(original_ids)) == 65
    assert str(late_id) not in original_ids

    fresh_pages = []
    cursor = None
    while True:
        page = await inventory.list_inventory(app_id, limit=20, cursor=cursor)
        fresh_pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            break

    fresh_ids = [
        item["installation_id"]
        for page in fresh_pages
        for item in page["items"]
    ]
    assert len(fresh_ids) == 66
    assert len(set(fresh_ids)) == 66
    assert fresh_ids.count(str(late_id)) == 1
    assert [item_id for item_id in fresh_ids if item_id != str(late_id)] == original_ids
