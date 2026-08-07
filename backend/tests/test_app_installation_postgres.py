"""Live-PostgreSQL proof for app installation lifecycle atomicity."""

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

from app.config import settings
from app.exceptions import ConflictError, ForbiddenError
from app.services import app_installation_service as installation
from app.services import access_service
from app.services.app_identity_service import AppPrincipal
from app.services.auth_service import AuthenticatedUser

pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[1]
_INIT_SQL = (_BACKEND / "app" / "db" / "init.sql").read_text()
_MIGRATIONS = [
    _BACKEND / "app" / "db" / "migrations" / "044_vault_write_policy.py",
    _BACKEND / "app" / "db" / "migrations" / "047_app_registry.py",
    _BACKEND / "app" / "db" / "migrations" / "051_app_credentials.py",
    _BACKEND / "app" / "db" / "migrations" / "052_app_inventory.py",
]
_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",  # pragma: allowlist secret
)
_ADMIN_ID = uuid.uuid4()


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
    name = f"akb_app_installation_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    pool = await asyncpg.create_pool(_database_dsn(name), min_size=1, max_size=12)
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


async def _app(pool, label: str) -> uuid.UUID:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO app_definitions (app_key) VALUES ($1) RETURNING id",
            f"{label}-{uuid.uuid4().hex}",
        )


async def _release(pool, app_id: uuid.UUID, version: str, fingerprint: str = "a" * 64):
    manifest = {
        "steps": [{"id": "prepare"}],
        "expected_schema_fingerprint": fingerprint,
    }
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


async def _vault(pool, label: str) -> uuid.UUID:
    name = f"{label}-{uuid.uuid4().hex[:10]}"
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO vaults (name, git_path) VALUES ($1, $2) RETURNING id",
            name,
            f"/tmp/{name}.git",
        )


async def _user(pool, label: str) -> uuid.UUID:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO users (username, email, password_hash)
            VALUES ($1, $2, 'fixture')
            RETURNING id
            """,
            f"{label}-{uuid.uuid4().hex}",
            f"{label}-{uuid.uuid4().hex}@example.invalid",
        )


def _admin() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=str(_ADMIN_ID),
        username="system-operator",
        email="operator@example.invalid",
        display_name=None,
        is_admin=True,
        auth_method="jwt",
    )


async def _command(pool, app_id, vault_id, release_id, *, mode="install", capabilities=None):
    return await installation.command_installation(
        app_id,
        vault_id,
        release_id=release_id,
        capabilities=capabilities or ["installation:read", "inventory:read"],
        mode=mode,
        user=_admin(),
        correlation_id=str(uuid.uuid4()),
    )


@pytest.fixture
async def lifecycle_pool(monkeypatch):
    async with _fresh_database() as pool:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO users (id, username, email, password_hash, is_admin)
                VALUES ($1, 'installation-admin', 'installation-admin@example.invalid', 'fixture', TRUE)
                """,
                _ADMIN_ID,
            )
        monkeypatch.setattr(installation, "get_pool", lambda: pool)
        monkeypatch.setattr(access_service, "get_pool", lambda: pool)
        monkeypatch.setattr(settings, "app_token_secret", "lifecycle-test-app-secret-long", raising=False)
        yield pool


async def test_install_replays_sequentially_and_concurrently(lifecycle_pool):
    app_id = await _app(lifecycle_pool, "install")
    release_a = await _release(lifecycle_pool, app_id, "1.0.0")
    release_b = await _release(lifecycle_pool, app_id, "2.0.0")
    vault_id = await _vault(lifecycle_pool, "install")

    first = await _command(lifecycle_pool, app_id, vault_id, release_a)
    replay = await _command(lifecycle_pool, app_id, vault_id, release_a)
    assert first["command_status"] == "accepted"
    assert first["replayed"] is False
    assert first["lifecycle"] == "installing"
    assert first["desired_grant_generation"] == 1
    assert replay["command_status"] == "already_applied"
    assert replay["replayed"] is True
    assert replay["installation_id"] == first["installation_id"]
    assert replay["latest_grant"]["generation"] == 1

    concurrent = await asyncio.gather(
        *[_command(lifecycle_pool, app_id, vault_id, release_a) for _ in range(8)]
    )
    assert {item["installation_id"] for item in concurrent} == {first["installation_id"]}
    assert {item["desired_grant_generation"] for item in concurrent} == {1}
    assert all(item["replayed"] for item in concurrent)

    conflicts = await asyncio.gather(
        *[
            _command(
                lifecycle_pool,
                app_id,
                vault_id,
                release_b,
                capabilities=["installation:read"],
            )
            for _ in range(4)
        ],
        return_exceptions=True,
    )
    assert all(isinstance(result, ConflictError) for result in conflicts)
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


