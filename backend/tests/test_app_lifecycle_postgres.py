"""Live PostgreSQL contract for app installation lifecycle commands."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import pytest

from app.exceptions import ConflictError, ForbiddenError, ValidationError
from app.services import app_identity_service
from app.services import app_lifecycle_service as lifecycle
from app.services.app_identity_service import AppPrincipal
from scripts.ci import e2e_runtime

pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[1]
_INIT_SQL = (_BACKEND / "app" / "db" / "init.sql").read_text()
_MIGRATIONS = [
    _BACKEND / "app" / "db" / "migrations" / "047_app_registry.py",
    _BACKEND / "app" / "db" / "migrations" / "051_app_credentials.py",
    _BACKEND / "app" / "db" / "migrations" / "052_app_inventory.py",
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
async def _fresh_database(*, include_dsn: bool = False):
    if not await _can_connect():
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"Required PostgreSQL is not reachable at {_DSN}")
        pytest.skip(f"Postgres not reachable at {_DSN}")
    admin = await asyncpg.connect(_DSN)
    name = f"akb_app_lifecycle_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    pool = await asyncpg.create_pool(_database_dsn(name), min_size=1, max_size=12)
    try:
        async with pool.acquire() as conn:
            await conn.execute(_INIT_SQL)
            for path in _MIGRATIONS:
                await _load_migration(path).migrate(conn=conn)
        yield (pool, _database_dsn(name)) if include_dsn else pool
    finally:
        await pool.close()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


@pytest.fixture
async def lifecycle_pool(monkeypatch):
    async with _fresh_database() as pool:
        monkeypatch.setattr(lifecycle, "get_pool", lambda: pool)
        yield pool


@pytest.fixture
async def runtime_database(monkeypatch):
    async with _fresh_database(include_dsn=True) as database:
        pool, dsn = database

        async def get_pool():
            return pool

        monkeypatch.setattr(app_identity_service, "get_pool", get_pool)
        yield pool, dsn


async def _app(pool, label: str) -> uuid.UUID:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO app_definitions (app_key) VALUES ($1) RETURNING id",
            f"{label}-{uuid.uuid4().hex}",
        )


async def _vault(pool, label: str) -> uuid.UUID:
    name = f"{label}-{uuid.uuid4().hex[:12]}"
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO vaults (name, git_path) VALUES ($1, $2) RETURNING id",
            name,
            f"/tmp/{name}.git",
        )


async def _release(
    pool,
    app_id: uuid.UUID,
    version: str,
    *,
    fingerprint: str | None = "a" * 64,
) -> uuid.UUID:
    manifest = {"steps": [{"id": "prepare"}]}
    if fingerprint is not None:
        manifest["expected_schema_fingerprint"] = fingerprint
    encoded = json.dumps(manifest, separators=(",", ":"))
    checksum = hashlib.sha256(encoded.encode()).hexdigest()
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


async def _fixture_installation(
    pool,
    app_id: uuid.UUID,
    vault_id: uuid.UUID,
    release_id: uuid.UUID,
    *,
    lifecycle_name: str = "active",
    capabilities: list[str] | None = None,
    resource: tuple[str, str] | None = None,
) -> uuid.UUID:
    capabilities = capabilities or ["installation:read"]
    async with pool.acquire() as conn:
        installation_id = await conn.fetchval(
            """
            INSERT INTO vault_app_installations (
                app_id, vault_id, desired_release_id, current_release_id, lifecycle
            ) VALUES ($1, $2, $3, $3, $4)
            RETURNING id
            """,
            app_id,
            vault_id,
            release_id,
            lifecycle_name,
        )
        await conn.execute(
            """
            INSERT INTO installation_grants (
                installation_id, generation, capabilities, issuer
            ) VALUES ($1, 1, $2, 'fixture')
            """,
            installation_id,
            capabilities,
        )
        if resource is not None:
            await conn.execute(
                """
                INSERT INTO app_owned_resources (
                    installation_id, vault_id, resource_kind, resource_key
                ) VALUES ($1, $2, $3, $4)
                """,
                installation_id,
                vault_id,
                resource[0],
                resource[1],
            )
    return installation_id


async def _observed(
    pool,
    installation_id: uuid.UUID,
    app_id: uuid.UUID,
    vault_id: uuid.UUID,
    release_id: uuid.UUID,
    *,
    fingerprint: str = "a" * 64,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO app_installation_observed_states (
                installation_id, app_id, vault_id, observed_generation,
                observed_at, observed_release_id, observed_release_version,
                schema_fingerprint, observed_grant_generation, checkpoint,
                recent_error
            ) VALUES (
                $1, $2, $3, 2, NOW(), $4, '1.0.0', $5, 1,
                '{"phase":"ready","token":"secret-marker"}'::jsonb,
                '{"code":"worker_timeout","message":"secret-marker"}'::jsonb
            )
            """,
            installation_id,
            app_id,
            vault_id,
            release_id,
            fingerprint,
        )


