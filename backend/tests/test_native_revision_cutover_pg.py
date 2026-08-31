"""Fixture-first checks for coordinating an existing database cutover."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import os
import re
import threading
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import asyncpg
import pytest
from git import Repo

from app.db import postgres
from app.repositories.native_revision_migration_repo import MigrationIntegrityError
import app.services.native_revision_cutover as cutover_module
from app.services.git_service import FixedRefHistoryError, GitService
from app.services.native_revision_backfill import NativeRevisionBackfill
from app.services.native_revision_authority import (
    NativeAuthorityError,
    NativeAuthorityIdentity,
    consume_or_validate_existing_database_authority,
)
from app.services.native_revision_cutover import (
    CutoverApplyError,
    CutoverVerificationError,
    CutoverVaultInput,
    NativeRevisionCutover,
    NativeRevisionCutoverVerifier,
)
from app.services.native_revision_backend import NativeRevisionBackend


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
    except OSError, asyncpg.PostgresError:
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
async def _fresh_schema(*, cutover_migrations: bool = True):
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
        filenames = [
            "010_external_git_mirror.py",
            "048_native_revision_core.py",
            "053_native_revision_m1_pg_body.py",
            "060_native_revision_migration_bridge.py",
            "061_native_revision_authority.py",
        ]
        if cutover_migrations:
            filenames.extend(
                [
                    "088_native_revision_existing_cutover.py",
                    "089_native_file_projection_outbox.py",
                    "090_native_revision_vault_purge_fence.py",
                    "091_native_revision_committed_receipt_guard.py",
                    "092_native_revision_plan_supersession.py",
                ]
            )
        for filename in filenames:
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


async def _confirmed_file(
    pool: asyncpg.Pool,
    *,
    namespace_id: uuid.UUID,
    label: str,
    data: bytes,
    mime_type: str,
) -> tuple[uuid.UUID, str]:
    file_id = uuid.uuid4()
    s3_key = f"cutover-fixture/{file_id}/{label}"
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO vault_files (
                id, vault_id, kind, upload_state, name, s3_key, mime_type,
                size_bytes, content_hash, hash_algorithm, hash_verified_at
            ) VALUES ($1, $2, 'file', 'confirmed', $3, $4, $5, $6, $7, 'sha256', NOW())
            """,
            file_id,
            namespace_id,
            label,
            s3_key,
            mime_type,
            len(data),
            hashlib.sha256(data).hexdigest(),
        )
    return file_id, s3_key


def _identity(label: str) -> NativeAuthorityIdentity:
    return NativeAuthorityIdentity(
        tenant_id=f"fixture-tenant-{label}",
        namespace=f"fixture-namespace-{label}",
        database_id=uuid.uuid4(),
        current_database="akb",
        runtime_image_digest="sha256:" + hashlib.sha256(label.encode()).hexdigest(),
    )


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
            and row["complete_items"] == row["native_resources"] == row["native_heads"] == row["retained_mappings"]
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


async def test_active_and_archived_manual_vaults_cut_over_as_one_database(tmp_path):
    async with _fresh_schema() as pool:
        git = GitService(storage_path=str(tmp_path / "git"))
        vaults = [
            await _manual_vault(pool, git, label="one"),
            await _manual_vault(pool, git, label="two"),
        ]
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE vaults SET status = 'archived' WHERE id = $1",
                vaults[1].namespace_id,
            )
        backfill = NativeRevisionBackfill(pool, git=git)
        verifier = _FixtureVerifier(pool)
        cutover = NativeRevisionCutover(pool, backfill=backfill, verifier=verifier)

        with pytest.raises(ValueError, match="complete retained vault inventory"):
            await cutover.plan(
                vaults=vaults[:1],
                coverage_version="fixture-omitted-vault-v1",
            )
        with pytest.raises(ValueError, match="complete retained vault inventory"):
            await cutover.plan(
                vaults=[
                    *vaults,
                    CutoverVaultInput(namespace_id=uuid.uuid4(), fixed_ref="f" * 40),
                ],
                coverage_version="fixture-added-vault-v1",
            )

        planned = await cutover.plan(vaults=vaults, coverage_version="fixture-v1")
        assert planned.status == "planned"
        assert len(planned.vaults) == 2
        assert {item.namespace_id for item in planned.vaults} == {item.namespace_id for item in vaults}
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

        identity = _identity("two-vaults")
        authority = await cutover.commit(planned.cutover_id, identity=identity)
        assert authority.cutover_id == planned.cutover_id
        assert authority.inventory_digest == verified.inventory_digest
        assert authority.status == "committed"
        assert await cutover.commit(planned.cutover_id, identity=identity) == authority

        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT status FROM vaults WHERE id = $1", vaults[1].namespace_id) == "archived"
            await conn.execute(
                "UPDATE vaults SET status = 'active' WHERE id = $1",
                vaults[1].namespace_id,
            )
            assert await conn.fetchval("SELECT status FROM vaults WHERE id = $1", vaults[1].namespace_id) == "active"
            assert (
                await consume_or_validate_existing_database_authority(
                    conn,
                    identity=identity,
                )
                == "cutover_validated"
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


async def test_archived_external_mirror_blocks_authority_without_poisoning_manual_backfill(
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
                VALUES ($1, $2, 'archived') RETURNING id
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
        assert [(item.namespace_id, item.fixed_git_oid, item.reason) for item in planned.exclusions] == [
            (mirror_id, mirror_oid, "external_git_requires_collector")
        ]
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    """
                SELECT count(*) FROM native_revision_migration_runs
                 WHERE namespace_id = $1
                """,
                    mirror_id,
                )
                == 0
            )

        assert (await cutover.apply(planned.cutover_id)).status == "applied"
        verified = await cutover.verify(planned.cutover_id)
        assert verified.status == "verified"
        assert [item.namespace_id for item in verified.vaults] == [manual.namespace_id]
        assert verified.exclusions == planned.exclusions

        identity = _identity("with-mirror")
        with pytest.raises(
            CutoverVerificationError,
            match="persisted external Git vaults remain",
        ):
            await cutover.commit(planned.cutover_id, identity=identity)
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM native_revision_existing_authority") == 0
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM native_resources WHERE namespace_id = $1",
                    mirror_id,
                )
                == 0
            )
            await conn.execute("DELETE FROM vaults WHERE id = $1", mirror_id)
            assert (
                await conn.fetchval(
                    """
                SELECT count(*) FROM native_revision_cutover_exclusions
                 WHERE cutover_id = $1 AND namespace_id = $2
                """,
                    planned.cutover_id,
                    mirror_id,
                )
                == 1
            )

        with pytest.raises(
            CutoverVerificationError,
            match="retained vault inventory drifted",
        ):
            await cutover.commit(planned.cutover_id, identity=identity)
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval("SELECT state FROM native_revision_legacy_write_fence WHERE fence_key = TRUE")
                == "open"
            )
            assert await conn.fetchval("SELECT count(*) FROM native_revision_existing_authority") == 0


