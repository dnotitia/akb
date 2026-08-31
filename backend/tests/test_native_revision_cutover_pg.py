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
import app.services.native_revision_cutover as cutover_module
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

        with pytest.raises(ValueError, match="complete active vault inventory"):
            await cutover.plan(
                vaults=vaults[:1],
                coverage_version="fixture-omitted-vault-v1",
            )
        with pytest.raises(ValueError, match="complete active vault inventory"):
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

        identity = _identity("two-vaults")
        authority = await cutover.commit(planned.cutover_id, identity=identity)
        assert authority.cutover_id == planned.cutover_id
        assert authority.inventory_digest == verified.inventory_digest
        assert authority.status == "committed"
        assert await cutover.commit(planned.cutover_id, identity=identity) == authority

        async with pool.acquire() as conn:
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

        identity = _identity("with-mirror")
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

        with pytest.raises(
            CutoverVerificationError,
            match="active vault inventory drifted",
        ):
            await cutover.commit(planned.cutover_id, identity=identity)
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT state FROM native_revision_legacy_write_fence WHERE fence_key = TRUE"
            ) == "open"
            assert await conn.fetchval(
                "SELECT count(*) FROM native_revision_existing_authority"
            ) == 0


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
            assert await conn.fetchval(
                "SELECT state FROM native_revision_legacy_write_fence WHERE fence_key = TRUE"
            ) == "open"
            assert await conn.fetchval(
                "SELECT count(*) FROM native_revision_existing_authority"
            ) == 0
            assert await conn.execute(
                "UPDATE documents SET title = title WHERE vault_id = $1",
                vault.namespace_id,
            ) == "UPDATE 1"


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
            match="active vault inventory drifted",
        ):
            await cutover.commit(
                planned.cutover_id,
                identity=_identity("post-plan-vault"),
            )

        async with pool.acquire() as conn:
            fence = await conn.fetchrow(
                "SELECT state, epoch, cutover_id FROM native_revision_legacy_write_fence"
            )
            assert tuple(fence.values()) == ("open", 0, None)
            assert await conn.fetchval(
                "SELECT count(*) FROM native_revision_existing_authority"
            ) == 0
            assert await conn.execute(
                "UPDATE documents SET title = title WHERE vault_id = $1",
                original.namespace_id,
            ) == "UPDATE 1"


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
            assert await conn.fetchval(
                "SELECT state FROM native_revision_legacy_write_fence"
            ) == "open"
            assert await conn.fetchval(
                "SELECT count(*) FROM native_revision_existing_authority"
            ) == 0


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
            assert await conn.fetchval(
                "SELECT count(*) FROM native_resources WHERE resource_id = ANY($1::uuid[])",
                list(file_ids),
            ) == 0


async def test_authority_mint_is_the_boundary_and_fences_external_git_race(tmp_path):
    async with _fresh_schema() as pool:
        git = GitService(storage_path=str(tmp_path / "git-fence-race"))
        vault = await _manual_vault(pool, git, label="fence-race")
        data = b"race fixture\x00"
        _, s3_key = await _confirmed_file(
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
        commit_task = asyncio.create_task(
            cutover.commit(planned.cutover_id, identity=identity)
        )
        assert await asyncio.to_thread(reader.started.wait, 5)
        try:
            async with pool.acquire() as racer:
                raced_insert = asyncio.create_task(
                    racer.execute(
                        """
                        INSERT INTO vault_external_git (vault_id, remote_url, remote_branch)
                        VALUES ($1, 'https://git.example.invalid/race.git', 'main')
                        """,
                        vault.namespace_id,
                    )
                )
                await asyncio.sleep(0.1)
                assert not raced_insert.done()
                reader.release.set()
                authority = await commit_task
                with pytest.raises(
                    asyncpg.ObjectNotInPrerequisiteStateError,
                    match="Legacy revision writes are fenced",
                ):
                    await raced_insert
        finally:
            reader.release.set()

        assert authority.status == "committed"
        async with pool.acquire() as conn:
            fence = await conn.fetchrow(
                "SELECT state, epoch, cutover_id FROM native_revision_legacy_write_fence"
            )
            assert tuple(fence.values()) == ("committed", 1, planned.cutover_id)
            with pytest.raises(
                asyncpg.ObjectNotInPrerequisiteStateError,
                match="Legacy revision writes are fenced",
            ):
                await conn.execute(
                    "UPDATE documents SET title = title WHERE vault_id = $1",
                    vault.namespace_id,
                )
            assert await conn.execute(
                "UPDATE vault_files SET description = 'native-era write' WHERE vault_id = $1",
                vault.namespace_id,
            ) == "UPDATE 1"
            assert (
                await consume_or_validate_existing_database_authority(
                    conn,
                    identity=identity,
                )
                == "cutover_validated"
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
            applied = {
                row["filename"]
                for row in await conn.fetch("SELECT filename FROM schema_migrations")
            }
            await postgres._apply_pending_migrations(conn, applied)
            assert loaded == [
                "088_native_revision_existing_cutover.py",
                "089_native_file_projection_outbox.py",
            ]
            assert await conn.fetchval(
                "SELECT state FROM native_revision_legacy_write_fence"
            ) == "open"
            assert await conn.fetchval(
                "SELECT to_regclass('public.native_file_projection_outbox') IS NOT NULL"
            ) is True

            loaded.clear()
            applied = {
                row["filename"]
                for row in await conn.fetch("SELECT filename FROM schema_migrations")
            }
            await postgres._apply_pending_migrations(conn, applied)
            assert loaded == []
            assert await conn.fetchval(
                "SELECT count(*) FROM schema_migrations WHERE filename = ANY($1::text[])",
                list(cutover_files),
            ) == 2


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