def _actor() -> dict[str, str]:
    return {"correlation_id": str(uuid.uuid4()), "actor": "operator", "actor_id": str(uuid.uuid4())}


def _principal(app_id: uuid.UUID) -> AppPrincipal:
    return AppPrincipal(
        app_id=app_id,
        credential_id=uuid.uuid4(),
        credential_generation=1,
        deployment="test",
        token_id="token-id",
        expires_at=None,  # type: ignore[arg-type]
    )


async def _put(pool, app_id, vault_id, release_id, capabilities=None, mode="install"):
    return await lifecycle.put_installation(
        app_id,
        vault_id,
        release_id=release_id,
        capabilities=capabilities or ["installation:read"],
        mode=mode,
        **_actor(),
    )


async def test_install_is_atomic_and_exact_replay_is_stable(lifecycle_pool):
    app_id = await _app(lifecycle_pool, "install")
    vault_id = await _vault(lifecycle_pool, "install")
    release_id = await _release(lifecycle_pool, app_id, "1.0.0")

    first = await _put(lifecycle_pool, app_id, vault_id, release_id)
    replay = await _put(lifecycle_pool, app_id, vault_id, release_id)

    assert first["command_status"] == "accepted"
    assert first["replayed"] is False
    assert first["lifecycle"] == "installing"
    assert first["grant_generation"] == 1
    assert replay["command_status"] == "already_applied"
    assert replay["replayed"] is True
    assert replay["installation_id"] == first["installation_id"]
    assert replay["grant_generation"] == first["grant_generation"]

    app_view = await lifecycle.get_installation_status_for_app(
        _principal(app_id),
        vault_id=vault_id,
        correlation_id=str(uuid.uuid4()),
    )
    assert app_view["lifecycle"] == "installing"
    assert app_view["command_status"] == "not_applicable"

    async with lifecycle_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM vault_app_installations WHERE app_id = $1 AND vault_id = $2",
            app_id,
            vault_id,
        ) == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM installation_grants WHERE installation_id = $1",
            uuid.UUID(first["installation_id"]),
        ) == 1


async def test_status_projects_grant_identity_and_resource_registry(lifecycle_pool):
    app_id = await _app(lifecycle_pool, "status-projection")
    vault_id = await _vault(lifecycle_pool, "status-projection")
    release_id = await _release(lifecycle_pool, app_id, "1.0.0")
    installation_id = await _fixture_installation(
        lifecycle_pool,
        app_id,
        vault_id,
        release_id,
        resource=("collection", "lifecycle-resource"),
    )
    await _observed(lifecycle_pool, installation_id, app_id, vault_id, release_id)

    async with lifecycle_pool.acquire() as conn:
        first_grant_id = await conn.fetchval(
            "SELECT id FROM installation_grants WHERE installation_id = $1 AND generation = 1",
            installation_id,
        )

    installed = await lifecycle.get_installation_status(app_id, vault_id)
    assert installed["latest_grant"]["id"] == str(first_grant_id)
    assert installed["latest_active_grant"]["id"] == str(first_grant_id)
    assert installed["resources"] == [
        {"kind": "collection", "key": "lifecycle-resource", "status": "owned"}
    ]

    await lifecycle.uninstall_installation(app_id, vault_id, **_actor())
    uninstalled = await lifecycle.get_installation_status(app_id, vault_id)
    assert uninstalled["latest_grant"]["id"] == str(first_grant_id)
    assert uninstalled["latest_active_grant"] is None
    assert uninstalled["resources"] == [
        {"kind": "collection", "key": "lifecycle-resource", "status": "retained"}
    ]

    restored = await _put(lifecycle_pool, app_id, vault_id, release_id, mode="restore")
    async with lifecycle_pool.acquire() as conn:
        second_grant_id = await conn.fetchval(
            "SELECT id FROM installation_grants WHERE installation_id = $1 AND generation = 2",
            installation_id,
        )
    assert restored["latest_grant"]["id"] == str(second_grant_id)

    restored_status = await lifecycle.get_installation_status(app_id, vault_id)
    assert restored_status["latest_active_grant"]["id"] == str(second_grant_id)
    assert restored_status["resources"] == [
        {"kind": "collection", "key": "lifecycle-resource", "status": "owned"}
    ]