async def test_aborting_classification_plan_releases_pending_item_reservations(tmp_path):
    """A retired external mirror may be replanned only after explicit abort."""
    async with _fresh_schema() as pool:
        git = GitService(storage_path=str(tmp_path / "git-reclassification"))
        manual = await _manual_vault(pool, git, label="reclassification-manual")

        mirror_name = f"cutover-reclassification-mirror-{uuid.uuid4().hex}"
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

        backfill = NativeRevisionBackfill(pool, git=git)
        cutover = NativeRevisionCutover(
            pool,
            backfill=backfill,
            verifier=_FixtureVerifier(pool),
        )
        classification = await cutover.plan(
            vaults=[
                manual,
                CutoverVaultInput(namespace_id=mirror_id, fixed_ref=mirror_oid),
            ],
            coverage_version="fixture-reclassification-classification-v1",
        )
        assert [item.namespace_id for item in classification.vaults] == [manual.namespace_id]
        assert [item.namespace_id for item in classification.exclusions] == [mirror_id]
        classification_run_id = classification.vaults[0].migration_run_id
        with pytest.raises(
            asyncpg.UniqueViolationError,
            match="native_revision_migration_items_active_resource_head_key",
        ):
            await cutover.plan(
                vaults=[
                    manual,
                    CutoverVaultInput(namespace_id=mirror_id, fixed_ref=mirror_oid),
                ],
                coverage_version="fixture-reclassification-must-abort-v2",
            )

        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM vaults WHERE id = $1", mirror_id)

        aborted = await cutover.abort(classification.cutover_id)
        assert aborted.status == "aborted"
        assert aborted.aborted_from_status == "planned"
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT status FROM native_revision_migration_runs WHERE run_id = $1",
                    classification_run_id,
                )
                == "superseded"
            )
            assert (
                await conn.fetchval(
                    """
                    SELECT reservation_active
                      FROM native_revision_migration_items
                     WHERE run_id = $1
                    """,
                    classification_run_id,
                )
                is False
            )
            assert (
                await conn.fetchval(
                    """
                    SELECT count(*)
                      FROM native_revision_cutover_exclusions
                     WHERE cutover_id = $1 AND namespace_id = $2
                    """,
                    classification.cutover_id,
                    mirror_id,
                )
                == 1
            )

        fresh = await cutover.plan(
            vaults=[manual],
            coverage_version="fixture-reclassification-authority-v2",
        )
        assert fresh.cutover_id != classification.cutover_id
        assert fresh.vaults[0].migration_run_id != classification_run_id
        assert (await cutover.apply(fresh.cutover_id)).status == "applied"
        with pytest.raises(CutoverApplyError, match="aborted"):
            await cutover.apply(classification.cutover_id)
        with pytest.raises(MigrationIntegrityError, match="superseded"):
            await backfill.backfill_run(classification_run_id)


