"""Real-PostgreSQL proof for the stable Native authority state machine."""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from urllib.parse import unquote, urlsplit

import asyncpg
import pytest

from app.config import Settings
from app.db import postgres
from app.services.native_revision_authority import (
    ExistingDatabaseAuthorityFence,
    NativeAuthorityError,
    NativeAuthorityIdentity,
    begin_existing_database_authority,
    bootstrap_postgres_native,
    finalize_existing_database_authority,
    consume_or_validate_native_authority,
    mint_new_database_claim,
    pre_migration_revision_authority_guard,
    startup_revision_authority_preflight,
)

pytestmark = pytest.mark.asyncio

_ADMIN_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",  # pragma: allowlist secret
)
_IMAGE = "sha256:" + "a" * 64


async def _can_connect() -> bool:
    try:
        conn = await asyncpg.connect(_ADMIN_DSN, timeout=2)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


def _database_dsn(name: str) -> str:
    base, _ = _ADMIN_DSN.rsplit("/", 1)
    return f"{base}/{name}"


def _settings(name: str, git_root, *, backend: str = "postgres_native") -> Settings:
    parsed = urlsplit(_ADMIN_DSN)
    values = {
        "db_host": parsed.hostname or "localhost",
        "db_port": parsed.port or 5432,
        "db_name": name,
        "db_user": unquote(parsed.username or "akb"),
        "db_password": unquote(parsed.password or ""),
        "git_storage_path": str(git_root),
        "document_revision_backend": backend,
    }
    if backend == "postgres_native":
        values.update(
            document_revision_tenant_id="tenant-authority-proof",
            document_revision_namespace="tenant-authority-proof",
            document_revision_database_id=uuid.uuid4(),
            document_revision_runtime_image_digest=_IMAGE,
        )
    return Settings(**values)


@asynccontextmanager
async def _fresh_database(tmp_path, monkeypatch, *, backend: str = "postgres_native"):
    if not await _can_connect():
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"Required PostgreSQL is not reachable at {_ADMIN_DSN}")
        pytest.skip(f"PostgreSQL is not reachable at {_ADMIN_DSN}")
    admin = await asyncpg.connect(_ADMIN_DSN)
    name = f"akb_native_authority_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    configured = _settings(name, tmp_path / "git", backend=backend)
    monkeypatch.setattr(postgres, "settings", configured)
    await postgres.close_pool()
    try:
        yield configured, _database_dsn(name)
    finally:
        await postgres.close_pool()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