async def test_same_request_concurrency_has_one_winner_and_conflict_is_atomic(lifecycle_pool):
    app_id = await _app(lifecycle_pool, "concurrent")
    vault_id = await _vault(lifecycle_pool, "concurrent")
    release_a = await _release(lifecycle_pool, app_id, "1.0.0")
    release_b = await _release(lifecycle_pool, app_id, "2.0.0")

    same_results = await asyncio.gather(
        *[_put(lifecycle_pool, app_id, vault_id, release_a) for _ in range(6)]
    )
    assert sum(not result["replayed"] for result in same_results) == 1
    assert len({result["installation_id"] for result in same_results}) == 1
    assert {result["grant_generation"] for result in same_results} == {1}

    conflict_results = await asyncio.gather(
        _put(lifecycle_pool, app_id, vault_id, release_a),
        _put(lifecycle_pool, app_id, vault_id, release_b),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ConflictError) for result in conflict_results) == 1
    assert sum(isinstance(result, dict) for result in conflict_results) == 1

    async with lifecycle_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM vault_app_installations WHERE app_id = $1 AND vault_id = $2",
            app_id,
            vault_id,
        ) == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM installation_grants WHERE installation_id = "
            "(SELECT id FROM vault_app_installations WHERE app_id = $1 AND vault_id = $2)",
            app_id,
            vault_id,
        ) == 1


async def test_invalid_and_foreign_release_leave_no_partial_state(lifecycle_pool):
    app_id = await _app(lifecycle_pool, "foreign")
    foreign_app_id = await _app(lifecycle_pool, "other")
    vault_id = await _vault(lifecycle_pool, "foreign")
    foreign_release = await _release(lifecycle_pool, foreign_app_id, "1.0.0")

    with pytest.raises(ConflictError):
        await _put(lifecycle_pool, app_id, vault_id, foreign_release)

    with pytest.raises(ValidationError):
        await _put(
            lifecycle_pool,
            app_id,
            vault_id,
            uuid.uuid4(),
            capabilities=["documents:write"],
        )

    async with lifecycle_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM vault_app_installations WHERE app_id = $1 AND vault_id = $2",
            app_id,
            vault_id,
        ) == 0


async def test_uninstall_revokes_and_retains_then_replays_without_deletion(lifecycle_pool):
    app_id = await _app(lifecycle_pool, "uninstall")
    vault_id = await _vault(lifecycle_pool, "uninstall")
    release_id = await _release(lifecycle_pool, app_id, "1.0.0")
    installation_id = await _fixture_installation(
        lifecycle_pool,
        app_id,
        vault_id,
        release_id,
        resource=("collection", "retained-key"),
    )

    first = await lifecycle.uninstall_installation(
        app_id, vault_id, **_actor()
    )
    replay = await lifecycle.uninstall_installation(
        app_id, vault_id, **_actor()
    )

    assert first["lifecycle"] == "uninstalled"
    assert first["command_status"] == "accepted"
    assert replay["command_status"] == "already_applied"
    assert replay["grant_generation"] == first["grant_generation"] == 1

    async with lifecycle_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT desired_release_id, current_release_id, lifecycle FROM vault_app_installations WHERE id = $1",
            installation_id,
        )
        assert row["desired_release_id"] is None
        assert row["current_release_id"] == release_id
        assert row["lifecycle"] == "uninstalled"
        assert await conn.fetchval(
            "SELECT status FROM installation_grants WHERE installation_id = $1 AND generation = 1",
            installation_id,
        ) == "revoked"
        assert await conn.fetchval(
            "SELECT status FROM app_owned_resources WHERE installation_id = $1",
            installation_id,
        ) == "retained"

    with pytest.raises(ForbiddenError):
        await lifecycle.get_installation_status_for_app(
            _principal(app_id),
            vault_id=vault_id,
            correlation_id=str(uuid.uuid4()),
        )