async def test_plan_supersession_migration_releases_legacy_aborted_reservations(tmp_path):
    """Upgrade releases an attempt that was aborted before plan supersession existed."""
    async with _fresh_schema(cutover_migrations=False) as pool:
        async with pool.acquire() as conn:
            for filename in (
                "088_native_revision_existing_cutover.py",
                "089_native_file_projection_outbox.py",
                "090_native_revision_vault_purge_fence.py",
                "091_native_revision_committed_receipt_guard.py",
            ):
                await _load(filename).migrate(conn=conn)

        git = GitService(storage_path=str(tmp_path / "git-legacy-aborted-plan"))
        manual = await _manual_vault(pool, git, label="legacy-aborted-plan")
        cutover = NativeRevisionCutover(
            pool,
            backfill=NativeRevisionBackfill(pool, git=git),
            verifier=_FixtureVerifier(pool),
        )
        prior = await cutover.plan(
            vaults=[manual],
            coverage_version="fixture-legacy-aborted-plan-v1",
        )
        prior_run_id = prior.vaults[0].migration_run_id
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE native_revision_cutover_runs
                   SET status = 'aborted',
                       aborted_from_status = 'planned',
                       aborted_at = NOW()
                 WHERE cutover_id = $1
                """,
                prior.cutover_id,
            )
            await _load("092_native_revision_plan_supersession.py").migrate(conn=conn)
            assert (
                await conn.fetchval(
                    "SELECT status FROM native_revision_migration_runs WHERE run_id = $1",
                    prior_run_id,
                )
                == "superseded"
            )
            assert (
                await conn.fetchval(
                    """
                    SELECT reservation_active
                      FROM native_revision_migration_items
                     WHERE run_id = $1
                    """,
                    prior_run_id,
                )
                is False
            )

        fresh = await cutover.plan(
            vaults=[manual],
            coverage_version="fixture-legacy-aborted-plan-v2",
        )
        assert fresh.vaults[0].migration_run_id != prior_run_id


async def test_deleted_vault_external_git_sidecar_blocks_authority_outside_retained_inventory(tmp_path):
    """A deleted vault is not migratable, but its sidecar still blocks authority."""
    async with _fresh_schema() as pool:
        git = GitService(storage_path=str(tmp_path / "git-deleted-sidecar"))
        manual = await _manual_vault(pool, git, label="deleted-sidecar-manual")

        async with pool.acquire() as conn:
            deleted_vault_id = await conn.fetchval(
                """
                INSERT INTO vaults (name, git_path, status)
                VALUES ($1, $2, 'deleted')
                RETURNING id
                """,
                f"cutover-deleted-sidecar-{uuid.uuid4().hex}",
                "/tmp/deleted-sidecar.git",
            )
            await conn.execute(
                """
                INSERT INTO vault_external_git (vault_id, remote_url, remote_branch)
                VALUES ($1, 'https://git.example.invalid/deleted-sidecar.git', 'main')
                """,
                deleted_vault_id,
            )

        cutover = NativeRevisionCutover(
            pool,
            backfill=NativeRevisionBackfill(pool, git=git),
            verifier=_FixtureVerifier(pool),
        )
        planned = await cutover.plan(
            vaults=[manual],
            coverage_version="fixture-deleted-external-sidecar-v1",
        )
        assert [item.namespace_id for item in planned.vaults] == [manual.namespace_id]
        assert planned.exclusions == ()
        await cutover.apply(planned.cutover_id)
        await cutover.verify(planned.cutover_id)

        with pytest.raises(
            CutoverVerificationError,
            match="persisted external Git vaults remain",
        ):
            await cutover.commit(
                planned.cutover_id,
                identity=_identity("deleted-external-sidecar"),
            )

        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM native_revision_existing_authority") == 0
            assert await conn.fetchval(
                "SELECT state FROM native_revision_legacy_write_fence WHERE fence_key = TRUE"
            ) == "open"


async def test_commit_rejects_an_omitted_eligible_file_and_leaves_writes_open(tmp_path):
    async with _fresh_schema() as pool:
        git = GitService(storage_path=str(tmp_path / "git-omitted-file"))
        vault = await _manual_vault(pool, git, label="omitted-file")
        data = b"preserved binary\x00fixture"
        file_id, s3_key = await _confirmed_file(
            pool,
            namespace_id=vault.namespace_id,
            label="omitted.bin",
            data=data,
            mime_type="application/octet-stream",
        )
        cutover = NativeRevisionCutover(
            pool,
            backfill=NativeRevisionBackfill(pool, git=git),
            verifier=_FixtureVerifier(pool),
            file_reader=lambda key: {s3_key: data}[key],
        )
        planned = await cutover.plan(
            vaults=[vault],
            coverage_version="fixture-omitted-file-v1",
        )
        await cutover.apply(planned.cutover_id)
        await cutover.verify(planned.cutover_id)

        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM native_revision_cutover_files WHERE cutover_id = $1 AND file_id = $2",
                planned.cutover_id,
                file_id,
            )

        with pytest.raises(CutoverVerificationError, match="File inventory drifted"):
            await cutover.commit(planned.cutover_id, identity=_identity("omitted-file"))

        async with pool.acquire() as conn:
            assert (
                await conn.fetchval("SELECT state FROM native_revision_legacy_write_fence WHERE fence_key = TRUE")
                == "open"
            )
            assert await conn.fetchval("SELECT count(*) FROM native_revision_existing_authority") == 0
            assert (
                await conn.execute(
                    "UPDATE documents SET title = title WHERE vault_id = $1",
                    vault.namespace_id,
                )
                == "UPDATE 1"
            )


async def test_commit_rejects_a_post_plan_vault_and_rolls_back_the_fence(tmp_path):
    async with _fresh_schema() as pool:
        git = GitService(storage_path=str(tmp_path / "git-post-plan-vault"))
        original = await _manual_vault(pool, git, label="original")
        cutover = NativeRevisionCutover(
            pool,
            backfill=NativeRevisionBackfill(pool, git=git),
            verifier=_FixtureVerifier(pool),
        )
        planned = await cutover.plan(
            vaults=[original],
            coverage_version="fixture-post-plan-vault-v1",
        )
        await cutover.apply(planned.cutover_id)
        await cutover.verify(planned.cutover_id)
        await _manual_vault(pool, git, label="created-after-plan")

        with pytest.raises(
            CutoverVerificationError,
            match="retained vault inventory drifted",
        ):
            await cutover.commit(
                planned.cutover_id,
                identity=_identity("post-plan-vault"),
            )

        async with pool.acquire() as conn:
            fence = await conn.fetchrow("SELECT state, epoch, cutover_id FROM native_revision_legacy_write_fence")
            assert tuple(fence.values()) == ("open", 0, None)
            assert await conn.fetchval("SELECT count(*) FROM native_revision_existing_authority") == 0
            assert (
                await conn.execute(
                    "UPDATE documents SET title = title WHERE vault_id = $1",
                    original.namespace_id,
                )
                == "UPDATE 1"
            )


async def test_commit_revalidates_file_bytes_and_current_git_refs(tmp_path):
    async with _fresh_schema() as pool:
        git = GitService(storage_path=str(tmp_path / "git-final-revalidation"))
        vault = await _manual_vault(pool, git, label="final-revalidation")
        original_data = b"final bytes\x00"
        file_id, s3_key = await _confirmed_file(
            pool,
            namespace_id=vault.namespace_id,
            label="final.bin",
            data=original_data,
            mime_type="application/octet-stream",
        )
        payloads = {s3_key: original_data}
        cutover = NativeRevisionCutover(
            pool,
            backfill=NativeRevisionBackfill(pool, git=git),
            verifier=_FixtureVerifier(pool),
            file_reader=lambda key: payloads[key],
        )
        planned = await cutover.plan(
            vaults=[vault],
            coverage_version="fixture-final-revalidation-v1",
        )
        await cutover.apply(planned.cutover_id)
        await cutover.verify(planned.cutover_id)
        identity = _identity("final-revalidation")

        payloads[s3_key] = b"other bytes\x00"
        with pytest.raises(
            CutoverVerificationError,
            match=f"File {file_id} content digest drifted",
        ):
            await cutover.commit(planned.cutover_id, identity=identity)

        payloads[s3_key] = original_data
        async with pool.acquire() as conn:
            vault_name = await conn.fetchval(
                "SELECT name FROM vaults WHERE id = $1",
                vault.namespace_id,
            )
        git.commit_file(
            vault_name,
            "after-plan.md",
            "post-plan mutation\n",
            "[create] after-plan.md\n\nagent: fixture\naction: create\nsummary: mutation",
        )
        with pytest.raises(CutoverVerificationError, match="Git ref drifted"):
            await cutover.commit(planned.cutover_id, identity=identity)

        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT state FROM native_revision_legacy_write_fence") == "open"
            assert await conn.fetchval("SELECT count(*) FROM native_revision_existing_authority") == 0


async def test_text_mime_with_invalid_nul_or_oversized_bytes_is_preserved_binary(
    tmp_path,
    monkeypatch,
):
    async with _fresh_schema() as pool:
        git = GitService(storage_path=str(tmp_path / "git-binary-classification"))
        vault = await _manual_vault(pool, git, label="binary-classification")
        monkeypatch.setattr(cutover_module, "M1_PG_TEXT_MAX_BYTES", 4)
        payloads: dict[str, bytes] = {}
        file_ids: set[uuid.UUID] = set()
        for label, data in (
            ("invalid.txt", b"\xff"),
            ("nul.txt", b"a\x00b"),
            ("oversized.txt", b"abcde"),
        ):
            file_id, s3_key = await _confirmed_file(
                pool,
                namespace_id=vault.namespace_id,
                label=label,
                data=data,
                mime_type="text/plain",
            )
            file_ids.add(file_id)
            payloads[s3_key] = data

        cutover = NativeRevisionCutover(
            pool,
            backfill=NativeRevisionBackfill(pool, git=git),
            verifier=_FixtureVerifier(pool),
            file_reader=lambda key: payloads[key],
        )
        planned = await cutover.plan(
            vaults=[vault],
            coverage_version="fixture-binary-classification-v1",
        )
        assert {item.file_id for item in planned.files} == file_ids
        assert {item.disposition for item in planned.files} == {"preserved_binary"}

        await cutover.apply(planned.cutover_id)
        verified = await cutover.verify(planned.cutover_id)
        assert {item.status for item in verified.files} == {"verified"}
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM native_resources WHERE resource_id = ANY($1::uuid[])",
                    list(file_ids),
                )
                == 0
            )


async def test_authority_mint_is_the_boundary_and_fences_external_git_and_receipt_races(tmp_path):
    async with _fresh_schema() as pool:
        git = GitService(storage_path=str(tmp_path / "git-fence-race"))
        vault = await _manual_vault(pool, git, label="fence-race")
        async with pool.acquire() as conn:
            document_id = await conn.fetchval(
                "SELECT id FROM documents WHERE vault_id = $1",
                vault.namespace_id,
            )
            await conn.execute(
                """
                INSERT INTO resource_aliases (vault_id, resource_type, old_ref, resource_id)
                VALUES ($1, 'document', 'legacy-document.md', $2)
                """,
                vault.namespace_id,
                document_id,
            )
        data = b"race fixture\x00"
        file_id, s3_key = await _confirmed_file(
            pool,
            namespace_id=vault.namespace_id,
            label="race.bin",
            data=data,
            mime_type="application/octet-stream",
        )

        class BlockingReader:
            def __init__(self) -> None:
                self.block = False
                self.started = threading.Event()
                self.release = threading.Event()

            def __call__(self, key: str) -> bytes:
                assert key == s3_key
                if self.block:
                    self.started.set()
                    if not self.release.wait(timeout=10):
                        raise TimeoutError("test did not release authority revalidation")
                return data

        reader = BlockingReader()
        cutover = NativeRevisionCutover(
            pool,
            backfill=NativeRevisionBackfill(pool, git=git),
            verifier=_FixtureVerifier(pool),
            file_reader=reader,
        )
        planned = await cutover.plan(
            vaults=[vault],
            coverage_version="fixture-fence-race-v1",
        )
        await cutover.apply(planned.cutover_id)
        await cutover.verify(planned.cutover_id)
        identity = _identity("fence-race")

        reader.block = True
        commit_task = asyncio.create_task(cutover.commit(planned.cutover_id, identity=identity))
        assert await asyncio.to_thread(reader.started.wait, 5)
        try:
            async with pool.acquire() as legacy_racer, pool.acquire() as receipt_racer:
                raced_insert = asyncio.create_task(
                    legacy_racer.execute(
                        """
                        INSERT INTO vault_external_git (vault_id, remote_url, remote_branch)
                        VALUES ($1, 'https://git.example.invalid/race.git', 'main')
                        """,
                        vault.namespace_id,
                    )
                )
                raced_receipt_delete = asyncio.create_task(
                    receipt_racer.execute(
                        "DELETE FROM native_revision_cutover_files WHERE cutover_id = $1 AND file_id = $2",
                        planned.cutover_id,
                        file_id,
                    )
                )
                await asyncio.sleep(0.1)
                assert not raced_insert.done()
                assert not raced_receipt_delete.done()
                reader.release.set()
                authority = await commit_task
                with pytest.raises(
                    asyncpg.ObjectNotInPrerequisiteStateError,
                    match="Legacy revision writes are fenced",
                ):
                    await raced_insert
                with pytest.raises(
                    asyncpg.ObjectNotInPrerequisiteStateError,
                    match="committed Native revision cutover receipt",
                ):
                    await raced_receipt_delete
        finally:
            reader.release.set()

        assert authority.status == "committed"
        async with pool.acquire() as conn:
            fence = await conn.fetchrow("SELECT state, epoch, cutover_id FROM native_revision_legacy_write_fence")
            assert tuple(fence.values()) == ("committed", 1, planned.cutover_id)
            with pytest.raises(
                asyncpg.ObjectNotInPrerequisiteStateError,
                match="Legacy revision writes are fenced",
            ):
                await conn.execute(
                    "UPDATE documents SET title = title WHERE vault_id = $1",
                    vault.namespace_id,
                )
            with pytest.raises(
                asyncpg.ObjectNotInPrerequisiteStateError,
                match="Legacy revision writes are fenced",
            ):
                await conn.execute(
                    "DELETE FROM documents WHERE vault_id = $1",
                    vault.namespace_id,
                )
            with pytest.raises(
                asyncpg.ObjectNotInPrerequisiteStateError,
                match="Legacy revision writes are fenced",
            ):
                await conn.execute(
                    "DELETE FROM resource_aliases WHERE vault_id = $1",
                    vault.namespace_id,
                )
            assert (
                await conn.execute(
                    "UPDATE vault_files SET description = 'native-era write' WHERE vault_id = $1",
                    vault.namespace_id,
                )
                == "UPDATE 1"
            )
            assert (
                await consume_or_validate_existing_database_authority(
                    conn,
                    identity=identity,
                )
                == "cutover_validated"
            )


async def test_authorized_vault_delete_purges_only_its_post_cutover_legacy_rows(
    monkeypatch,
    tmp_path,
):
    """The authorized whole-vault lifecycle delete is narrower than direct SQL."""
    async with _fresh_schema() as pool:
        from app.config import settings
        from app.db import postgres as postgres_module
        from app.services import access_service, index_service

        git_root = tmp_path / "git-authorized-vault-delete"
        git = GitService(storage_path=str(git_root))
        vault = await _manual_vault(pool, git, label="authorized-vault-delete")
        retained_file_data = b"authorized vault delete file\x00"
        retained_file_id, retained_file_key = await _confirmed_file(
            pool,
            namespace_id=vault.namespace_id,
            label="retained.bin",
            data=retained_file_data,
            mime_type="application/octet-stream",
        )
        owner_id = uuid.uuid4()
        async with pool.acquire() as conn:
            vault_name = await conn.fetchval("SELECT name FROM vaults WHERE id = $1", vault.namespace_id)
            document_id = await conn.fetchval(
                "SELECT id FROM documents WHERE vault_id = $1",
                vault.namespace_id,
            )
            await conn.execute(
                """
                INSERT INTO resource_aliases (vault_id, resource_type, old_ref, resource_id)
                VALUES ($1, 'document', 'legacy-document.md', $2)
                """,
                vault.namespace_id,
                document_id,
            )
            await conn.execute(
                """
                INSERT INTO users (id, username, email, password_hash, is_admin)
                VALUES ($1, $2, $3, 'fixture', TRUE)
                """,
                owner_id,
                f"cutover-delete-owner-{owner_id.hex}",
                f"cutover-delete-owner-{owner_id.hex}@example.invalid",
            )

        cutover = NativeRevisionCutover(
            pool,
            backfill=NativeRevisionBackfill(pool, git=git),
            verifier=_FixtureVerifier(pool),
            file_reader=lambda key: {
                retained_file_key: retained_file_data,
            }[key],
        )
        planned = await cutover.plan(
            vaults=[vault],
            coverage_version="fixture-authorized-vault-delete-v1",
        )
        await cutover.apply(planned.cutover_id)
        await cutover.verify(planned.cutover_id)
        identity = _identity("authorized-vault-delete")
        await cutover.commit(
            planned.cutover_id,
            identity=identity,
        )

        async with pool.acquire() as conn:
            with pytest.raises(
                asyncpg.ObjectNotInPrerequisiteStateError,
                match="Legacy revision writes are fenced",
            ):
                await conn.execute("DELETE FROM documents WHERE vault_id = $1", vault.namespace_id)
            with pytest.raises(
                asyncpg.ObjectNotInPrerequisiteStateError,
                match="Legacy revision writes are fenced",
            ):
                await conn.execute("DELETE FROM resource_aliases WHERE vault_id = $1", vault.namespace_id)
            with pytest.raises(
                asyncpg.ObjectNotInPrerequisiteStateError,
                match="committed Native revision cutover receipt",
            ):
                await conn.execute(
                    "DELETE FROM native_revision_cutover_files WHERE cutover_id = $1 AND file_id = $2",
                    planned.cutover_id,
                    retained_file_id,
                )
            with pytest.raises(
                asyncpg.ObjectNotInPrerequisiteStateError,
                match="committed Native revision cutover receipt",
            ):
                await conn.execute(
                    """
                    UPDATE native_revision_cutover_files
                       SET logical_path = logical_path
                     WHERE cutover_id = $1 AND file_id = $2
                    """,
                    planned.cutover_id,
                    retained_file_id,
                )
            with pytest.raises(
                asyncpg.ObjectNotInPrerequisiteStateError,
                match="committed Native revision cutover receipt",
            ):
                await conn.execute(
                    """
                    INSERT INTO native_revision_cutover_files (
                        cutover_id, namespace_id, file_id, logical_path, mime_type,
                        content_hash, byte_size, s3_key, etag, storage_version,
                        created_by, disposition, status, native_revision_id,
                        verification_digest, applied_at, verified_at
                    )
                    SELECT cutover_id, namespace_id, $3, logical_path, mime_type,
                           content_hash, byte_size, s3_key, etag, storage_version,
                           created_by, disposition, status, native_revision_id,
                           verification_digest, applied_at, verified_at
                      FROM native_revision_cutover_files
                     WHERE cutover_id = $1 AND file_id = $2
                    """,
                    planned.cutover_id,
                    retained_file_id,
                    uuid.uuid4(),
                )
            with pytest.raises(
                asyncpg.ObjectNotInPrerequisiteStateError,
                match="committed Native revision cutover receipt",
            ):
                await conn.execute(
                    "DELETE FROM native_revision_cutover_vaults WHERE cutover_id = $1 AND namespace_id = $2",
                    planned.cutover_id,
                    vault.namespace_id,
                )
            with pytest.raises(
                asyncpg.ObjectNotInPrerequisiteStateError,
                match="committed Native revision cutover receipt",
            ):
                await conn.execute(
                    """
                    UPDATE native_revision_cutover_vaults
                       SET fixed_git_oid = fixed_git_oid
                     WHERE cutover_id = $1 AND namespace_id = $2
                    """,
                    planned.cutover_id,
                    vault.namespace_id,
                )
            with pytest.raises(
                asyncpg.ObjectNotInPrerequisiteStateError,
                match="committed Native revision cutover receipt",
            ):
                await conn.execute(
                    """
                    INSERT INTO native_revision_cutover_vaults (
                        cutover_id, namespace_id, migration_run_id, fixed_git_oid,
                        inventory_digest, status, verification_digest, applied_at,
                        verified_at
                    )
                    SELECT cutover_id, $3, $4, fixed_git_oid, inventory_digest,
                           status, verification_digest, applied_at, verified_at
                      FROM native_revision_cutover_vaults
                     WHERE cutover_id = $1 AND namespace_id = $2
                    """,
                    planned.cutover_id,
                    vault.namespace_id,
                    uuid.uuid4(),
                    uuid.uuid4(),
                )
            with pytest.raises(
                asyncpg.ObjectNotInPrerequisiteStateError,
                match="committed Native revision cutover receipt",
            ):
                await conn.execute(
                    """
                    UPDATE native_revision_cutover_runs
                       SET coverage_version = coverage_version
                     WHERE cutover_id = $1
                    """,
                    planned.cutover_id,
                )
            with pytest.raises(
                asyncpg.ObjectNotInPrerequisiteStateError,
                match="committed Native revision cutover receipt",
            ):
                await conn.execute(
                    """
                    INSERT INTO native_revision_cutover_exclusions (
                        cutover_id, namespace_id, fixed_git_oid, reason
                    ) VALUES ($1, $2, $3, 'external_git_requires_collector')
                    """,
                    planned.cutover_id,
                    uuid.uuid4(),
                    vault.fixed_ref,
                )

        class _RoleSync:
            async def on_vault_delete(self, vault_id: uuid.UUID) -> None:
                assert vault_id == vault.namespace_id

        async def _no_policy(*_args, **_kwargs):
            return None

        async def _no_chunk_cleanup(*_args, **_kwargs) -> None:
            return None

        async def _no_source_chunk_cleanup(*_args, **_kwargs) -> None:
            return None

        monkeypatch.setattr(postgres_module, "_pool", pool)
        monkeypatch.setattr(access_service, "get_role_sync", _RoleSync)
        monkeypatch.setattr(access_service.write_policy_repo, "get_policy", _no_policy)
        monkeypatch.setattr(index_service, "delete_vault_chunks", _no_chunk_cleanup)
        monkeypatch.setattr(index_service, "_drop_source_chunks_with_outbox", _no_source_chunk_cleanup)
        monkeypatch.setattr(settings, "git_storage_path", str(git_root))
        monkeypatch.setattr(settings, "s3_endpoint_url", None)

        assert await access_service.delete_vault(str(owner_id), vault_name) == {
            "deleted": True,
            "vault": vault_name,
        }

        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT COUNT(*) FROM vaults WHERE id = $1", vault.namespace_id) == 0
            assert await conn.fetchval("SELECT COUNT(*) FROM documents WHERE vault_id = $1", vault.namespace_id) == 0
            assert await conn.fetchval("SELECT COUNT(*) FROM resource_aliases WHERE vault_id = $1", vault.namespace_id) == 0
            assert await conn.fetchval("SELECT COUNT(*) FROM vault_files WHERE id = $1", retained_file_id) == 0
            assert await conn.fetchval(
                """
                SELECT COUNT(*) FROM native_revision_cutover_vaults
                 WHERE cutover_id = $1 AND namespace_id = $2
                """,
                planned.cutover_id,
                vault.namespace_id,
            ) == 1
            assert await conn.fetchval(
                """
                SELECT COUNT(*) FROM native_revision_cutover_files
                 WHERE cutover_id = $1 AND file_id = $2
                """,
                planned.cutover_id,
                retained_file_id,
            ) == 1
            assert await consume_or_validate_existing_database_authority(
                conn,
                identity=identity,
            ) == "cutover_validated"


async def test_committed_cutover_preserves_vault_activity_for_deleted_and_recreated_documents(tmp_path):
    async with _fresh_schema() as pool:
        git = GitService(storage_path=str(tmp_path / "git-frozen-vault-activity"))
        vault_name = f"cutover-frozen-activity-{uuid.uuid4().hex}"
        git.init_vault(vault_name)

        deleted_create = git.commit_file(
            vault_name,
            "deleted.md",
            "deleted resource\n",
            "[create] deleted.md\n\nagent: fixture\naction: create\nsummary: create deleted resource",
        )
        await asyncio.sleep(1.05)
        deleted_remove = git.delete_file(
            vault_name,
            "deleted.md",
            "[delete] deleted.md\n\nagent: fixture\naction: delete\nsummary: delete deleted resource",
        )
        await asyncio.sleep(1.05)
        prior_create = git.commit_file(
            vault_name,
            "recreated.md",
            "prior resource\n",
            "[create] recreated.md\n\nagent: fixture\naction: create\nsummary: create prior resource",
        )
        await asyncio.sleep(1.05)
        prior_remove = git.delete_file(
            vault_name,
            "recreated.md",
            "[delete] recreated.md\n\nagent: fixture\naction: delete\nsummary: delete prior resource",
        )
        await asyncio.sleep(1.05)
        recreated_create = git.commit_file(
            vault_name,
            "recreated.md",
            "replacement resource\n",
            "[create] recreated.md\n\nagent: fixture\naction: create\nsummary: create replacement resource",
        )
        recreated_at = Repo(str(git._bare_path(vault_name))).commit(recreated_create).committed_datetime

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
                VALUES ($1, $2, 'recreated.md', 'replacement', $3, $3, $4, 'manual')
                """,
                uuid.uuid4(),
                namespace_id,
                recreated_at,
                recreated_create,
            )

        expected = await asyncio.to_thread(git.vault_log, vault_name, max_count=20)
        assert [entry["hash"] for entry in expected] == [
            recreated_create[:12],
            prior_remove[:12],
            prior_create[:12],
            deleted_remove[:12],
            deleted_create[:12],
        ]
        expected_by_path = {
            path: await asyncio.to_thread(git.vault_log, vault_name, max_count=20, path=path)
            for path in ("deleted.md", "recreated.md")
        }

        vault = CutoverVaultInput(namespace_id=namespace_id, fixed_ref=recreated_create)
        cutover = NativeRevisionCutover(
            pool,
            backfill=NativeRevisionBackfill(pool, git=git),
            verifier=_FixtureVerifier(pool),
        )
        planned = await cutover.plan(
            vaults=[vault],
            coverage_version="fixture-frozen-vault-activity-v1",
        )
        await cutover.apply(planned.cutover_id)
        await cutover.verify(planned.cutover_id)
        await cutover.commit(planned.cutover_id, identity=_identity("frozen-vault-activity"))

        post_cutover = git.commit_file(
            vault_name,
            "recreated.md",
            "legacy head moved after cutover\n",
            "[update] recreated.md\n\nagent: fixture\naction: update\nsummary: must not enter frozen activity",
        )
        moved_head = await asyncio.to_thread(git.vault_log, vault_name, max_count=20)
        assert moved_head[0]["hash"] == post_cutover[:12]

        backend = NativeRevisionBackend(pool=pool, legacy_git=git)
        activity = await backend.vault_activity(
            vault_name,
            max_count=20,
            since=None,
            path=None,
        )
        assert activity == expected
        for path in ("deleted.md", "recreated.md"):
            assert (
                await backend.vault_activity(
                    vault_name,
                    max_count=20,
                    since=None,
                    path=path,
                )
                == expected_by_path[path]
            )

        class _UnreadableFixedRefGit(GitService):
            def manual_fixed_ref_vault_log(self, *_args, **_kwargs):
                raise FixedRefHistoryError("fixture cannot read committed fixed graph")

        with pytest.raises(FixedRefHistoryError, match="cannot read committed fixed graph"):
            await NativeRevisionBackend(
                pool=pool,
                legacy_git=_UnreadableFixedRefGit(
                    storage_path=str(tmp_path / "git-unreadable-frozen-activity")
                ),
            ).vault_activity(
                vault_name,
                max_count=20,
                since=None,
                path=None,
            )


