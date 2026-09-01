"""PostgreSQL proof for v2 release replay, identity, and conflict semantics."""

from __future__ import annotations

import importlib.util
import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import pytest

from app.exceptions import ConflictError, ValidationError
from app.services import app_registry_service as registry
from app.services import app_rollout_service as rollout
from app.services.auth_service import AuthenticatedUser

pytestmark = pytest.mark.asyncio

BACKEND = Path(__file__).resolve().parents[1]
INIT_SQL = (BACKEND / "app" / "db" / "init.sql").read_text()
MIGRATIONS = [
    BACKEND / "app" / "db" / "migrations" / "047_app_registry.py",
    BACKEND / "app" / "db" / "migrations" / "095_app_release_manifest_v2.py",
]
DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",  # pragma: allowlist secret
)


def _load_migration(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _can_connect() -> bool:
    try:
        conn = await asyncpg.connect(DSN, timeout=2)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


def _database_dsn(name: str) -> str:
    base, _ = DSN.rsplit("/", 1)
    return f"{base}/{name}"


@asynccontextmanager
async def _fresh_database():
    if not await _can_connect():
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"Required PostgreSQL is not reachable at {DSN}")
        pytest.skip(f"Postgres not reachable at {DSN}")
    admin = await asyncpg.connect(DSN)
    name = f"akb_app_registry_v2_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    conn = await asyncpg.connect(_database_dsn(name))
    try:
        await conn.execute(INIT_SQL)
        for migration in MIGRATIONS:
            await _load_migration(migration).migrate(conn=conn)
        yield conn
    finally:
        await conn.close()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


def _admin() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=str(uuid.uuid4()),
        username="registry-v2-admin",
        email="registry-v2-admin@example.invalid",
        display_name=None,
        is_admin=True,
        auth_method="jwt",
    )


def _manifest(app_key: str) -> dict:
    return {
        "manifest_version": 2,
        "app_key": app_key,
        "source_revision": "a" * 40,
        "image_digest": "sha256:" + "b" * 64,
        "schema_version": 3,
        "schema": {"tables": []},
        "transition_plans": [{"source": "fresh", "steps": []}],
    }


async def test_v2_release_replays_byte_equivalent_and_conflicts_without_partial_state(
    monkeypatch,
):
    async with _fresh_database() as conn:
        class _Pool:
            def acquire(self):
                return self

            async def __aenter__(self):
                return conn

            async def __aexit__(self, *_args):
                return False

        async def get_test_pool():
            return _Pool()

        monkeypatch.setattr(registry, "get_pool", get_test_pool)
        monkeypatch.setattr(registry, "record_app_audit", lambda *_args, **_kwargs: None)
        user = _admin()
        app_key = f"registry-v2-{uuid.uuid4().hex}"
        app = await registry.create_app_definition(
            app_key=app_key,
            display_name="Registry v2",
            description=None,
            metadata={},
            user=user,
            correlation_id="registry-v2-app",
        )
        manifest = _manifest(app_key)
        version = "1.0.0"
        checksum = rollout.manifest_checksum(manifest, version=version)

        first = await registry.create_app_release(
            app["id"],
            version=version,
            manifest=manifest,
            manifest_checksum=checksum,
            user=user,
            correlation_id="registry-v2-first",
        )
        replay_manifest = json.loads(json.dumps(manifest))
        replay = await registry.create_app_release(
            app["id"],
            version=version,
            manifest=replay_manifest,
            manifest_checksum=checksum,
            user=user,
            correlation_id="registry-v2-replay",
        )
        assert first["replayed"] is False
        assert replay["replayed"] is True
        assert replay["id"] == first["id"]

        changed = json.loads(json.dumps(manifest))
        changed["source_revision"] = "c" * 40
        changed_checksum = rollout.manifest_checksum(changed, version=version)
        with pytest.raises(ConflictError):
            await registry.create_app_release(
                app["id"],
                version=version,
                manifest=changed,
                manifest_checksum=changed_checksum,
                user=user,
                correlation_id="registry-v2-conflict",
            )

        with pytest.raises(ConflictError):
            await registry.create_app_release(
                app["id"],
                version="2.0.0",
                manifest={**manifest, "app_key": "foreign-app"},
                manifest_checksum=rollout.manifest_checksum(
                    {**manifest, "app_key": "foreign-app"}, version="2.0.0"
                ),
                user=user,
                correlation_id="registry-v2-app-key-conflict",
            )

        assert await conn.fetchval(
            "SELECT count(*) FROM app_releases WHERE app_id=$1", uuid.UUID(app["id"])
        ) == 1

        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(
                """
                INSERT INTO app_releases(app_id, version, manifest, manifest_checksum)
                VALUES($1, '3.0.0', '{"manifest_version":1,"steps":[]}'::jsonb, $2)
                """,
                uuid.UUID(app["id"]),
                "a" * 64,
            )

        with pytest.raises(ValidationError):
            await registry.create_app_release(
                app["id"],
                version="3.0.0",
                manifest={"manifest_version": 1, "steps": []},
                manifest_checksum="a" * 64,
                user=user,
                correlation_id="registry-v2-v1-reject",
            )