async def test_restore_requires_compatibility_and_creates_next_grant(lifecycle_pool):
    app_id = await _app(lifecycle_pool, "restore")
    vault_id = await _vault(lifecycle_pool, "restore")
    release_id = await _release(lifecycle_pool, app_id, "1.0.0")
    installation_id = await _fixture_installation(
        lifecycle_pool,
        app_id,
        vault_id,
        release_id,
        resource=("table", "retained-key"),
    )
    await _observed(lifecycle_pool, installation_id, app_id, vault_id, release_id)
    await lifecycle.uninstall_installation(app_id, vault_id, **_actor())

    restored = await _put(
        lifecycle_pool,
        app_id,
        vault_id,
        release_id,
        mode="restore",
    )
    replay = await _put(
        lifecycle_pool,
        app_id,
        vault_id,
        release_id,
        mode="restore",
    )

    assert restored["lifecycle"] == "active"
    assert restored["grant_generation"] == 2
    assert restored["command_status"] == "accepted"
    assert replay["command_status"] == "already_applied"
    assert "secret-marker" not in json.dumps(restored)
    assert restored["checkpoint"] == {"phase": "ready"}
    assert restored["recent_error"] == {"code": "worker_timeout"}

    async with lifecycle_pool.acquire() as conn:
        grants = await conn.fetch(
            "SELECT generation, status FROM installation_grants WHERE installation_id = $1 ORDER BY generation",
            installation_id,
        )
        assert [(row["generation"], row["status"]) for row in grants] == [
            (1, "revoked"),
            (2, "active"),
        ]
        assert await conn.fetchval(
            "SELECT status FROM app_owned_resources WHERE installation_id = $1",
            installation_id,
        ) == "owned"


async def test_restore_mismatch_and_fresh_collision_preserve_state(lifecycle_pool):
    app_id = await _app(lifecycle_pool, "rollback")
    vault_id = await _vault(lifecycle_pool, "rollback")
    release_id = await _release(lifecycle_pool, app_id, "1.0.0")
    installation_id = await _fixture_installation(
        lifecycle_pool,
        app_id,
        vault_id,
        release_id,
        resource=("table", "retained-key"),
    )
    await _observed(
        lifecycle_pool,
        installation_id,
        app_id,
        vault_id,
        release_id,
        fingerprint="b" * 64,
    )
    await lifecycle.uninstall_installation(app_id, vault_id, **_actor())

    async with lifecycle_pool.acquire() as conn:
        before = await conn.fetchrow(
            """
            SELECT i.desired_release_id, i.current_release_id, i.lifecycle,
                   i.grant_generation,
                   (SELECT count(*) FROM installation_grants g WHERE g.installation_id = i.id) AS grants,
                   (SELECT jsonb_agg(jsonb_build_object('status', r.status) ORDER BY r.id)
                      FROM app_owned_resources r WHERE r.installation_id = i.id) AS resources
              FROM vault_app_installations i
             WHERE i.id = $1
            """,
            installation_id,
        )

    with pytest.raises(ConflictError):
        await _put(lifecycle_pool, app_id, vault_id, release_id, mode="restore")

    async with lifecycle_pool.acquire() as conn:
        after = await conn.fetchrow(
            """
            SELECT i.desired_release_id, i.current_release_id, i.lifecycle,
                   i.grant_generation,
                   (SELECT count(*) FROM installation_grants g WHERE g.installation_id = i.id) AS grants,
                   (SELECT jsonb_agg(jsonb_build_object('status', r.status) ORDER BY r.id)
                      FROM app_owned_resources r WHERE r.installation_id = i.id) AS resources
              FROM vault_app_installations i
             WHERE i.id = $1
            """,
            installation_id,
        )
    assert dict(after) == dict(before)

    with pytest.raises(ConflictError):
        await _put(lifecycle_pool, app_id, vault_id, release_id, mode="fresh")
    async with lifecycle_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT lifecycle FROM vault_app_installations WHERE id = $1",
            installation_id,
        ) == "uninstalled"
        assert await conn.fetchval(
            "SELECT count(*) FROM app_owned_resources WHERE installation_id = $1",
            installation_id,
        ) == 1


