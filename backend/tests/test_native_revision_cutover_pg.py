"""Fixture-first checks for coordinating an existing database cutover."""

from __future__ import annotations

import importlib.util
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import asyncpg
import pytest
from git import Repo

from app.services.git_service import GitService
from app.services.native_revision_backfill import NativeRevisionBackfill
from app.services.native_revision_authority import (
    NativeAuthorityError,
    NativeAuthorityIdentity,
    consume_or_validate_existing_database_authority,
)
from app.services.native_revision_cutover import (
    CutoverVerificationError,
    CutoverVaultInput,
    NativeRevisionCutover,
    NativeRevisionCutoverVerifier,
)


pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[1]
_INIT_SQL = (_BACKEND / "app" / "db" / "init.sql").read_text(encoding="utf-8")
_MIGRATIONS = _BACKEND / "app" / "db" / "migrations"
_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:5433/akb",  # pragma: allowlist secret
)


async def _reachable() -> bool:
    try:
        conn = await asyncpg.connect(_DSN, timeout=2)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


def _database_dsn(name: str) -> str:
    return f"{_DSN.rsplit('/', 1)[0]}/{name}"


def _load(filename: str):
    path = _MIGRATIONS / filename
    spec = importlib.util.spec_from_file_location(f"migration_cutover_test_{filename}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@asynccontextmanager
async def _fresh_schema():
    if not await _reachable():
        pytest.skip(f"Postgres not reachable at {_DSN}")

    name = f"akb_native_cutover_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(_DSN)
    conn = None
    pool = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        dsn = _database_dsn(name)
        conn = await asyncpg.connect(dsn)
        await conn.execute(_INIT_SQL)
        for filename in (
            "010_external_git_mirror.py",
            "048_native_revision_core.py",
            "053_native_revision_m1_pg_body.py",
            "060_native_revision_migration_bridge.py",
            "061_native_revision_authority.py",
            "088_native_revision_existing_cutover.py",
            "089_native_file_projection_outbox.py",
        ):
            await _load(filename).migrate(conn=conn)
        await conn.close()
        conn = None
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
        yield pool
    finally:
        if pool is not None:
            await pool.close()
        if conn is not None and not conn.is_closed():
            await conn.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def _manual_vault(pool, git: GitService, *, label: str) -> CutoverVaultInput:
    vault_name = f"cutover-{label}-{uuid.uuid4().hex}"
    git.init_vault(vault_name)
    current_oid = git.commit_file(
        vault_name,
        "document.md",
        f"fixture body {label}\n",
        f"[create] document.md\n\nagent: fixture\naction: create\nsummary: fixture {label}",
    )
    committed_at = Repo(str(git._bare_path(vault_name))).commit(current_oid).committed_datetime

    async with pool.acquire() as conn:
        namespace_id = await conn.fetchval(
            """
            INSERT INTO vaults (name, git_path, status)
            VALUES ($1, $2, 'active')
            RETURNING id
            """,
            vault_name,
            str(git._bare_path(vault_name)),
        )
        await conn.execute(
            """
            INSERT INTO documents
                (id, vault_id, path, title, created_at, updated_at, current_commit, source)
            VALUES ($1, $2, 'document.md', $3, $4, $4, $5, 'manual')
            """,
            uuid.uuid4(),
            namespace_id,
            f"fixture-{label}",
            committed_at - timedelta(seconds=1),
            current_oid,
        )
    return CutoverVaultInput(namespace_id=namespace_id, fixed_ref=current_oid)


class _FixtureVerifier:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self.calls: list[uuid.UUID] = []

    async def compare_run(self, run_id: uuid.UUID) -> dict:
        self.calls.append(run_id)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT r.status,
                       count(*) FILTER (WHERE i.status = 'complete') AS complete_items,
                       count(*) FILTER (WHERE nr.resource_id IS NOT NULL) AS native_resources,
                       count(*) FILTER (WHERE rev.revision_id IS NOT NULL) AS native_heads,
                       count(*) FILTER (WHERE lm.resource_id IS NOT NULL) AS retained_mappings
                  FROM native_revision_migration_runs r
                  JOIN native_revision_migration_items i ON i.run_id = r.run_id
             LEFT JOIN native_resources nr
                    ON nr.namespace_id = i.namespace_id
                   AND nr.resource_id = i.native_resource_id
             LEFT JOIN native_revisions rev
                    ON rev.namespace_id = nr.namespace_id
                   AND rev.resource_id = nr.resource_id
                   AND rev.revision_id = nr.head_revision_id
             LEFT JOIN legacy_revision_mappings lm
                    ON lm.run_id = r.run_id
                   AND lm.resource_id = i.native_resource_id
                 WHERE r.run_id = $1
              GROUP BY r.status
                """,
                run_id,
            )
        assert row is not None
        passed = (
            row["status"] == "complete"
            and row["complete_items"] > 0
            and row["complete_items"]
            == row["native_resources"]
            == row["native_heads"]
            == row["retained_mappings"]
        )
        return {
            "status": "passed" if passed else "failed",
            "passed": passed,
            "run_id": str(run_id),
            "summary": {
                "resource_count": row["complete_items"],
                "unexplained_mismatch_count": 0 if passed else 1,
            },
        }


class _MismatchVerifier:
    async def compare_run(self, run_id: uuid.UUID) -> dict:
        return {
            "status": "failed",
            "passed": False,
            "run_id": str(run_id),
            "summary": {"unexplained_mismatch_count": 1},
        }


async def test_two_manual_vaults_plan_apply_and_verify_as_one_database_cutover(tmp_path):
    async with _fresh_schema() as pool:
        git = GitService(storage_path=str(tmp_path / "git"))
        vaults = [
            await _manual_vault(pool, git, label="one"),
            await _manual_vault(pool, git, label="two"),
        ]
        backfill = NativeRevisionBackfill(pool, git=git)
        verifier = _FixtureVerifier(pool)
        cutover = NativeRevisionCutover(pool, backfill=backfill, verifier=verifier)

        planned = await cutover.plan(vaults=vaults, coverage_version="fixture-v1")
        assert planned.status == "planned"
        assert len(planned.vaults) == 2
        assert {item.namespace_id for item in planned.vaults} == {
            item.namespace_id for item in vaults
        }
        assert len({item.migration_run_id for item in planned.vaults}) == 2

        applied = await cutover.apply(planned.cutover_id)
        assert applied.status == "applied"
        assert {item.status for item in applied.vaults} == {"applied"}

        async with pool.acquire() as conn:
            counts_before_replay = await conn.fetchrow(
                """
                SELECT (SELECT count(*) FROM native_resources) AS resources,
                       (SELECT count(*) FROM native_revisions) AS revisions,
                       (SELECT count(*) FROM native_revision_migration_runs) AS migration_runs
                """
            )
        replay = await cutover.apply(planned.cutover_id)
        async with pool.acquire() as conn:
            counts_after_replay = await conn.fetchrow(
                """
                SELECT (SELECT count(*) FROM native_resources) AS resources,
                       (SELECT count(*) FROM native_revisions) AS revisions,
                       (SELECT count(*) FROM native_revision_migration_runs) AS migration_runs
                """
            )
        assert replay == applied
        assert counts_after_replay == counts_before_replay

        verified = await cutover.verify(planned.cutover_id)
        assert verified.status == "verified"
        assert {item.status for item in verified.vaults} == {"verified"}
        assert all(item.verification_digest is not None for item in verified.vaults)
        assert verified.verification_digest is not None
        assert set(verifier.calls) == {item.migration_run_id for item in planned.vaults}

        identity = NativeAuthorityIdentity(
            tenant_id="fixture-tenant",
            namespace="fixture-namespace",
            database_id=uuid.uuid4(),
            current_database="akb",
            runtime_image_digest="sha256:" + "a" * 64,
        )
        authority = await cutover.commit(planned.cutover_id, identity=identity)
        assert authority.cutover_id == planned.cutover_id
        assert authority.inventory_digest == verified.inventory_digest
        assert authority.status == "pending"
        assert await cutover.commit(planned.cutover_id, identity=identity) == authority

        async with pool.acquire() as conn:
            assert (
                await consume_or_validate_existing_database_authority(
                    conn,
                    identity=identity,
                )
                == "cutover_committed"
            )
            upgraded = replace(identity, runtime_image_digest="sha256:" + "b" * 64)
            assert (
                await consume_or_validate_existing_database_authority(
                    conn,
                    identity=upgraded,
                )
                == "cutover_validated"
            )
            with pytest.raises(NativeAuthorityError) as mismatch:
                await consume_or_validate_existing_database_authority(
                    conn,
                    identity=replace(identity, namespace="another-namespace"),
                )
            assert mismatch.value.code == "native_authority_existing_mismatch"
            with pytest.raises(asyncpg.RaiseError, match="cannot be deleted"):
                await conn.execute("DELETE FROM native_revision_existing_authority")


async def test_persisted_external_mirror_is_reported_without_poisoning_manual_backfill(
    tmp_path,
):
    async with _fresh_schema() as pool:
        git = GitService(storage_path=str(tmp_path / "git-with-mirror"))
        manual = await _manual_vault(pool, git, label="manual-eligible")

        mirror_name = f"cutover-mirror-{uuid.uuid4().hex}"
        git.init_vault(mirror_name)
        mirror_oid = git.commit_file(
            mirror_name,
            "mirrored.md",
            "persisted external mirror body\n",
            "[create] mirrored.md\n\nagent: fixture\naction: create\nsummary: mirror",
        )
        async with pool.acquire() as conn:
            mirror_id = await conn.fetchval(
                """
                INSERT INTO vaults (name, git_path, status)
                VALUES ($1, $2, 'active') RETURNING id
                """,
                mirror_name,
                str(git._bare_path(mirror_name)),
            )
            await conn.execute(
                """
                INSERT INTO vault_external_git (vault_id, remote_url, remote_branch)
                VALUES ($1, 'https://git.example.invalid/fixture.git', 'main')
                """,
                mirror_id,
            )

        verifier = _FixtureVerifier(pool)
        cutover = NativeRevisionCutover(
            pool,
            backfill=NativeRevisionBackfill(pool, git=git),
            verifier=verifier,
        )
        planned = await cutover.plan(
            vaults=[
                manual,
                CutoverVaultInput(namespace_id=mirror_id, fixed_ref=mirror_oid),
            ],
            coverage_version="fixture-external-mirror-v1",
        )
        assert [item.namespace_id for item in planned.vaults] == [manual.namespace_id]
        assert [
            (item.namespace_id, item.fixed_git_oid, item.reason)
            for item in planned.exclusions
        ] == [(mirror_id, mirror_oid, "external_git_requires_collector")]
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                """
                SELECT count(*) FROM native_revision_migration_runs
                 WHERE namespace_id = $1
                """,
                mirror_id,
            ) == 0

        assert (await cutover.apply(planned.cutover_id)).status == "applied"
        verified = await cutover.verify(planned.cutover_id)
        assert verified.status == "verified"
        assert [item.namespace_id for item in verified.vaults] == [manual.namespace_id]
        assert verified.exclusions == planned.exclusions

        identity = NativeAuthorityIdentity(
            tenant_id="fixture-tenant-with-mirror",
            namespace="fixture-namespace-with-mirror",
            database_id=uuid.uuid4(),
            current_database="akb",
            runtime_image_digest="sha256:" + "c" * 64,
        )
        with pytest.raises(
            CutoverVerificationError,
            match="persisted external Git vaults remain",
        ):
            await cutover.commit(planned.cutover_id, identity=identity)
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM native_revision_existing_authority"
            ) == 0
            assert await conn.fetchval(
                "SELECT count(*) FROM native_resources WHERE namespace_id = $1",
                mirror_id,
            ) == 0
            await conn.execute("DELETE FROM vaults WHERE id = $1", mirror_id)
            assert await conn.fetchval(
                """
                SELECT count(*) FROM native_revision_cutover_exclusions
                 WHERE cutover_id = $1 AND namespace_id = $2
                """,
                planned.cutover_id,
                mirror_id,
            ) == 1

        authority = await cutover.commit(planned.cutover_id, identity=identity)
        assert authority.cutover_id == planned.cutover_id
        assert authority.status == "pending"


async def test_unexplained_fixture_mismatch_stops_before_verified_state(tmp_path):
    async with _fresh_schema() as pool:
        git = GitService(storage_path=str(tmp_path / "git-mismatch"))
        vault = await _manual_vault(pool, git, label="mismatch")
        cutover = NativeRevisionCutover(
            pool,
            backfill=NativeRevisionBackfill(pool, git=git),
            verifier=_MismatchVerifier(),
        )

        planned = await cutover.plan(vaults=[vault], coverage_version="fixture-mismatch-v1")
        applied = await cutover.apply(planned.cutover_id)
        assert applied.status == "applied"

        with pytest.raises(CutoverVerificationError):
            await cutover.verify(planned.cutover_id)

        async with pool.acquire() as conn:
            run_status = await conn.fetchval(
                "SELECT status FROM native_revision_cutover_runs WHERE cutover_id = $1",
                planned.cutover_id,
            )
            vault_status = await conn.fetchval(
                """
                SELECT status
                  FROM native_revision_cutover_vaults
                 WHERE cutover_id = $1 AND namespace_id = $2
                """,
                planned.cutover_id,
                vault.namespace_id,
            )
        assert run_status == "applied"
        assert vault_status == "applied"


async def test_product_shadow_verifier_checks_real_git_and_native_reads(tmp_path):
    async with _fresh_schema() as pool:
        git = GitService(storage_path=str(tmp_path / "git-product-verifier"))
        vaults = [
            await _manual_vault(pool, git, label="product-one"),
            await _manual_vault(pool, git, label="product-two"),
        ]
        cutover = NativeRevisionCutover(
            pool,
            backfill=NativeRevisionBackfill(pool, git=git),
            verifier=NativeRevisionCutoverVerifier(pool, git=git),
        )

        planned = await cutover.plan(
            vaults=vaults,
            coverage_version="fixture-product-shadow-v1",
        )
        await cutover.apply(planned.cutover_id)
        verified = await cutover.verify(planned.cutover_id)

        assert verified.status == "verified"
        assert {vault.status for vault in verified.vaults} == {"verified"}