async def _verified_cutover(conn: asyncpg.Connection, tmp_path) -> tuple[uuid.UUID, uuid.UUID]:
    """Create the smallest verified cutover receipt accepted by authority mint."""
    namespace_id = uuid.uuid4()
    migration_run_id = uuid.uuid4()
    cutover_id = uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO vaults (id, name, git_path, status)
        VALUES ($1, $2, $3, 'active')
        """,
        namespace_id,
        f"authority-{namespace_id}",
        str(tmp_path / f"{namespace_id}.git"),
    )
    await conn.execute(
        """
        INSERT INTO native_revision_migration_runs (
            run_id, namespace_id, fixed_git_oid, coverage_version,
            inventory_digest, status, completed_at
        ) VALUES ($1, $2, $3, $4, $5, 'complete', NOW())
        """,
        migration_run_id,
        namespace_id,
        "a" * 40,
        f"authority-{cutover_id}",
        "b" * 64,
    )
    await conn.execute(
        """
        INSERT INTO native_revision_cutover_runs (
            cutover_id, coverage_version, inventory_digest, status,
            verification_digest, applied_at, verified_at
        ) VALUES ($1, $2, $3, 'verified', $4, NOW(), NOW())
        """,
        cutover_id,
        f"authority-{cutover_id}",
        "c" * 64,
        "d" * 64,
    )
    await conn.execute(
        """
        INSERT INTO native_revision_cutover_vaults (
            cutover_id, namespace_id, migration_run_id, fixed_git_oid,
            inventory_digest, status, verification_digest, applied_at, verified_at
        ) VALUES ($1, $2, $3, $4, $5, 'verified', $6, NOW(), NOW())
        """,
        cutover_id,
        namespace_id,
        migration_run_id,
        "a" * 40,
        "e" * 64,
        "f" * 64,
    )
    return cutover_id, namespace_id


async def test_existing_authority_fence_is_resumable_and_finalizable(tmp_path, monkeypatch):
    async with _fresh_database(tmp_path, monkeypatch) as (configured, dsn):
        await postgres.init_db()
        conn = await asyncpg.connect(dsn)
        try:
            cutover_id, namespace_id = await _verified_cutover(conn, tmp_path)
            identity = NativeAuthorityIdentity.from_settings(configured)
            short_callback_states: list[tuple[bool, str]] = []

            async def short_callback(callback_conn: asyncpg.Connection) -> None:
                short_callback_states.append(
                    (
                        callback_conn.is_in_transaction(),
                        await callback_conn.fetchval(
                            "SELECT state FROM native_revision_legacy_write_fence"
                        ),
                    )
                )

            fence = await begin_existing_database_authority(
                conn,
                identity=identity,
                cutover_id=cutover_id,
                preflight=short_callback,
            )
            assert isinstance(fence, ExistingDatabaseAuthorityFence)
            assert short_callback_states == [(True, "open")]
            assert not conn.is_in_transaction()
            assert fence.cutover_id == cutover_id
            assert fence.legacy_write_epoch == 1
            assert await conn.fetchval(
                "SELECT state FROM native_revision_legacy_write_fence"
            ) == "fenced"

            with pytest.raises(
                asyncpg.ObjectNotInPrerequisiteStateError,
                match="Native cutover writes are fenced",
            ):
                await conn.execute(
                    "UPDATE vaults SET description = description WHERE id = $1",
                    namespace_id,
                )
            with pytest.raises(
                asyncpg.ObjectNotInPrerequisiteStateError,
                match="Native cutover writes are fenced",
            ):
                await conn.execute(
                    "UPDATE native_revision_cutover_runs "
                    "SET coverage_version = coverage_version WHERE cutover_id = $1",
                    cutover_id,
                )
            with pytest.raises(
                asyncpg.ObjectNotInPrerequisiteStateError,
                match="Native cutover writes are fenced",
            ):
                await conn.execute(
                    "UPDATE native_revision_migration_runs "
                    "SET coverage_version = coverage_version WHERE run_id = $1",
                    await conn.fetchval(
                        "SELECT migration_run_id FROM native_revision_cutover_vaults "
                        "WHERE cutover_id = $1",
                        cutover_id,
                    ),
                )

            resumed = await begin_existing_database_authority(
                conn,
                identity=identity,
                cutover_id=cutover_id,
            )
            assert resumed == fence
            assert not conn.is_in_transaction()

            authority_id = await finalize_existing_database_authority(
                conn,
                identity=identity,
                fence=resumed,
            )
            assert isinstance(authority_id, uuid.UUID)
            assert not conn.is_in_transaction()
            assert await conn.fetchval(
                "SELECT state FROM native_revision_legacy_write_fence"
            ) == "committed"
            assert await conn.fetchval(
                "SELECT authority_id FROM native_revision_existing_authority "
                "WHERE marker_id = TRUE"
            ) == authority_id
            assert await conn.execute(
                "UPDATE vaults SET description = 'native-era' WHERE id = $1",
                namespace_id,
            ) == "UPDATE 1"
        finally:
            await conn.close()


async def test_existing_authority_phase_a_failure_rolls_back_to_open_and_fence_is_exact(
    tmp_path,
    monkeypatch,
):
    async with _fresh_database(tmp_path, monkeypatch) as (configured, dsn):
        await postgres.init_db()
        conn = await asyncpg.connect(dsn)
        try:
            cutover_id, _ = await _verified_cutover(conn, tmp_path)
            identity = NativeAuthorityIdentity.from_settings(configured)

            async def reject(_callback_conn: asyncpg.Connection) -> None:
                raise RuntimeError("short preflight drift")

            with pytest.raises(RuntimeError, match="short preflight drift"):
                await begin_existing_database_authority(
                    conn,
                    identity=identity,
                    cutover_id=cutover_id,
                    preflight=reject,
                )
            assert not conn.is_in_transaction()
            assert await conn.fetchval(
                "SELECT state FROM native_revision_legacy_write_fence"
            ) == "open"
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM native_revision_existing_authority_fence"
            ) == 0

            fence = await begin_existing_database_authority(
                conn,
                identity=identity,
                cutover_id=cutover_id,
            )
            with pytest.raises(NativeAuthorityError) as identity_error:
                await begin_existing_database_authority(
                    conn,
                    identity=replace(identity, tenant_id="other-tenant"),
                    cutover_id=cutover_id,
                )
            assert identity_error.value.code == "native_authority_fence_identity_mismatch"

            with pytest.raises(NativeAuthorityError) as cutover_error:
                await begin_existing_database_authority(
                    conn,
                    identity=identity,
                    cutover_id=uuid.uuid4(),
                )
            assert cutover_error.value.code == "native_authority_fence_conflict"
            assert await conn.fetchval(
                "SELECT state FROM native_revision_legacy_write_fence"
            ) == "fenced"

            with pytest.raises(NativeAuthorityError) as startup_error:
                await startup_revision_authority_preflight(configured)
            assert startup_error.value.code == "native_authority_fence_incomplete"

            with pytest.raises(NativeAuthorityError):
                await finalize_existing_database_authority(
                    conn,
                    identity=identity,
                    fence=replace(fence, legacy_write_epoch=2),
                )
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM native_revision_existing_authority"
            ) == 0
            assert await conn.fetchval(
                "SELECT state FROM native_revision_legacy_write_fence"
            ) == "fenced"
        finally:
            await conn.close()


async def test_bootstrap_consume_restart_and_immutable_marker(tmp_path, monkeypatch):
    async with _fresh_database(tmp_path, monkeypatch) as (configured, dsn):
        report = await bootstrap_postgres_native(configured)
        assert report["status"] == "pending"

        conn = await asyncpg.connect(dsn)
        try:
            identity = NativeAuthorityIdentity.from_settings(configured)
            assert await consume_or_validate_native_authority(conn, identity=identity) == "initialized"

            rerun = await bootstrap_postgres_native(configured)
            assert rerun["status"] == "initialized"
            assert rerun["claim_id"] == report["claim_id"]
            assert rerun["authority_id"] == report["authority_id"]

            counts = await conn.fetchrow(
                """
                SELECT
                    (SELECT COUNT(*) FROM document_revision_bootstrap_claims) AS claims,
                    (SELECT COUNT(*) FROM document_revision_authority_pending WHERE status = 'consumed') AS consumed,
                    (SELECT COUNT(*) FROM document_revision_authority_marker) AS markers
                """
            )
            assert tuple(counts.values()) == (1, 1, 1)

            upgraded = replace(identity, runtime_image_digest="sha256:" + "b" * 64)
            assert await consume_or_validate_native_authority(conn, identity=upgraded) == "validated"

            with pytest.raises(asyncpg.RaiseError, match="cannot be deleted"):
                await conn.execute("DELETE FROM document_revision_authority_marker")
        finally:
            await conn.close()


async def test_failpoint_rolls_back_to_reusable_pending_authority(tmp_path, monkeypatch):
    async with _fresh_database(tmp_path, monkeypatch) as (configured, dsn):
        await bootstrap_postgres_native(configured)
        conn = await asyncpg.connect(dsn)
        identity = NativeAuthorityIdentity.from_settings(configured)

        def failpoint(name: str) -> None:
            if name == "after_marker":
                raise RuntimeError("injected startup crash")

        try:
            with pytest.raises(RuntimeError, match="injected startup crash"):
                await consume_or_validate_native_authority(
                    conn,
                    identity=identity,
                    failpoint=failpoint,
                )
            assert await conn.fetchval("SELECT COUNT(*) FROM document_revision_authority_marker") == 0
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM document_revision_authority_pending WHERE status = 'pending'"
                )
                == 1
            )
            assert await consume_or_validate_native_authority(conn, identity=identity) == "initialized"
        finally:
            await conn.close()


async def test_rejects_old_empty_schema_mismatch_and_bare_git_reuse(tmp_path, monkeypatch):
    async with _fresh_database(tmp_path, monkeypatch, backend="bare_git") as (legacy, dsn):
        await postgres.init_db()
        native = _settings(legacy.db_name, legacy.git_storage_path)
        conn = await asyncpg.connect(dsn)
        try:
            with pytest.raises(NativeAuthorityError) as rejected:
                await mint_new_database_claim(
                    conn,
                    identity=NativeAuthorityIdentity.from_settings(native),
                )
            assert rejected.value.code == "native_authority_database_not_new"
        finally:
            await conn.close()

    async with _fresh_database(tmp_path, monkeypatch) as (configured, dsn):
        await bootstrap_postgres_native(configured)
        conn = await asyncpg.connect(dsn)
        try:
            identity = NativeAuthorityIdentity.from_settings(configured)
            mismatched = replace(identity, tenant_id="another-tenant")
            with pytest.raises(NativeAuthorityError) as rejected:
                await consume_or_validate_native_authority(conn, identity=mismatched)
            assert rejected.value.code == "native_authority_pending_mismatch"
        finally:
            await conn.close()

        bare = configured.model_copy(
            update={
                "document_revision_backend": "bare_git",
                "document_revision_tenant_id": "",
                "document_revision_namespace": "",
                "document_revision_database_id": None,
                "document_revision_runtime_image_digest": "",
            }
        )
        with pytest.raises(NativeAuthorityError) as conflict:
            await startup_revision_authority_preflight(bare)
        assert conflict.value.code == "native_authority_mode_conflict"


async def test_missing_claim_fails_before_ordinary_startup_creates_schema(tmp_path, monkeypatch):
    async with _fresh_database(tmp_path, monkeypatch) as (configured, dsn):
        with pytest.raises(NativeAuthorityError) as missing:
            await pre_migration_revision_authority_guard(configured)
        assert missing.value.code == "native_authority_missing"

        conn = await asyncpg.connect(dsn)
        try:
            assert await conn.fetchval("SELECT to_regclass('public.schema_migrations')") is None
            assert (
                await conn.fetchval(
                    "SELECT to_regclass('public.document_revision_bootstrap_claims')"
                )
                is None
            )
        finally:
            await conn.close()