async def test_fresh_without_retained_resources_clears_old_current_pointer(lifecycle_pool):
    app_id = await _app(lifecycle_pool, "fresh")
    vault_id = await _vault(lifecycle_pool, "fresh")
    old_release = await _release(lifecycle_pool, app_id, "1.0.0")
    new_release = await _release(lifecycle_pool, app_id, "2.0.0")
    await _fixture_installation(lifecycle_pool, app_id, vault_id, old_release)
    await lifecycle.uninstall_installation(app_id, vault_id, **_actor())

    fresh = await _put(
        lifecycle_pool,
        app_id,
        vault_id,
        new_release,
        mode="fresh",
    )
    assert fresh["lifecycle"] == "installing"
    assert fresh["grant_generation"] == 2
    assert fresh["desired_release"]["id"] == str(new_release)
    assert fresh["current_release"] is None

    async with lifecycle_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT desired_release_id, current_release_id, lifecycle
              FROM vault_app_installations
             WHERE app_id = $1 AND vault_id = $2
            """,
            app_id,
            vault_id,
        )
        assert row["desired_release_id"] == new_release
        assert row["current_release_id"] is None
        assert row["lifecycle"] == "installing"


async def test_runtime_seed_passes_registry_triggers_and_rotates_runtime_artifacts(
    runtime_database,
    monkeypatch,
    tmp_path,
):
    pool, dsn = runtime_database
    git_root = tmp_path / "vaults"
    monkeypatch.setattr(e2e_runtime, "GIT_FIXTURE_ROOT", git_root)
    monkeypatch.setattr(
        app_identity_service,
        "_configured_app_secret",
        lambda: "test-app-signing-key",
    )
    settings = e2e_runtime.RuntimeSettings(
        database_url=dsn,
        scenario=e2e_runtime.APP_LIFECYCLE_SCENARIO,
        ready_file=tmp_path / "ready.json",
    )

    first_manifest, first_credentials = await e2e_runtime._seed_app_lifecycle(
        dsn,
        origin=settings.origin,
        fixture_origin=settings.fixture_origin,
    )
    first_token = await app_identity_service.exchange_app_credential(
        first_credentials[e2e_runtime.CREDENTIAL_VARIABLES[5]],
        correlation_id="runtime-seed-proof",
    )
    first_credentials[e2e_runtime.CREDENTIAL_VARIABLES[6]] = first_token["access_token"]
    e2e_runtime.write_fixture_artifacts(settings, first_manifest, first_credentials)
    e2e_runtime.write_ready_file(settings.ready_file, e2e_runtime.ready_payload(settings))

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT i.lifecycle, i.grant_generation, g.generation, g.status
              FROM vault_app_installations AS i
              JOIN installation_grants AS g ON g.installation_id = i.id
             ORDER BY i.id, g.generation
            """
        )
    assert len(rows) == 8
    assert all(row["generation"] == 1 for row in rows)
    assert sum(row["status"] == "active" for row in rows) == 3
    assert sum(row["status"] == "revoked" for row in rows) == 5
    assert all(row["grant_generation"] == 1 for row in rows)
    assert isinstance(first_token["access_token"], str)
    assert settings.ready_file.exists()

    first_namespace = first_manifest["namespace"]
    first_profile = e2e_runtime.credential_profile_path(settings.ready_file).read_text()
    e2e_runtime.remove_ready_file(settings.ready_file)
    e2e_runtime.remove_fixture_artifacts(settings)
    await e2e_runtime._reset_database(dsn)
    e2e_runtime.clear_git_fixture_root(git_root)

    second_manifest, second_credentials = await e2e_runtime._seed_app_lifecycle(
        dsn,
        origin=settings.origin,
        fixture_origin=settings.fixture_origin,
    )
    second_token = await app_identity_service.exchange_app_credential(
        second_credentials[e2e_runtime.CREDENTIAL_VARIABLES[5]],
        correlation_id="runtime-reset-proof",
    )
    second_credentials[e2e_runtime.CREDENTIAL_VARIABLES[6]] = second_token["access_token"]
    e2e_runtime.write_fixture_artifacts(settings, second_manifest, second_credentials)
    e2e_runtime.write_ready_file(settings.ready_file, e2e_runtime.ready_payload(settings))

    manifest_path, profile_path = e2e_runtime.fixture_artifact_paths(settings)
    assert second_manifest["namespace"] != first_namespace
    assert json.loads(manifest_path.read_text())["namespace"] == second_manifest["namespace"]
    assert first_namespace not in manifest_path.read_text()
    assert first_profile != profile_path.read_text()
    assert os.stat(manifest_path).st_mode & 0o777 == 0o600
    assert os.stat(profile_path).st_mode & 0o777 == 0o600
    assert os.stat(settings.ready_file).st_mode & 0o777 == 0o600
