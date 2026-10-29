"""Live-PostgreSQL proof for app installation lifecycle atomicity."""

from __future__ import annotations

import asyncio
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
from app.services import app_resource_service as resources
from app.services import app_rollout_service as rollout
from app.services import app_rollout_worker as worker
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
    _BACKEND / "app" / "db" / "migrations" / "062_app_rollout.py",
    _BACKEND / "app" / "db" / "migrations" / "073_app_rollout_resume.py",
    _BACKEND / "app" / "db" / "migrations" / "086_dynamic_table_rows_changed.py",
    _BACKEND / "app" / "db" / "migrations" / "095_app_release_manifest_v2.py",
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


async def _release(pool, app_id: uuid.UUID, version: str):
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


async def _vault(pool, label: str, *, owner_id: uuid.UUID | None = None) -> uuid.UUID:
    name = f"{label}-{uuid.uuid4().hex[:10]}"
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO vaults (name, git_path, owner_id) VALUES ($1, $2, $3) RETURNING id",
            name,
            f"/tmp/{name}.git",
            owner_id,
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


def _member_session(user_id: uuid.UUID, username: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=str(user_id),
        username=username,
        email=f"{username}@example.invalid",
        display_name=None,
        is_admin=False,
        auth_method="jwt",
    )


async def _command(pool, app_id, vault_id, release_id, *, mode="install", capabilities=None):
    return await installation.command_installation(
        app_id,
        vault_id,
        release_id=release_id,
        capabilities=capabilities or ["installation:read"],
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
        assert await conn.fetchval(
            "SELECT count(*) FROM app_rollout_jobs AS job JOIN app_rollout_targets AS target ON target.job_id = job.id WHERE job.app_id = $1 AND target.installation_id = $2",
            app_id,
            uuid.UUID(first["installation_id"]),
        ) == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM app_rollout_jobs AS job JOIN app_rollout_targets AS target ON target.job_id = job.id WHERE job.app_id = $1 AND target.installation_id = $2 AND job.requested_by_kind = 'admin'",
            app_id,
            uuid.UUID(first["installation_id"]),
        ) == 1


async def test_install_creates_one_durable_target_and_worker_applies_it(
    monkeypatch, lifecycle_pool
):
    monkeypatch.setattr(worker, "get_pool", lambda: lifecycle_pool)

    class _RoleSync:
        async def grant_table_in_conn(self, *_args):
            return None

    monkeypatch.setattr(worker, "get_role_sync", lambda: _RoleSync())
    app_id = await _app(lifecycle_pool, "install-worker")
    release_id = await _release(lifecycle_pool, app_id, "1.0.0")
    vault_a = await _vault(lifecycle_pool, "install-worker-a")
    vault_b = await _vault(lifecycle_pool, "install-worker-b")

    first = await _command(lifecycle_pool, app_id, vault_a, release_id)
    first_replay = await _command(lifecycle_pool, app_id, vault_a, release_id)
    second = await _command(lifecycle_pool, app_id, vault_b, release_id)
    second_replay = await _command(lifecycle_pool, app_id, vault_b, release_id)

    assert first["lifecycle"] == second["lifecycle"] == "installing"
    assert first_replay["replayed"] is True
    assert second_replay["replayed"] is True
    async with lifecycle_pool.acquire() as conn:
        jobs = await conn.fetch(
            """
            SELECT job.id, job.status, target.installation_id, target.vault_id
              FROM app_rollout_jobs AS job
              JOIN app_rollout_targets AS target ON target.job_id = job.id
             WHERE job.app_id = $1
             ORDER BY target.vault_id
            """,
            app_id,
        )
        grants = await conn.fetch(
            """
            SELECT installation.id AS installation_id, grant_row.capabilities
              FROM vault_app_installations AS installation
              JOIN installation_grants AS grant_row
                ON grant_row.installation_id = installation.id
             WHERE installation.app_id = $1
             ORDER BY installation.id
            """,
            app_id,
        )
    assert len(jobs) == 2
    assert len(grants) == 2
    assert all(row["capabilities"] == ["installation:read"] for row in grants)
    assert {row["installation_id"] for row in jobs} == {
        uuid.UUID(first["installation_id"]),
        uuid.UUID(second["installation_id"]),
    }
    assert {row["vault_id"] for row in jobs} == {vault_a, vault_b}
    assert {row["status"] for row in jobs} == {"pending"}
    async with lifecycle_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM app_rollout_jobs WHERE app_id = $1 AND requested_by_kind = 'admin'",
            app_id,
        ) == 2

    await worker.run_once()
    await worker.run_once()
    async with lifecycle_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM app_rollout_jobs WHERE app_id = $1 AND status = 'applied'",
            app_id,
        ) == 2
        assert await conn.fetchval(
            "SELECT count(*) FROM vault_app_installations WHERE app_id = $1 AND lifecycle = 'active'",
            app_id,
        ) == 2


