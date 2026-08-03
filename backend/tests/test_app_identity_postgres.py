"""Live-PostgreSQL security scenarios for app identity and policy."""

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
from app.exceptions import AuthenticationError, ConflictError, ForbiddenError

pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[1]
_INIT_SQL = (_BACKEND / "app" / "db" / "init.sql").read_text()
_MIGRATIONS = [
    _BACKEND / "app" / "db" / "migrations" / "047_app_registry.py",
    _BACKEND / "app" / "db" / "migrations" / "051_app_credentials.py",
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
        pytest.skip(f"Postgres not reachable at {_DSN}")
    admin = await asyncpg.connect(_DSN)
    name = f"akb_app_identity_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    pool = await asyncpg.create_pool(_database_dsn(name), min_size=1, max_size=8)
    try:
        async with pool.acquire() as conn:
            await conn.execute(_INIT_SQL)
            for migration in _MIGRATIONS:
                await _load_migration(migration).migrate(conn=conn)
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


@pytest.fixture
async def identity_pool(monkeypatch):
    from app.services import app_identity_service, auth_service

    async with _fresh_database() as pool:
        async def _get_pool():
            return pool

        monkeypatch.setattr(app_identity_service, "get_pool", _get_pool)
        monkeypatch.setattr(auth_service, "get_pool", _get_pool)
        monkeypatch.setattr(
            settings,
            "jwt_secret",
            "live-user-signing-material-long-enough",
            raising=False,
        )
        monkeypatch.setattr(
            settings,
            "app_token_secret",
            "live-app-signing-material-separate-and-long",
            raising=False,
        )
        monkeypatch.setattr(settings, "app_token_ttl_seconds", 300, raising=False)
        monkeypatch.setattr(
            settings,
            "app_credential_overlap_seconds",
            120,
            raising=False,
        )
        yield pool


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


async def _release(pool, app_id: uuid.UUID) -> uuid.UUID:
    manifest = json.dumps({"steps": [{"id": "prepare"}]}, separators=(",", ":"))
    checksum = hashlib.sha256(manifest.encode()).hexdigest()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO app_releases (
                app_id, version, manifest, manifest_checksum
            ) VALUES ($1, $2, $3::jsonb, $4)
            RETURNING id
            """,
            app_id,
            f"1.0.{uuid.uuid4().int % 100000}",
            manifest,
            checksum,
        )


async def _install(
    pool,
    app_id: uuid.UUID,
    vault_id: uuid.UUID,
    *,
    capability: str,
    resource_kind: str,
    resource_key: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    release_id = await _release(pool, app_id)
    async with pool.acquire() as conn:
        installation_id = await conn.fetchval(
            """
            INSERT INTO vault_app_installations (
                app_id, vault_id, desired_release_id,
                current_release_id, lifecycle
            ) VALUES ($1, $2, $3, $3, 'active')
            RETURNING id
            """,
            app_id,
            vault_id,
            release_id,
        )
        grant_id = await conn.fetchval(
            """
            INSERT INTO installation_grants (
                installation_id, generation, capabilities, issuer
            ) VALUES ($1, 1, $2, 'fixture')
            RETURNING id
            """,
            installation_id,
            [capability],
        )
        await conn.execute(
            """
            INSERT INTO app_owned_resources (
                installation_id, vault_id, resource_kind, resource_key
            ) VALUES ($1, $2, $3, $4)
            """,
            installation_id,
            vault_id,
            resource_kind,
            resource_key,
        )
    return installation_id, grant_id


async def _issue(pool, app_id: uuid.UUID, deployment: str):
    from app.services.app_identity_service import issue_app_credential

    return await issue_app_credential(
        app_id,
        deployment,
        actor="operator",
        actor_id=str(uuid.uuid4()),
        correlation_id=str(uuid.uuid4()),
    )


async def _exchange(raw_credential: str):
    from app.services.app_identity_service import exchange_app_credential

    return await exchange_app_credential(
        raw_credential,
        correlation_id=str(uuid.uuid4()),
    )


async def test_migration_reapply_and_plaintext_never_persists(identity_pool):
    from app.services.auth_service import resolve_token
    from app.services.app_identity_service import list_app_credentials

    async with identity_pool.acquire() as conn:
        await _load_migration(_MIGRATIONS[1]).migrate(conn=conn)
        await _load_migration(_MIGRATIONS[1]).migrate(conn=conn)

    app_a = await _app(identity_pool, "app-a")
    app_b = await _app(identity_pool, "app-b")
    issued_a = await _issue(identity_pool, app_a, "production")
    issued_b = await _issue(identity_pool, app_b, "production")
    issued_a_staging = await _issue(identity_pool, app_a, "staging")

    assert len({issued_a["credential"], issued_b["credential"], issued_a_staging["credential"]}) == 3
    listed = await list_app_credentials(app_a)
    assert {(item["deployment"], item["generation"]) for item in listed} == {
        ("production", 1),
        ("staging", 1),
    }
    assert all("credential" not in item and "credential_hash" not in item for item in listed)
    assert await resolve_token(f"Bearer {issued_a['credential']}") is None

    exchanged = await _exchange(issued_a["credential"])
    assert await resolve_token(f"Bearer {exchanged['access_token']}") is None

    async with identity_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT credential_hash, credential_prefix, to_jsonb(app_credentials.*) AS stored "
            "FROM app_credentials"
        )
    for row in rows:
        stored = str(row["stored"])
        for issued in (issued_a, issued_b, issued_a_staging):
            assert issued["credential"] not in stored
        assert len(row["credential_hash"]) == 64
        assert row["credential_prefix"].startswith("akb_app_")


async def test_rotation_overlap_stale_token_and_revoke(identity_pool):
    from app.services.app_identity_service import (
        resolve_app_authorization,
        revoke_app_credential,
        rotate_app_credential,
    )

    app_id = await _app(identity_pool, "rotate")
    first = await _issue(identity_pool, app_id, "production")
    first_exchange = await _exchange(first["credential"])
    assert await resolve_app_authorization(
        f"Bearer {first_exchange['access_token']}"
    ) is not None

    second = await rotate_app_credential(
        app_id,
        uuid.UUID(first["credential_id"]),
        actor="operator",
        actor_id=str(uuid.uuid4()),
        correlation_id=str(uuid.uuid4()),
    )
    assert second["generation"] == 2
    assert await resolve_app_authorization(
        f"Bearer {first_exchange['access_token']}"
    ) is None

    overlap_exchange = await _exchange(first["credential"])
    overlap_principal = await resolve_app_authorization(
        f"Bearer {overlap_exchange['access_token']}"
    )
    assert overlap_principal is not None
    assert overlap_principal.credential_generation == 2
    assert str(overlap_principal.credential_id) == second["credential_id"]

    async with identity_pool.acquire() as conn:
        await conn.execute(
            "UPDATE app_credentials SET overlap_until = NOW() - INTERVAL '1 second' "
            "WHERE id = $1",
            uuid.UUID(first["credential_id"]),
        )
    with pytest.raises(AuthenticationError):
        await _exchange(first["credential"])

    second_exchange = await _exchange(second["credential"])
    await revoke_app_credential(
        app_id,
        uuid.UUID(second["credential_id"]),
        actor="operator",
        actor_id=str(uuid.uuid4()),
        correlation_id=str(uuid.uuid4()),
    )
    assert await resolve_app_authorization(
        f"Bearer {second_exchange['access_token']}"
    ) is None
    with pytest.raises(AuthenticationError):
        await _exchange(second["credential"])

    retry = await revoke_app_credential(
        app_id,
        uuid.UUID(second["credential_id"]),
        actor="operator",
        actor_id=str(uuid.uuid4()),
        correlation_id=str(uuid.uuid4()),
    )
    assert retry["status"] == "revoked"


async def test_concurrent_rotation_has_one_winner(identity_pool):
    from app.services.app_identity_service import rotate_app_credential

    app_id = await _app(identity_pool, "concurrent")
    issued = await _issue(identity_pool, app_id, "production")
    credential_id = uuid.UUID(issued["credential_id"])

    async def rotate_once():
        return await rotate_app_credential(
            app_id,
            credential_id,
            actor="operator",
            actor_id=str(uuid.uuid4()),
            correlation_id=str(uuid.uuid4()),
        )

    results = await asyncio.gather(rotate_once(), rotate_once(), return_exceptions=True)
    assert sum(isinstance(item, dict) for item in results) == 1
    assert sum(isinstance(item, ConflictError) for item in results) == 1

    async with identity_pool.acquire() as conn:
        generations = await conn.fetch(
            "SELECT generation, status FROM app_credentials "
            "WHERE app_id = $1 ORDER BY generation",
            app_id,
        )
    assert [(row["generation"], row["status"]) for row in generations] == [
        (1, "rotated"),
        (2, "active"),
    ]


async def test_live_grant_ownership_and_structural_default_deny(identity_pool):
    from app.services.app_identity_service import (
        authorize_app_request,
        resolve_app_authorization,
    )

    app_a = await _app(identity_pool, "policy-a")
    app_b = await _app(identity_pool, "policy-b")
    vault_a = await _vault(identity_pool, "policy-a")
    vault_b = await _vault(identity_pool, "policy-b")
    resource_kind = "managed_table"
    resource_a = f"resource-{uuid.uuid4().hex}"
    resource_b = f"resource-{uuid.uuid4().hex}"
    installation_a, grant_a = await _install(
        identity_pool,
        app_a,
        vault_a,
        capability="inventory:read",
        resource_kind=resource_kind,
        resource_key=resource_a,
    )
    await _install(
        identity_pool,
        app_b,
        vault_b,
        capability="inventory:read",
        resource_kind=resource_kind,
        resource_key=resource_b,
    )
    issued = await _issue(identity_pool, app_a, "production")
    exchanged = await _exchange(issued["credential"])
    principal = await resolve_app_authorization(
        f"Bearer {exchanged['access_token']}"
    )
    assert principal is not None

    await authorize_app_request(
        principal,
        vault_id=vault_a,
        capability="inventory:read",
        resource_kind=resource_kind,
        resource_key=resource_a,
        correlation_id=str(uuid.uuid4()),
    )
    for denied in (
        {"vault_id": vault_b, "resource_key": resource_b},
        {"vault_id": vault_a, "resource_key": resource_b},
        {"vault_id": vault_a, "resource_key": f"foreign-{uuid.uuid4().hex}"},
    ):
        with pytest.raises(ForbiddenError):
            await authorize_app_request(
                principal,
                vault_id=denied["vault_id"],
                capability="inventory:read",
                resource_kind=resource_kind,
                resource_key=denied["resource_key"],
                correlation_id=str(uuid.uuid4()),
            )

    for capability in (
        "document:read",
        "table:any",
        "raw_sql",
        "user:impersonate",
    ):
        with pytest.raises(ForbiddenError):
            await authorize_app_request(
                principal,
                vault_id=vault_a,
                capability=capability,
                correlation_id=str(uuid.uuid4()),
            )

    async with identity_pool.acquire() as conn:
        await conn.execute(
            "UPDATE app_owned_resources SET status = 'retained' "
            "WHERE installation_id = $1",
            installation_a,
        )
    with pytest.raises(ForbiddenError):
        await authorize_app_request(
            principal,
            vault_id=vault_a,
            capability="inventory:read",
            resource_kind=resource_kind,
            resource_key=resource_a,
            correlation_id=str(uuid.uuid4()),
        )

    async with identity_pool.acquire() as conn:
        await conn.execute(
            "UPDATE installation_grants "
            "SET status = 'revoked', revoked_at = NOW() WHERE id = $1",
            grant_a,
        )
    with pytest.raises(ForbiddenError):
        await authorize_app_request(
            principal,
            vault_id=vault_a,
            capability="inventory:read",
            correlation_id=str(uuid.uuid4()),
        )