async def test_non_admin_denial_is_generic_before_registry_lookup(lifecycle_pool):
    app_id = await _app(lifecycle_pool, "authority")
    release_id = await _release(lifecycle_pool, app_id, "1.0.0")
    vault_id = await _vault(lifecycle_pool, "authority")
    reader_id = await _user(lifecycle_pool, "reader")
    async with lifecycle_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO vault_access (vault_id, user_id, role) VALUES ($1, $2, 'reader')",
            vault_id,
            reader_id,
        )
    reader = AuthenticatedUser(
        user_id=str(reader_id),
        username="reader",
        email="reader@example.invalid",
        display_name=None,
        is_admin=False,
        auth_method="jwt",
    )

    with pytest.raises(ForbiddenError) as existing:
        await installation.command_installation(
            app_id,
            vault_id,
            release_id=release_id,
            capabilities=["installation:read"],
            mode="install",
            user=reader,
            correlation_id=str(uuid.uuid4()),
        )
    with pytest.raises(ForbiddenError) as missing:
        await installation.command_installation(
            uuid.uuid4(),
            uuid.uuid4(),
            release_id=uuid.uuid4(),
            capabilities=["installation:read"],
            mode="install",
            user=reader,
            correlation_id=str(uuid.uuid4()),
        )
    assert existing.value.message == missing.value.message == "Installation request denied"
    async with lifecycle_pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM vault_app_installations") == 0


async def test_app_status_requires_a_live_installation_read_grant(lifecycle_pool):
    app_id = await _app(lifecycle_pool, "app-status")
    release_id = await _release(lifecycle_pool, app_id, "1.0.0")
    vault_id = await _vault(lifecycle_pool, "app-status")
    installed = await _command(lifecycle_pool, app_id, vault_id, release_id)
    installation_id = uuid.UUID(installed["installation_id"])
    async with lifecycle_pool.acquire() as conn:
        await conn.execute(
            "UPDATE vault_app_installations SET current_release_id = desired_release_id, lifecycle = 'active' WHERE id = $1",
            installation_id,
        )
    principal = AppPrincipal(
        app_id=app_id,
        credential_id=uuid.uuid4(),
        credential_generation=1,
        deployment="fixture",
        token_id="fixture-token",
        expires_at=None,  # type: ignore[arg-type]
    )
    status = await installation.get_app_installation_status(
        principal,
        vault_id,
        correlation_id=str(uuid.uuid4()),
    )
    assert status["lifecycle"] == "active"
    async with lifecycle_pool.acquire() as conn:
        await conn.execute(
            "UPDATE installation_grants SET status = 'revoked', revoked_at = NOW() WHERE installation_id = $1",
            installation_id,
        )
    with pytest.raises(ForbiddenError):
        await installation.get_app_installation_status(
            principal,
            vault_id,
            correlation_id=str(uuid.uuid4()),
        )