async def test_fresh_install_queues_one_target_and_worker_applies_it(
    monkeypatch, lifecycle_pool
):
    monkeypatch.setattr(worker, "get_pool", lambda: lifecycle_pool)
    app_id = await _app(lifecycle_pool, "fresh-worker")
    release_id = await _release(lifecycle_pool, app_id, "2.0.0")
    vault_id = await _vault(lifecycle_pool, "fresh-worker")
    async with lifecycle_pool.acquire() as conn:
        installation_id = await conn.fetchval(
            "INSERT INTO vault_app_installations(app_id,vault_id,lifecycle) VALUES($1,$2,'uninstalled') RETURNING id",
            app_id,
            vault_id,
        )

    fresh = await _command(
        lifecycle_pool,
        app_id,
        vault_id,
        release_id,
        mode="fresh",
    )
    replay = await _command(
        lifecycle_pool,
        app_id,
        vault_id,
        release_id,
        mode="fresh",
    )
    assert fresh["command_status"] == "accepted"
    assert fresh["replayed"] is False
    assert fresh["lifecycle"] == "installing"
    assert replay["command_status"] == "already_applied"
    assert replay["replayed"] is True

    async with lifecycle_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM app_rollout_jobs AS job JOIN app_rollout_targets AS target ON target.job_id=job.id WHERE job.app_id=$1 AND target.installation_id=$2 AND job.release_id=$3 AND job.requested_by_kind='admin'",
            app_id,
            installation_id,
            release_id,
        ) == 1

    for _ in range(3):
        await worker.run_once()

    async with lifecycle_pool.acquire() as conn:
        state = await conn.fetchrow(
            "SELECT lifecycle,current_release_id,desired_release_id FROM vault_app_installations WHERE id=$1",
            installation_id,
        )
        assert await conn.fetchval(
            "SELECT status FROM app_rollout_jobs WHERE app_id=$1 AND release_id=$2",
            app_id,
            release_id,
        ) == "applied"
    assert state["lifecycle"] == "active"
    assert state["current_release_id"] == release_id
    assert state["desired_release_id"] == release_id


async def test_install_rollout_queue_failure_rolls_back_command(
    monkeypatch, lifecycle_pool
):
    app_id = await _app(lifecycle_pool, "install-rollback")
    release_id = await _release(lifecycle_pool, app_id, "1.0.0")
    vault_id = await _vault(lifecycle_pool, "install-rollback")

    async def fail_queue(*_args, **_kwargs):
        raise ConflictError("Simulated durable job insertion failure")

    monkeypatch.setattr(rollout, "enqueue_installation_rollout", fail_queue)

    with pytest.raises(ConflictError, match="Simulated durable job insertion failure"):
        await _command(lifecycle_pool, app_id, vault_id, release_id)

    async with lifecycle_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM vault_app_installations WHERE app_id=$1 AND vault_id=$2",
            app_id,
            vault_id,
        ) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM installation_grants AS grant_row JOIN vault_app_installations AS installation ON installation.id=grant_row.installation_id WHERE installation.app_id=$1 AND installation.vault_id=$2",
            app_id,
            vault_id,
        ) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM app_rollout_jobs WHERE app_id=$1",
            app_id,
        ) == 0