async def test_cutover_migrations_upgrade_once_and_second_runner_start_is_a_noop(
    monkeypatch,
):
    async with _fresh_schema(cutover_migrations=False) as pool:
        source = (_BACKEND / "app" / "db" / "postgres.py").read_text(encoding="utf-8")
        registered = set(re.findall(r'"([0-9]{3}_[^"]+\.py)"', source))
        cutover_files = {
            "088_native_revision_existing_cutover.py",
            "089_native_file_projection_outbox.py",
            "090_native_revision_vault_purge_fence.py",
            "091_native_revision_committed_receipt_guard.py",
            "092_native_revision_plan_supersession.py",
        }
        assert cutover_files <= registered

        loaded: list[str] = []
        original_load = postgres._load_migration

        def recording_load(filename: str):
            loaded.append(filename)
            return original_load(filename)

        monkeypatch.setattr(postgres, "_load_migration", recording_load)
        async with pool.acquire() as conn:
            await conn.executemany(
                "INSERT INTO schema_migrations (filename) VALUES ($1) ON CONFLICT DO NOTHING",
                [(name,) for name in sorted(registered - cutover_files)],
            )
            applied = {row["filename"] for row in await conn.fetch("SELECT filename FROM schema_migrations")}
            await postgres._apply_pending_migrations(conn, applied)
            assert loaded == [
                "088_native_revision_existing_cutover.py",
                "089_native_file_projection_outbox.py",
                "090_native_revision_vault_purge_fence.py",
                "091_native_revision_committed_receipt_guard.py",
                "092_native_revision_plan_supersession.py",
            ]
            assert await conn.fetchval("SELECT state FROM native_revision_legacy_write_fence") == "open"
            assert await conn.fetchval("SELECT to_regclass('public.native_file_projection_outbox') IS NOT NULL") is True

            loaded.clear()
            applied = {row["filename"] for row in await conn.fetch("SELECT filename FROM schema_migrations")}
            await postgres._apply_pending_migrations(conn, applied)
            assert loaded == []
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM schema_migrations WHERE filename = ANY($1::text[])",
                    list(cutover_files),
                )
                == 5
            )


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