async def test_uninstall_revokes_immediately_and_replays_without_deleting_resources(
    lifecycle_pool,
):
    app_id = await _app(lifecycle_pool, "uninstall")
    release_id = await _release(lifecycle_pool, app_id, "1.0.0")
    vault_id = await _vault(lifecycle_pool, "uninstall")
    installed = await _command(lifecycle_pool, app_id, vault_id, release_id)
    installation_id = uuid.UUID(installed["installation_id"])
    async with lifecycle_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE vault_app_installations
               SET current_release_id = desired_release_id, lifecycle = 'active'
             WHERE id = $1
            """,
            installation_id,
        )
        await conn.execute(
            """
            INSERT INTO app_owned_resources (installation_id, vault_id, resource_kind, resource_key)
            VALUES ($1, $2, 'table', 'owned-table')
            """,
            installation_id,
            vault_id,
        )

    deleted = await installation.uninstall_installation(
        app_id,
        vault_id,
        user=_admin(),
        correlation_id=str(uuid.uuid4()),
    )
    replay = await installation.uninstall_installation(
        app_id,
        vault_id,
        user=_admin(),
        correlation_id=str(uuid.uuid4()),
    )
    assert deleted["command_status"] == "accepted"
    assert deleted["lifecycle"] == "uninstalled"
    assert deleted["latest_grant"]["status"] == "revoked"
    assert deleted["active_grant"] is None
    assert deleted["owned_resources"] == [
        {"kind": "table", "key": "owned-table", "status": "retained"}
    ]
    assert replay["command_status"] == "already_applied"
    assert replay["installation_id"] == deleted["installation_id"]
    assert replay["desired_grant_generation"] == deleted["desired_grant_generation"]


async def test_restore_requires_compatibility_and_uses_next_generation(lifecycle_pool):
    app_id = await _app(lifecycle_pool, "restore")
    release_id = await _release(lifecycle_pool, app_id, "1.0.0")
    vault_id = await _vault(lifecycle_pool, "restore")
    installed = await _command(lifecycle_pool, app_id, vault_id, release_id)
    installation_id = uuid.UUID(installed["installation_id"])
    async with lifecycle_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE vault_app_installations
               SET current_release_id = desired_release_id, lifecycle = 'active'
             WHERE id = $1
            """,
            installation_id,
        )
        await conn.execute(
            """
            INSERT INTO app_installation_observed_states (
                installation_id, app_id, vault_id, observed_generation,
                observed_at, observed_release_id, schema_fingerprint,
                observed_grant_generation
            ) VALUES ($1, $2, $3, 1, NOW(), $4, $5, 1)
            """,
            installation_id,
            app_id,
            vault_id,
            release_id,
            "a" * 64,
        )
        await conn.execute(
            """
            INSERT INTO app_owned_resources (installation_id, vault_id, resource_kind, resource_key)
            VALUES ($1, $2, 'schema', 'owned-schema')
            """,
            installation_id,
            vault_id,
        )

    await installation.uninstall_installation(
        app_id,
        vault_id,
        user=_admin(),
        correlation_id=str(uuid.uuid4()),
    )
    restored = await _command(
        lifecycle_pool,
        app_id,
        vault_id,
        release_id,
        mode="restore",
    )
    replay = await _command(
        lifecycle_pool,
        app_id,
        vault_id,
        release_id,
        mode="restore",
    )
    assert restored["lifecycle"] == "active"
    assert restored["desired_grant_generation"] == 2
    assert restored["active_grant"]["generation"] == 2
    assert restored["owned_resources"][0]["status"] == "owned"
    assert replay["command_status"] == "already_applied"
    assert replay["desired_grant_generation"] == 2

    app_id_2 = await _app(lifecycle_pool, "restore-mismatch")
    release_id_2 = await _release(lifecycle_pool, app_id_2, "1.0.0")
    vault_id_2 = await _vault(lifecycle_pool, "restore-mismatch")
    installed_2 = await _command(lifecycle_pool, app_id_2, vault_id_2, release_id_2)
    installation_id_2 = uuid.UUID(installed_2["installation_id"])
    async with lifecycle_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE vault_app_installations
               SET current_release_id = desired_release_id, lifecycle = 'active'
             WHERE id = $1
            """,
            installation_id_2,
        )
        await conn.execute(
            """
            INSERT INTO app_installation_observed_states (
                installation_id, app_id, vault_id, observed_generation,
                observed_at, observed_release_id, schema_fingerprint,
                observed_grant_generation
            ) VALUES ($1, $2, $3, 1, NOW(), $4, $5, 1)
            """,
            installation_id_2,
            app_id_2,
            vault_id_2,
            release_id_2,
            "b" * 64,
        )
    await installation.uninstall_installation(
        app_id_2,
        vault_id_2,
        user=_admin(),
        correlation_id=str(uuid.uuid4()),
    )
    with pytest.raises(ConflictError):
        await _command(
            lifecycle_pool,
            app_id_2,
            vault_id_2,
            release_id_2,
            mode="restore",
        )
    async with lifecycle_pool.acquire() as conn:
        state = await conn.fetchrow(
            "SELECT desired_release_id, current_release_id, lifecycle, grant_generation FROM vault_app_installations WHERE id = $1",
            installation_id_2,
        )
    assert state["desired_release_id"] is None
    assert state["current_release_id"] == release_id_2
    assert state["lifecycle"] == "uninstalled"
    assert state["grant_generation"] == 1


async def test_fresh_rejects_retained_resources_and_clears_old_current_pointer(
    lifecycle_pool,
):
    app_id = await _app(lifecycle_pool, "fresh")
    release_a = await _release(lifecycle_pool, app_id, "1.0.0")
    release_b = await _release(lifecycle_pool, app_id, "2.0.0")
    vault_with_resource = await _vault(lifecycle_pool, "fresh-retained")
    installed = await _command(lifecycle_pool, app_id, vault_with_resource, release_a)
    installation_id = uuid.UUID(installed["installation_id"])
    async with lifecycle_pool.acquire() as conn:
        await conn.execute(
            "UPDATE vault_app_installations SET current_release_id = desired_release_id, lifecycle = 'active' WHERE id = $1",
            installation_id,
        )
        await conn.execute(
            "INSERT INTO app_owned_resources (installation_id, vault_id, resource_kind, resource_key) VALUES ($1, $2, 'table', 'retained-table')",
            installation_id,
            vault_with_resource,
        )
    await installation.uninstall_installation(
        app_id,
        vault_with_resource,
        user=_admin(),
        correlation_id=str(uuid.uuid4()),
    )
    with pytest.raises(ConflictError):
        await _command(
            lifecycle_pool,
            app_id,
            vault_with_resource,
            release_b,
            mode="fresh",
        )

    vault_without_resource = await _vault(lifecycle_pool, "fresh-empty")
    installed_empty = await _command(lifecycle_pool, app_id, vault_without_resource, release_a)
    empty_id = uuid.UUID(installed_empty["installation_id"])
    async with lifecycle_pool.acquire() as conn:
        await conn.execute(
            "UPDATE vault_app_installations SET current_release_id = desired_release_id, lifecycle = 'active' WHERE id = $1",
            empty_id,
        )
    await installation.uninstall_installation(
        app_id,
        vault_without_resource,
        user=_admin(),
        correlation_id=str(uuid.uuid4()),
    )
    fresh = await _command(
        lifecycle_pool,
        app_id,
        vault_without_resource,
        release_b,
        mode="fresh",
    )
    assert fresh["lifecycle"] == "installing"
    assert fresh["current_release"] is None
    assert fresh["desired_release"]["id"] == str(release_b)
    assert fresh["desired_grant_generation"] == 2
