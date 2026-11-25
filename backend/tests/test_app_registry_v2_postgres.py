"""PostgreSQL proof for v2 release replay, identity, and conflict semantics."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from tests.helpers.pg_database import database_template, temporary_database

from app.exceptions import ConflictError, ValidationError
from app.services import app_resource_service as resources
from app.services import app_registry_service as registry
from app.services import app_rollout_service as rollout
from app.services.auth_service import AuthenticatedUser

pytestmark = [pytest.mark.asyncio, pytest.mark.live_services]

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


async def _initialize(conn):
    await conn.execute(INIT_SQL)
    for migration in MIGRATIONS:
        await _load_migration(migration).migrate(conn=conn)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def db_template():
    async with database_template(DSN, "akb_app_registry_v2_template_", _initialize) as template:
        yield template


@asynccontextmanager
async def _fresh_database(db_template):
    async with temporary_database(DSN, "akb_app_registry_v2_", template=db_template) as database:
        conn = await asyncpg.connect(database.dsn)
        try:
            yield conn
        finally:
            await conn.close()


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


def _manifest_with_reordered_create_columns(app_key: str, version: str) -> dict:
    columns = [
        {"name": "alpha", "type": "text"},
        {"name": "zeta", "type": "text", "required": True},
    ]
    descriptor = {
        "name": "ordered_table",
        "columns": columns,
        "unique_keys": [],
        "indexes": [],
    }
    create_step = {
        "id": "create_ordered_table",
        "phase": "expand",
        "operation": "create_table",
        "payload": {
            "table": descriptor["name"],
            "columns": list(reversed(columns)),
            "unique_keys": [],
            "indexes": [],
        },
    }
    canonical_step = {
        **create_step,
        "payload": {
            **create_step["payload"],
            "columns": columns,
        },
    }
    create_step["checksum"] = hashlib.sha256(
        json.dumps(
            canonical_step,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    manifest = {
        "manifest_version": 2,
        "app_key": app_key,
        "source_revision": "a" * 40,
        "image_digest": "sha256:" + "b" * 64,
        "schema_version": 3,
        "schema": {"tables": [descriptor]},
        "transition_plans": [{"source": "fresh", "steps": [create_step]}],
    }
    manifest["schema"]["fingerprint"] = resources.canonical_table_fingerprint(
        manifest["schema"]["tables"]
    )
    assert version
    return manifest


async def test_v2_release_replays_byte_equivalent_and_conflicts_without_partial_state(
    monkeypatch, db_template,
):
    async with _fresh_database(db_template) as conn:
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


async def test_v2_release_replays_canonical_equivalent_stored_manifest(monkeypatch, db_template):
    async with _fresh_database(db_template) as conn:
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
        app_key = f"registry-v2-order-{uuid.uuid4().hex}"
        app = await registry.create_app_definition(
            app_key=app_key,
            display_name="Registry v2 ordering",
            description=None,
            metadata={},
            user=user,
            correlation_id="registry-v2-order-app",
        )
        version = "1.0.0"
        manifest = _manifest_with_reordered_create_columns(app_key, version)
        checksum = rollout.manifest_checksum(manifest, version=version)

        async with conn.transaction():
            release_id = await conn.fetchval(
                """
                INSERT INTO app_releases(app_id, version, manifest, manifest_checksum)
                VALUES($1, $2, $3::jsonb, $4)
                RETURNING id
                """,
                uuid.UUID(app["id"]),
                version,
                json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
                checksum,
            )

        projection = await registry.get_app_release(
            app["id"],
            release_id,
            user=user,
            correlation_id="registry-v2-order-read",
        )
        replay = await registry.create_app_release(
            app["id"],
            version=projection["version"],
            manifest=projection["manifest"],
            manifest_checksum=projection["manifest_checksum"],
            user=user,
            correlation_id="registry-v2-order-replay",
        )

        assert replay["replayed"] is True
        assert replay["id"] == str(release_id)