async def test_abort_preserves_additive_evidence_and_keeps_legacy_writes_open(tmp_path):
    async with _fresh_schema() as pool:
        git = GitService(storage_path=str(tmp_path / "git-abort"))
        vault = await _manual_vault(pool, git, label="abort")
        cutover = NativeRevisionCutover(
            pool,
            backfill=NativeRevisionBackfill(pool, git=git),
            verifier=_FixtureVerifier(pool),
        )

        planned = await cutover.plan(
            vaults=[vault],
            coverage_version="fixture-abort-v1",
        )
        applied = await cutover.apply(planned.cutover_id)
        assert applied.status == "applied"
        async with pool.acquire() as conn:
            native_before = await conn.fetchval("SELECT count(*) FROM native_resources")

        aborted = await cutover.abort(planned.cutover_id)
        assert aborted.status == "aborted"
        assert aborted.aborted_from_status == "applied"
        assert aborted.aborted_at is not None
        assert await cutover.abort(planned.cutover_id) == aborted

        with pytest.raises(CutoverVerificationError, match="aborted"):
            await cutover.commit(
                planned.cutover_id,
                identity=NativeAuthorityIdentity(
                    tenant_id="fixture-tenant",
                    namespace="fixture-namespace",
                    database_id=uuid.uuid4(),
                    current_database="akb",
                    runtime_image_digest="sha256:" + "a" * 64,
                ),
            )
        with pytest.raises(CutoverApplyError, match="aborted"):
            await cutover.apply(planned.cutover_id)

        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM native_resources") == native_before
            assert not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM native_revision_existing_authority)")
            await conn.execute(
                "UPDATE documents SET title = 'Legacy remains writable' WHERE vault_id = $1",
                vault.namespace_id,
            )


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