async def test_install_cannot_be_accepted_when_queue_returns_no_job(
    monkeypatch, lifecycle_pool
):
    app_id = await _app(lifecycle_pool, "install-no-queue")
    release_id = await _release(lifecycle_pool, app_id, "1.0.0")
    vault_id = await _vault(lifecycle_pool, "install-no-queue")

    async def no_job(*_args, **_kwargs):
        return None

    monkeypatch.setattr(rollout, "enqueue_installation_rollout", no_job)

    with pytest.raises(ConflictError, match="could not be queued"):
        await _command(lifecycle_pool, app_id, vault_id, release_id)

    async with lifecycle_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM vault_app_installations WHERE app_id=$1 AND vault_id=$2",
            app_id,
            vault_id,
        ) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM app_rollout_jobs WHERE app_id=$1",
            app_id,
        ) == 0


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


async def test_member_active_status_reads_live_lifecycle_and_requires_real_membership(
    lifecycle_pool,
):
    app_id = await _app(lifecycle_pool, "member-status")
    release_id = await _release(lifecycle_pool, app_id, "1.0.0")
    next_release_id = await _release(lifecycle_pool, app_id, "2.0.0")
    owner_id = await _user(lifecycle_pool, "member-owner")
    reader_id = await _user(lifecycle_pool, "member-reader")
    writer_id = await _user(lifecycle_pool, "member-writer")
    vault_admin_id = await _user(lifecycle_pool, "member-vault-admin")
    foreign_admin_id = await _user(lifecycle_pool, "foreign-vault-admin")
    outsider_id = await _user(lifecycle_pool, "member-outsider")
    vault_id = await _vault(lifecycle_pool, "member-status", owner_id=owner_id)
    foreign_vault_id = await _vault(lifecycle_pool, "member-foreign", owner_id=owner_id)
    async with lifecycle_pool.acquire() as conn:
        await conn.executemany(
            "INSERT INTO vault_access (vault_id, user_id, role, granted_by) VALUES ($1, $2, $3, $4)",
            [
                (vault_id, reader_id, "reader", _ADMIN_ID),
                (vault_id, writer_id, "writer", _ADMIN_ID),
                (vault_id, vault_admin_id, "admin", _ADMIN_ID),
                (vault_id, _ADMIN_ID, "admin", _ADMIN_ID),
                (foreign_vault_id, foreign_admin_id, "admin", _ADMIN_ID),
            ],
        )

    installed = await _command(lifecycle_pool, app_id, vault_id, release_id)
    installation_id = uuid.UUID(installed["installation_id"])
    async with lifecycle_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE vault_app_installations
               SET current_release_id = desired_release_id,
                   lifecycle = 'active',
                   blocked_reason = NULL
             WHERE id = $1
            """,
            installation_id,
        )

    members = [
        (owner_id, "owner"),
        (reader_id, "reader"),
        (writer_id, "writer"),
        (vault_admin_id, "vault-admin"),
    ]
    for user_id, label in members:
        before = await lifecycle_pool.fetchval(
            "SELECT count(*) FROM vault_app_installations WHERE id=$1", installation_id
        )
        status = await installation.get_member_installation_active_status(
            app_id,
            vault_id,
            user=_member_session(user_id, label),
            correlation_id=str(uuid.uuid4()),
        )
        after = await lifecycle_pool.fetchval(
            "SELECT count(*) FROM vault_app_installations WHERE id=$1", installation_id
        )
        assert status == {"active": True}
        assert after == before

    for lifecycle, desired, current, blocked_reason, expected_active in (
        ("installing", release_id, None, None, False),
        ("active", release_id, release_id, None, True),
        ("upgrading", next_release_id, release_id, None, False),
        ("blocked", next_release_id, release_id, "fixture_status_blocked", False),
        ("uninstalled", None, release_id, None, False),
    ):
        async with lifecycle_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE vault_app_installations
                   SET lifecycle = $2,
                       desired_release_id = $3,
                       current_release_id = $4,
                       blocked_reason = $5
                 WHERE id = $1
                """,
                installation_id,
                lifecycle,
                desired,
                current,
                blocked_reason,
            )
        status = await installation.get_member_installation_active_status(
            app_id,
            vault_id,
            user=_member_session(reader_id, "reader"),
            correlation_id=str(uuid.uuid4()),
        )
        assert status == {"active": expected_active}

    async with lifecycle_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE vault_app_installations
               SET lifecycle = 'active',
                   desired_release_id = $2,
                   current_release_id = $2,
                   blocked_reason = NULL
             WHERE id = $1
            """,
            installation_id,
            release_id,
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
            resources.canonical_table_fingerprint([]),
        )
    await installation.uninstall_installation(
        app_id,
        vault_id,
        user=_admin(),
        correlation_id=str(uuid.uuid4()),
    )
    assert await installation.get_member_installation_active_status(
        app_id,
        vault_id,
        user=_member_session(reader_id, "reader"),
        correlation_id=str(uuid.uuid4()),
    ) == {"active": False}
    restored = await _command(
        lifecycle_pool,
        app_id,
        vault_id,
        release_id,
        mode="restore",
    )
    assert restored["lifecycle"] == "active"
    assert await installation.get_member_installation_active_status(
        app_id,
        vault_id,
        user=_member_session(reader_id, "reader"),
        correlation_id=str(uuid.uuid4()),
    ) == {"active": True}

    without_installation_vault_id = await _vault(
        lifecycle_pool, "member-status-empty", owner_id=owner_id
    )
    async with lifecycle_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO vault_access (vault_id, user_id, role, granted_by) VALUES ($1, $2, 'reader', $3)",
            without_installation_vault_id,
            reader_id,
            _ADMIN_ID,
        )
    assert await installation.get_member_installation_active_status(
        app_id,
        without_installation_vault_id,
        user=_member_session(reader_id, "reader"),
        correlation_id=str(uuid.uuid4()),
    ) == {"active": False}

    missing = await installation.get_member_installation_active_status(
        uuid.uuid4(),
        vault_id,
        user=_member_session(reader_id, "reader"),
        correlation_id=str(uuid.uuid4()),
    )
    assert missing == {"active": False}

    async with lifecycle_pool.acquire() as conn:
        await conn.execute("UPDATE vaults SET public_access='reader' WHERE id=$1", vault_id)
    denials: list[str] = []
    for user, requested_app_id, requested_vault_id in (
        (_member_session(outsider_id, "outsider"), app_id, vault_id),
        (_member_session(foreign_admin_id, "foreign-admin"), app_id, vault_id),
        (_admin(), app_id, vault_id),
        (_member_session(outsider_id, "outsider"), uuid.uuid4(), uuid.uuid4()),
    ):
        with pytest.raises(ForbiddenError) as denied:
            await installation.get_member_installation_active_status(
                requested_app_id,
                requested_vault_id,
                user=user,
                correlation_id=str(uuid.uuid4()),
            )
        denials.append(denied.value.message)
    assert denials == ["Installation request denied"] * len(denials)

    async with lifecycle_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM vault_access WHERE vault_id=$1 AND user_id=$2",
            vault_id,
            reader_id,
        )
    with pytest.raises(ForbiddenError, match="Installation request denied"):
        await installation.get_member_installation_active_status(
            app_id,
            vault_id,
            user=_member_session(reader_id, "reader"),
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
            resources.canonical_table_fingerprint([]),
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


async def test_member_active_vault_collection_is_complete_current_and_read_only(lifecycle_pool, monkeypatch):
    app_id = await _app(lifecycle_pool, "member-list")
    other_app = await _app(lifecycle_pool, "other-list")
    release = await _release(lifecycle_pool, app_id, "1.0.0")
    other_release = await _release(lifecycle_pool, other_app, "1.0.0")
    owner = await _user(lifecycle_pool, "list-owner")
    member = await _user(lifecycle_pool, "list-member")
    outsider = await _user(lifecycle_pool, "list-outsider")
    vaults = [await _vault(lifecycle_pool, f"list-{i:02d}", owner_id=owner) for i in range(32)]
    roles = ("reader", "writer", "admin")
    async with lifecycle_pool.acquire() as conn:
        await conn.executemany(
            "INSERT INTO vault_access (vault_id,user_id,role,granted_by) VALUES ($1,$2,$3,$4)",
            [(vault_id, member, roles[i % 3], _ADMIN_ID) for i, vault_id in enumerate(vaults[:30])],
        )
        await conn.execute("UPDATE vaults SET public_access='reader' WHERE id=$1", vaults[30])
    for vault_id in vaults:
        await _command(lifecycle_pool, app_id, vault_id, release)
    await _command(lifecycle_pool, other_app, vaults[0], other_release)
    async with lifecycle_pool.acquire() as conn:
        await conn.execute(
            "UPDATE vault_app_installations SET lifecycle='active', current_release_id=desired_release_id WHERE app_id=$1 AND vault_id=ANY($2::uuid[])",
            app_id, vaults[:31],
        )
        before = await conn.fetchval("SELECT json_agg(row_to_json(i) ORDER BY id)::text FROM vault_app_installations i")

    user = _member_session(member, "member")
    query_count = 0

    class CountingConnection:
        def __init__(self, conn):
            self.conn = conn

        async def fetch(self, *args):
            nonlocal query_count
            query_count += 1
            return await self.conn.fetch(*args)

    class CountingAcquire:
        async def __aenter__(self):
            self.acquisition = lifecycle_pool.acquire()
            return CountingConnection(await self.acquisition.__aenter__())

        async def __aexit__(self, *args):
            return await self.acquisition.__aexit__(*args)

    class CountingPool:
        def acquire(self):
            return CountingAcquire()

    async def counting_pool():
        return CountingPool()

    monkeypatch.setattr(access_service, "get_pool", counting_pool)

    async def read(selected_app=app_id):
        return (await installation.list_member_active_installation_vaults(
            selected_app, user=user, correlation_id=str(uuid.uuid4())
        ))["vaults"]
    result = await read()
    large_query_count = query_count
    assert len(result) == 30
    assert {row["id"] for row in result} == {str(v) for v in vaults[:30]}
    assert [(r["name"], r["id"]) for r in result] == sorted((r["name"], r["id"]) for r in result)
    assert {r["id"]: r["role"] for r in result} == {str(v): roles[i % 3] for i, v in enumerate(vaults[:30])}
    assert await read(other_app) == []
    assert await read(uuid.uuid4()) == []
    query_count = 0
    await read(other_app)
    assert query_count == large_query_count
    assert (await installation.list_member_active_installation_vaults(
        app_id, user=_member_session(owner, "owner"), correlation_id="owner"
    ))["vaults"] and len((await installation.list_member_active_installation_vaults(
        app_id, user=_member_session(owner, "owner"), correlation_id="owner"
    ))["vaults"]) == 31
    assert (await installation.list_member_active_installation_vaults(
        app_id, user=_member_session(outsider, "outsider"), correlation_id="outsider"
    ))["vaults"] == []
    assert await read() == result
    async with lifecycle_pool.acquire() as conn:
        assert await conn.fetchval("SELECT json_agg(row_to_json(i) ORDER BY id)::text FROM vault_app_installations i") == before
        await conn.execute("DELETE FROM vault_access WHERE vault_id=$1 AND user_id=$2", vaults[0], member)
        await conn.execute("UPDATE vault_app_installations SET lifecycle='uninstalled', desired_release_id=NULL WHERE app_id=$1 AND vault_id=$2", app_id, vaults[1])
    result = await read()
    assert len(result) == 28
    assert str(vaults[0]) not in {r["id"] for r in result}
    assert str(vaults[1]) not in {r["id"] for r in result}
    async with lifecycle_pool.acquire() as conn:
        await conn.execute(
            "UPDATE vault_app_installations SET lifecycle='active', desired_release_id=$3 WHERE app_id=$1 AND vault_id=$2",
            app_id, vaults[1], release,
        )
    assert str(vaults[1]) in {r["id"] for r in await read()}
