"""Focused manual-Git/disposable-PG checks for C9 A3+A4."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import pytest
from git import Repo

from app.repositories.native_revision_migration_repo import (
    MigrationInventoryDriftError,
    NativeRevisionMigrationRepository,
)
from app.repositories.native_revision_repo import NativeRevisionIdCollisionError
from app.services.git_service import FixedRefHistoryError, GitService
from app.services.legacy_revision_bridge import (
    InventoryEligibilityError,
    LegacyRevisionBridge,
    ManualVaultRequiredError,
    SelectorAmbiguousError,
    SelectorInvalidError,
    SelectorUnknownError,
)
from app.services.m1_pg_body_store import M1PgBodyStore
from app.services.native_document_service import NativeDocumentService
from app.services.native_revision_backfill import (
    BackfillFailpointError,
    FAILPOINT_BOUNDARIES,
    NativeRevisionBackfill,
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
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


def _database_dsn(name: str) -> str:
    return f"{_DSN.rsplit('/', 1)[0]}/{name}"


def _load(filename: str):
    path = _MIGRATIONS / filename
    spec = importlib.util.spec_from_file_location(f"migration_c9_test_{filename}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@asynccontextmanager
async def _fresh_schema(tmp_path: Path):
    if not await _reachable():
        pytest.skip(f"Postgres not reachable at {_DSN}")

    name = f"akb_c9_bridge_{uuid.uuid4().hex[:12]}"
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


async def _make_fixture(pool, tmp_path: Path) -> dict:
    git = GitService(storage_path=str(tmp_path / "git"))
    vault_name = f"c9-{uuid.uuid4().hex}"
    git.init_vault(vault_name)

    old_oid = git.commit_file(vault_name, "same.md", "old resource\n", "old")
    await asyncio.sleep(1.05)
    initial_oid = git.commit_file(
        vault_name,
        "same.md",
        "new v1\n",
        "[create] same.md\n\nagent: legacy-writer\naction: create\nsummary: create same document",
    )
    initial_dt = Repo(str(git._bare_path(vault_name))).commit(initial_oid).committed_datetime
    created_at = initial_dt - timedelta(milliseconds=100)
    await asyncio.sleep(1.05)
    move_oid = git.move_file(
        vault_name,
        "same.md",
        "renamed.md",
        "[move] same.md -> renamed.md\n\nagent: legacy-writer\naction: move\nsummary: move same document",
    )
    await asyncio.sleep(1.05)
    current_oid = git.commit_file(
        vault_name,
        "renamed.md",
        "new v2\n",
        "[update] renamed.md\n\nagent: legacy-writer\naction: update\nsummary: update renamed document",
    )
    current_dt = Repo(str(git._bare_path(vault_name))).commit(current_oid).committed_datetime
    await asyncio.sleep(1.05)
    other_oid = git.commit_file(
        vault_name,
        "other.md",
        "other\n",
        "[create] other.md\n\nagent: legacy-writer\naction: create\nsummary: create other document",
    )
    other_dt = Repo(str(git._bare_path(vault_name))).commit(other_oid).committed_datetime
    await asyncio.sleep(1.05)
    unrelated_tip = git.commit_file(vault_name, "unrelated.md", "tip\n", "unrelated")
    await asyncio.sleep(1.05)
    later_file_tip = git.commit_file(vault_name, "renamed.md", "post-head\n", "later file")

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
        document_one = uuid.uuid4()
        document_two = uuid.uuid4()
        await conn.execute(
            """
            INSERT INTO documents
                (id, vault_id, path, title, created_at, updated_at, current_commit, source)
            VALUES ($1, $2, 'renamed.md', 'renamed', $3, $3, $4, 'manual')
            """,
            document_one,
            namespace_id,
            created_at,
            current_oid,
        )
        await conn.execute(
            """
            INSERT INTO documents
                (id, vault_id, path, title, created_at, updated_at, current_commit, source)
            VALUES ($1, $2, 'other.md', 'other', $3, $3, $4, 'manual')
            """,
            document_two,
            namespace_id,
            other_dt - timedelta(milliseconds=100),
            other_oid,
        )
        await conn.execute(
            """
            INSERT INTO resource_aliases (vault_id, resource_type, old_ref, resource_id)
            VALUES ($1, 'document', 'same.md', $2)
            """,
            namespace_id,
            document_one,
        )

    return {
        "pool": pool,
        "git": git,
        "vault_name": vault_name,
        "namespace_id": namespace_id,
        "document_one": document_one,
        "document_two": document_two,
        "old_oid": old_oid,
        "initial_oid": initial_oid,
        "move_oid": move_oid,
        "current_oid": current_oid,
        "current_dt": current_dt,
        "other_oid": other_oid,
        "unrelated_tip": unrelated_tip,
        "later_file_tip": later_file_tip,
        "created_at": created_at,
    }


async def _make_compact_failpoint_fixtures(pool, tmp_path: Path) -> tuple[
    GitService, list[dict]
]:
    """Build one short manual-vault case per registered failpoint.

    The loop intentionally avoids the multi-second chronology fixture above:
    each case has one current file commit and one unrelated fixed-ref tip, so
    the table exercises every atomic boundary without repeating six sleeps.
    """
    git = GitService(storage_path=str(tmp_path / "git-failpoints"))
    fixtures: list[dict] = []
    async with pool.acquire() as conn:
        for index, boundary in enumerate(FAILPOINT_BOUNDARIES):
            vault_name = f"c9-fp-{index}-{uuid.uuid4().hex}"
            git.init_vault(vault_name)
            current_oid = git.commit_file(
                vault_name,
                "current.md",
                f"body-{index}\n",
                f"[create] current-{index}.md\n\nagent: legacy-writer\naction: create\nsummary: create current-{index}",
            )
            fixed_ref = git.commit_file(
                vault_name,
                f"tip-{index}.md",
                "unrelated\n",
                "unrelated fixed-ref tip",
            )
            current_dt = Repo(str(git._bare_path(vault_name))).commit(
                current_oid
            ).committed_datetime
            namespace_id = await conn.fetchval(
                """
                INSERT INTO vaults (name, git_path, status)
                VALUES ($1, $2, 'active')
                RETURNING id
                """,
                vault_name,
                str(git._bare_path(vault_name)),
            )
            document_id = uuid.uuid4()
            await conn.execute(
                """
                INSERT INTO documents
                    (id, vault_id, path, title, created_at, updated_at,
                     current_commit, source)
                VALUES ($1, $2, 'current.md', $3, $4, $4, $5, 'manual')
                """,
                document_id,
                namespace_id,
                f"current-{index}",
                current_dt - timedelta(seconds=1),
                current_oid,
            )
            await conn.execute(
                """
                INSERT INTO resource_aliases (vault_id, resource_type, old_ref, resource_id)
                VALUES ($1, 'document', $2, $3)
                """,
                namespace_id,
                f"legacy-{index}.md",
                document_id,
            )
            fixtures.append(
                {
                    "boundary": boundary,
                    "namespace_id": namespace_id,
                    "document_id": document_id,
                    "fixed_ref": fixed_ref,
                    "current_oid": current_oid,
                    "alias": f"legacy-{index}.md",
                }
            )
    return git, fixtures


async def test_inventory_is_fixed_ref_bounded_and_includes_archived_manual_vaults(tmp_path):
    async with _fresh_schema(tmp_path) as pool:
        fixture = await _make_fixture(pool, tmp_path)
        bridge = LegacyRevisionBridge(
            pool,
            git=fixture["git"],
        )

        scope = await bridge.capture_inventory_scope(
            namespace_id=fixture["namespace_id"],
            fixed_ref=fixture["unrelated_tip"],
            coverage_version="c9-v1",
        )
        inventory = scope.inventory
        doc = next(
            item for item in inventory.documents
            if item.resource_id == fixture["document_one"]
        )
        assert doc.current_path == "renamed.md"
        assert doc.current_commit == fixture["current_oid"]
        assert not hasattr(doc, "body")
        async with bridge.materialize_body(scope, doc) as body:
            assert body == b"new v2\n"
            assert doc.byte_size == len(body)
        assert doc.lineage[-1].legacy_git_oid == fixture["current_oid"]
        assert [entry.legacy_git_oid for entry in doc.lineage] == [
            fixture["initial_oid"],
            fixture["move_oid"],
            fixture["current_oid"],
        ]
        assert [entry.path_at_revision for entry in doc.lineage] == [
            "same.md", "renamed.md", "renamed.md",
        ]
        assert fixture["old_oid"] not in {entry.legacy_git_oid for entry in doc.lineage}
        assert fixture["unrelated_tip"] not in {entry.legacy_git_oid for entry in doc.lineage}

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE documents SET current_commit = $2 WHERE id = $1",
                fixture["document_one"],
                fixture["unrelated_tip"],
            )
        with pytest.raises(InventoryEligibilityError):
            await bridge.capture_inventory(
                namespace_id=fixture["namespace_id"],
                fixed_ref=fixture["unrelated_tip"],
                coverage_version="c9-v2",
            )

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE documents SET current_commit = $2 WHERE id = $1",
                fixture["document_one"],
                fixture["current_oid"],
            )
        with pytest.raises(InventoryEligibilityError):
            await bridge.capture_inventory(
                namespace_id=fixture["namespace_id"],
                fixed_ref=fixture["later_file_tip"],
                coverage_version="c9-v3",
            )

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE documents SET source = 'external_git' WHERE id = $1",
                fixture["document_one"],
            )
        mixed_inventory = await bridge.capture_inventory(
            namespace_id=fixture["namespace_id"],
            fixed_ref=fixture["unrelated_tip"],
            coverage_version="c9-v4",
        )
        assert [item.resource_id for item in mixed_inventory.documents] == [
            fixture["document_two"]
        ]

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE documents SET source = 'manual' WHERE id = $1",
                fixture["document_one"],
            )
            await conn.execute(
                "UPDATE vaults SET status = 'archived' WHERE id = $1",
                fixture["namespace_id"],
            )
        archived_inventory = await bridge.capture_inventory(
            namespace_id=fixture["namespace_id"],
            fixed_ref=fixture["unrelated_tip"],
            coverage_version="c9-v5",
        )
        assert {item.resource_id for item in archived_inventory.documents} == {
            fixture["document_one"],
            fixture["document_two"],
        }

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE vaults SET status = 'active' WHERE id = $1",
                fixture["namespace_id"],
            )
            await conn.execute(
                """
                INSERT INTO vault_external_git (vault_id, remote_url)
                VALUES ($1, 'https://example.invalid/repo.git')
                """,
                fixture["namespace_id"],
            )
        with pytest.raises(ManualVaultRequiredError):
            await bridge.capture_inventory(
                namespace_id=fixture["namespace_id"],
                fixed_ref=fixture["unrelated_tip"],
                coverage_version="c9-v6",
            )
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM native_resources WHERE namespace_id = $1",
                fixture["namespace_id"],
            ) == 0


async def test_inventory_accepts_plain_git_activity_without_akb_footers(tmp_path):
    async with _fresh_schema(tmp_path) as pool:
        git = GitService(storage_path=str(tmp_path / "plain-import-git"))
        vault_name = f"plain-import-{uuid.uuid4().hex}"
        git.init_vault(vault_name)
        current_oid = git.commit_file(
            vault_name,
            "notes/imported.md",
            "# Imported\n\nPlain Git history.\n",
            "Import documentation",
            author_name="Fixture Collector",
            author_email="collector@example.dev",
        )
        current_dt = Repo(str(git._bare_path(vault_name))).commit(
            current_oid
        ).committed_datetime
        fixed_ref = git.commit_file(
            vault_name,
            "notes/unrelated.md",
            "# Unrelated\n",
            "Add unrelated document",
        )

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
            document_id = uuid.uuid4()
            await conn.execute(
                """
                INSERT INTO documents
                    (id, vault_id, path, title, created_at, updated_at,
                     current_commit, source)
                VALUES ($1, $2, 'notes/imported.md', 'Imported', $3, $3, $4, 'manual')
                """,
                document_id,
                namespace_id,
                current_dt - timedelta(seconds=1),
                current_oid,
            )

        scope = await LegacyRevisionBridge(pool, git=git).capture_inventory_scope(
            namespace_id=namespace_id,
            fixed_ref=fixed_ref,
            coverage_version="plain-import-v1",
        )

        assert len(scope.inventory.documents) == 1
        document = scope.inventory.documents[0]
        assert document.resource_id == document_id
        assert document.activity.action == "create"
        assert document.activity.actor == "Fixture Collector"
        assert document.activity.subject == "Import documentation"
        assert document.activity.summary == ""


async def test_inventory_rejects_duplicate_completed_ordinal_zero_anchor(tmp_path):
    async with _fresh_schema(tmp_path) as pool:
        fixture = await _make_fixture(pool, tmp_path)
        bridge = LegacyRevisionBridge(pool, git=fixture["git"])
        backfill = NativeRevisionBackfill(pool, git=fixture["git"], bridge=bridge)
        run, _ = await backfill.prepare_run(
            namespace_id=fixture["namespace_id"],
            fixed_ref=fixture["unrelated_tip"],
            coverage_version="c9-anchor-duplicate-base",
        )
        assert (await backfill.backfill_run(run.run_id)).status == "complete"

        duplicate_run_id = uuid.uuid4()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO native_revision_migration_runs
                    (run_id, namespace_id, fixed_git_oid, coverage_version,
                     inventory_digest, status, started_at, completed_at)
                VALUES ($1, $2, $3, 'c9-anchor-duplicate-corruption',
                        $4, 'complete', NOW(), NOW())
                """,
                duplicate_run_id,
                fixture["namespace_id"],
                "e" * 40,
                "d" * 64,
            )
            await conn.execute(
                """
                INSERT INTO legacy_revision_mappings
                    (namespace_id, resource_id, legacy_git_oid, path_at_revision,
                     resolution, native_revision_id, run_id, lineage_ordinal)
                VALUES ($1, $2, $3, 'corrupt-prior.md', 'bridge', NULL, $4, 0)
                """,
                fixture["namespace_id"],
                fixture["document_one"],
                "a" * 40,
                duplicate_run_id,
            )

        with pytest.raises(InventoryEligibilityError, match="duplicate ordinal-zero"):
            await bridge.capture_inventory(
                namespace_id=fixture["namespace_id"],
                fixed_ref=fixture["unrelated_tip"],
                coverage_version="c9-anchor-duplicate-check",
            )


async def test_backfill_inventory_is_metadata_only_and_materializes_one_body_at_a_time(
    tmp_path,
):
    class TrackingBodyBridge(LegacyRevisionBridge):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.active_bodies = 0
            self.max_active_bodies = 0

        @asynccontextmanager
        async def materialize_body(self, scope, document):
            async with super().materialize_body(scope, document) as body:
                self.active_bodies += 1
                self.max_active_bodies = max(
                    self.max_active_bodies,
                    self.active_bodies,
                )
                try:
                    yield body
                finally:
                    self.active_bodies -= 1

    async with _fresh_schema(tmp_path) as pool:
        fixture = await _make_fixture(pool, tmp_path)
        bridge = TrackingBodyBridge(pool, git=fixture["git"])
        backfill = NativeRevisionBackfill(pool, git=fixture["git"], bridge=bridge)
        run, inventory = await backfill.prepare_run(
            namespace_id=fixture["namespace_id"],
            fixed_ref=fixture["unrelated_tip"],
            coverage_version="c9-metadata-only-memory-bound",
        )

        assert len(inventory.documents) == 2
        assert all(not hasattr(document, "body") for document in inventory.documents)

        result = await backfill.backfill_run(run.run_id)

        assert result.status == "complete"
        assert bridge.max_active_bodies == 1
        assert bridge.active_bodies == 0


async def test_capture_releases_each_source_body_before_reading_the_next(tmp_path):
    class LiveBody(bytes):
        active = 0
        max_active = 0

        def __new__(cls, value):
            instance = super().__new__(cls, value)
            cls.active += 1
            cls.max_active = max(cls.max_active, cls.active)
            return instance

        def __del__(self):
            type(self).active -= 1

    async with _fresh_schema(tmp_path) as pool:
        fixture = await _make_fixture(pool, tmp_path)
        original_history = fixture["git"].manual_fixed_ref_history

        def tracked_history(*args, **kwargs):
            snapshot = original_history(*args, **kwargs)
            snapshot["body"] = LiveBody(snapshot["body"])
            return snapshot

        fixture["git"].manual_fixed_ref_history = tracked_history
        bridge = LegacyRevisionBridge(pool, git=fixture["git"])

        inventory = await bridge.capture_inventory(
            namespace_id=fixture["namespace_id"],
            fixed_ref=fixture["unrelated_tip"],
            coverage_version="c9-capture-max-live-body",
        )

        assert len(inventory.documents) == 2
        assert LiveBody.max_active == 1
        assert LiveBody.active == 0


async def test_p95_inventory_uses_one_validated_scope_and_one_body_read_per_item(
    tmp_path,
    monkeypatch,
):
    document_count = 95
    fixed_ref = "f" * 40
    committed_at = datetime(2026, 1, 1, tzinfo=UTC)

    class CountingGit:
        def __init__(self):
            self.bodies: dict[str, bytes] = {}
            self.commits: dict[str, str] = {}
            self.history_calls = 0
            self.body_read_calls = 0

        def manual_fixed_ref_history(
            self,
            vault_name,
            observed_fixed_ref,
            file_path,
            *,
            current_commit,
            since_epoch=None,
        ):
            del vault_name, since_epoch
            self.history_calls += 1
            assert observed_fixed_ref == fixed_ref
            assert current_commit == self.commits[file_path]
            return {
                "fixed_ref": observed_fixed_ref,
                "current_commit": current_commit,
                "body": self.bodies[file_path],
                "history": [
                    {
                        "legacy_git_oid": current_commit,
                        "path_at_revision": file_path,
                        "committed_at": committed_at,
                    }
                ],
                "activity": {
                    "legacy_git_oid": current_commit,
                    "committed_at": committed_at,
                    "actor": "legacy-writer",
                    "subject": file_path,
                    "summary": f"create {file_path}",
                    "action": "create",
                    "path_from": None,
                    "path_to": file_path,
                    "changed_paths": [
                        {"status": "A", "path_from": None, "path_to": file_path}
                    ],
                },
            }

        def read_file(self, vault_name, file_path, commit=None):
            del vault_name
            self.body_read_calls += 1
            assert commit == self.commits[file_path]
            return self.bodies[file_path].decode("utf-8")

    class CountingRepository(NativeRevisionMigrationRepository):
        def __init__(self, pool):
            super().__init__(pool)
            self.manual_vault_queries = 0

        async def get_manual_vault(self, namespace_id, *, conn=None):
            self.manual_vault_queries += 1
            return await super().get_manual_vault(namespace_id, conn=conn)

    class CountingBridge(LegacyRevisionBridge):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.run_scope_validations = 0

        async def inventory_scope_for_run(self, run):
            self.run_scope_validations += 1
            return await super().inventory_scope_for_run(run)

    canonical_calls = 0
    canonical_digest = LegacyRevisionBridge.canonical_inventory_digest.__func__

    def counted_canonical_digest(cls, **kwargs):
        nonlocal canonical_calls
        canonical_calls += 1
        return canonical_digest(cls, **kwargs)

    monkeypatch.setattr(
        LegacyRevisionBridge,
        "canonical_inventory_digest",
        classmethod(counted_canonical_digest),
    )

    async with _fresh_schema(tmp_path) as pool:
        git = CountingGit()
        vault_name = f"c9-p95-{uuid.uuid4().hex}"
        async with pool.acquire() as conn:
            namespace_id = await conn.fetchval(
                """
                INSERT INTO vaults (name, git_path, status)
                VALUES ($1, '/unused/p95.git', 'active')
                RETURNING id
                """,
                vault_name,
            )
            rows = []
            for index in range(document_count):
                path = f"p95/{index:03d}.md"
                current_commit = f"{index + 1:040x}"
                git.bodies[path] = f"body-{index}\n".encode()
                git.commits[path] = current_commit
                rows.append(
                    (
                        uuid.uuid4(),
                        namespace_id,
                        path,
                        committed_at - timedelta(seconds=1),
                        current_commit,
                    )
                )
            await conn.executemany(
                """
                INSERT INTO documents
                    (id, vault_id, path, title, created_at, updated_at,
                     current_commit, source)
                VALUES ($1, $2, $3, $3, $4, $4, $5, 'manual')
                """,
                rows,
            )

        repository = CountingRepository(pool)
        bridge = CountingBridge(pool, git=git, repository=repository)
        backfill = NativeRevisionBackfill(
            pool,
            git=git,
            bridge=bridge,
            repository=repository,
        )
        run, inventory = await backfill.prepare_run(
            namespace_id=namespace_id,
            fixed_ref=fixed_ref,
            coverage_version="c9-p95-linear-materialization",
        )
        assert len(inventory.documents) == document_count

        result = await backfill.backfill_run(run.run_id)

        assert result.status == "complete"
        assert bridge.run_scope_validations == 1
        assert canonical_calls == 2
        assert repository.manual_vault_queries == 2
        assert git.history_calls == document_count * 2
        assert git.body_read_calls == document_count


async def test_every_backfill_failpoint_rolls_back_then_retries_once(tmp_path):
    async with _fresh_schema(tmp_path) as pool:
        git, fixtures = await _make_compact_failpoint_fixtures(pool, tmp_path)

        for fixture in fixtures:
            bridge = LegacyRevisionBridge(pool, git=git)
            backfill = NativeRevisionBackfill(pool, git=git, bridge=bridge)
            run, inventory = await backfill.prepare_run(
                namespace_id=fixture["namespace_id"],
                fixed_ref=fixture["fixed_ref"],
                coverage_version=f"c9-failpoint-{fixture['boundary']}",
            )
            assert len(inventory.documents) == 1

            async def failpoint(name: str, expected=fixture["boundary"]):
                if name == expected:
                    raise RuntimeError("rollback this boundary")

            failing = NativeRevisionBackfill(
                pool,
                git=git,
                bridge=bridge,
                failpoint=failpoint,
            )
            with pytest.raises(BackfillFailpointError) as failure:
                await failing.backfill_run(run.run_id)
            assert failure.value.boundary == fixture["boundary"]

            async with pool.acquire() as conn:
                rolled_back = await conn.fetchrow(
                    """
                    SELECT
                        (SELECT count(*) FROM native_resources
                          WHERE namespace_id = $1) AS resources,
                        (SELECT count(*) FROM native_revisions
                          WHERE namespace_id = $1) AS revisions,
                        (SELECT count(*) FROM native_payload_manifests
                          WHERE namespace_id = $1) AS manifests,
                        (SELECT count(*) FROM native_resource_path_aliases
                          WHERE namespace_id = $1) AS aliases,
                        (SELECT count(*) FROM native_revision_activity
                          WHERE namespace_id = $1) AS activities,
                        (SELECT count(*) FROM native_invalidation_intents
                          WHERE namespace_id = $1) AS invalidations,
                        (SELECT count(*) FROM legacy_revision_mappings
                          WHERE namespace_id = $1) AS mappings,
                        (SELECT count(*) FROM native_revision_migration_items
                          WHERE run_id = $2 AND status = 'complete') AS completed_items,
                        (SELECT count(*) FROM m1_reference_payloads
                          WHERE namespace_id = $1) AS prepared_payloads,
                        (SELECT status FROM native_revision_migration_items
                          WHERE run_id = $2) AS item_status
                    """,
                    fixture["namespace_id"],
                    run.run_id,
                )
            assert dict(rolled_back) == {
                "resources": 0,
                "revisions": 0,
                "manifests": 0,
                "aliases": 0,
                "activities": 0,
                "invalidations": 0,
                "mappings": 0,
                "completed_items": 0,
                "prepared_payloads": 1,
                "item_status": "pending",
            }

            clean = NativeRevisionBackfill(pool, git=git, bridge=bridge)
            result = await clean.backfill_run(run.run_id)
            assert result.status == "complete"
            async with pool.acquire() as conn:
                published = await conn.fetchrow(
                    """
                    SELECT
                        (SELECT count(*) FROM native_resources
                          WHERE namespace_id = $1) AS resources,
                        (SELECT count(*) FROM native_revisions
                          WHERE namespace_id = $1) AS revisions,
                        (SELECT count(*) FROM native_payload_manifests
                          WHERE namespace_id = $1) AS manifests,
                        (SELECT count(*) FROM native_resource_path_aliases
                          WHERE namespace_id = $1) AS aliases,
                        (SELECT count(*) FROM native_revision_activity
                          WHERE namespace_id = $1) AS activities,
                        (SELECT count(*) FROM native_invalidation_intents
                          WHERE namespace_id = $1) AS invalidations,
                        (SELECT count(*) FROM legacy_revision_mappings
                          WHERE namespace_id = $1) AS mappings,
                        (SELECT count(*) FROM native_revision_migration_items
                          WHERE run_id = $2 AND status = 'complete') AS completed_items,
                        (SELECT count(*) FROM m1_reference_payloads
                          WHERE namespace_id = $1) AS prepared_payloads,
                        (SELECT status FROM native_revision_migration_items
                          WHERE run_id = $2) AS item_status,
                        (SELECT old_path FROM native_resource_path_aliases
                          WHERE namespace_id = $1 AND resource_id = $3) AS alias_path
                    """,
                    fixture["namespace_id"],
                    run.run_id,
                    fixture["document_id"],
                )
            assert dict(published) == {
                "resources": 1,
                "revisions": 1,
                "manifests": 1,
                "aliases": 1,
                "activities": 1,
                "invalidations": 1,
                "mappings": 1,
                "completed_items": 1,
                "prepared_payloads": 1,
                "item_status": "complete",
                "alias_path": fixture["alias"],
            }

            repeated = await clean.backfill_run(run.run_id)
            assert repeated.status == "complete"
            async with pool.acquire() as conn:
                assert await conn.fetchval(
                    """
                    SELECT count(*)
                      FROM native_resources
                     WHERE namespace_id = $1
                    """,
                    fixture["namespace_id"],
                ) == 1
                assert await conn.fetchval(
                    """
                    SELECT count(*)
                      FROM legacy_revision_mappings
                     WHERE namespace_id = $1
                    """,
                    fixture["namespace_id"],
                ) == 1


async def test_mixed_manual_and_external_documents_exclude_external_noop(tmp_path):
    async with _fresh_schema(tmp_path) as pool:
        fixture = await _make_fixture(pool, tmp_path)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE documents SET source = 'external_git' WHERE id = $1",
                fixture["document_two"],
            )

        backfill = NativeRevisionBackfill(pool, git=fixture["git"])
        run, inventory = await backfill.prepare_run(
            namespace_id=fixture["namespace_id"],
            fixed_ref=fixture["unrelated_tip"],
            coverage_version="c9-mixed-source",
        )
        assert [item.resource_id for item in inventory.documents] == [
            fixture["document_one"]
        ]

        result = await backfill.backfill_run(run.run_id)
        assert result.status == "complete"
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM native_revision_migration_items WHERE run_id = $1",
                run.run_id,
            ) == 1
            assert await conn.fetchval(
                """
                SELECT count(*)
                  FROM native_revision_migration_items
                 WHERE run_id = $1 AND legacy_document_id = $2
                """,
                run.run_id,
                fixture["document_two"],
            ) == 0
            assert await conn.fetchval(
                "SELECT count(*) FROM native_resources WHERE namespace_id = $1",
                fixture["namespace_id"],
            ) == 1
            assert await conn.fetchval(
                """
                SELECT count(*)
                  FROM native_resources
                 WHERE namespace_id = $1 AND resource_id = $2
                """,
                fixture["namespace_id"],
                fixture["document_two"],
            ) == 0
            assert await conn.fetchval(
                """
                SELECT count(*)
                  FROM legacy_revision_mappings
                 WHERE namespace_id = $1 AND resource_id = $2
                """,
                fixture["namespace_id"],
                fixture["document_two"],
            ) == 0
            assert await conn.fetchval(
                "SELECT source FROM documents WHERE id = $1",
                fixture["document_two"],
            ) == "external_git"


async def test_current_move_activity_semantics_are_preserved(tmp_path):
    async with _fresh_schema(tmp_path) as pool:
        git = GitService(storage_path=str(tmp_path / "git-current-move"))
        vault_name = f"c9-move-{uuid.uuid4().hex}"
        git.init_vault(vault_name)
        initial_oid = git.commit_file(
            vault_name,
            "same.md",
            "move body\n",
            "[create] same.md\n\nagent: legacy-writer\naction: create\nsummary: create same",
        )
        initial_dt = Repo(str(git._bare_path(vault_name))).commit(
            initial_oid
        ).committed_datetime
        move_oid = git.move_file(
            vault_name,
            "same.md",
            "renamed.md",
            "[move] same.md -> renamed.md\n\nagent: legacy-writer\naction: move\nsummary: move same document",
        )
        fixed_ref = git.commit_file(
            vault_name,
            "unrelated.md",
            "tip\n",
            "unrelated fixed-ref tip",
        )
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
            document_id = uuid.uuid4()
            await conn.execute(
                """
                INSERT INTO documents
                    (id, vault_id, path, title, created_at, updated_at,
                     current_commit, source)
                VALUES ($1, $2, 'renamed.md', 'renamed.md', $3, $3, $4, 'manual')
                """,
                document_id,
                namespace_id,
                initial_dt - timedelta(seconds=1),
                move_oid,
            )

        backfill = NativeRevisionBackfill(pool, git=git)
        run, inventory = await backfill.prepare_run(
            namespace_id=namespace_id,
            fixed_ref=fixed_ref,
            coverage_version="c9-current-move",
        )
        document = inventory.documents[0]
        assert document.activity.action == "move"
        assert document.activity.path_from == "same.md"
        assert document.activity.path_to == "renamed.md"
        body_store = M1PgBodyStore(pool)
        scope = await backfill.bridge.validated_inventory_scope(inventory)
        async with backfill.bridge.materialize_body(scope, document) as body:
            await body_store.prepare_text(
                namespace_id=namespace_id,
                payload=body,
                expected_digest=document.body_digest,
                expected_size=document.byte_size,
            )
        result = await backfill.backfill_run(run.run_id)
        async with pool.acquire() as conn:
            item_error = await conn.fetchval(
                "SELECT error_code FROM native_revision_migration_items WHERE run_id = $1",
                run.run_id,
            )
            activity = await conn.fetchrow(
                """
                SELECT action, actor, subject, summary,
                       changed_path_from, changed_path_to
                  FROM native_revision_activity
                 WHERE namespace_id = $1 AND resource_id = $2
                """,
                namespace_id,
                document_id,
            )
        assert result.status == "complete", item_error
        assert dict(activity) == {
            "action": "create",
            "actor": "akb-native-revision-migration",
            "subject": None,
            "summary": None,
            "changed_path_from": None,
            "changed_path_to": "renamed.md",
        }


async def test_selector_is_hidden_until_run_complete(tmp_path):
    async with _fresh_schema(tmp_path) as pool:
        fixture = await _make_fixture(pool, tmp_path)
        backfill = NativeRevisionBackfill(pool, git=fixture["git"])
        run, inventory = await backfill.prepare_run(
            namespace_id=fixture["namespace_id"],
            fixed_ref=fixture["unrelated_tip"],
            coverage_version="c9-selector",
        )
        document = next(
            item for item in inventory.documents
            if item.resource_id == fixture["document_one"]
        )
        body_store = M1PgBodyStore(pool)
        scope = await backfill.bridge.validated_inventory_scope(inventory)
        async with backfill.bridge.materialize_body(scope, document) as body:
            prepared = await body_store.prepare_text(
                namespace_id=fixture["namespace_id"],
                payload=body,
                expected_digest=document.body_digest,
                expected_size=document.byte_size,
            )
        await backfill._publish_item(
            run=run,
            document=document,
            inventory=inventory,
            prepared=prepared,
        )
        bridge = backfill.bridge
        with pytest.raises(SelectorUnknownError):
            await bridge.resolve_selector(
                resource_id=document.resource_id,
                selector=document.current_commit,
            )
        with pytest.raises(SelectorUnknownError):
            await bridge.resolve_selector(
                resource_id=document.resource_id,
                selector=document.lineage[0].legacy_git_oid[:7],
            )

        other = next(item for item in inventory.documents if item.resource_id != document.resource_id)
        async with backfill.bridge.materialize_body(scope, other) as body:
            other_prepared = await body_store.prepare_text(
                namespace_id=fixture["namespace_id"],
                payload=body,
                expected_digest=other.body_digest,
                expected_size=other.byte_size,
            )
        await backfill._publish_item(
            run=run,
            document=other,
            inventory=inventory,
            prepared=other_prepared,
        )
        repository = NativeRevisionMigrationRepository(pool)
        async with pool.acquire() as conn:
            await repository.set_run_status(run.run_id, "complete", conn=conn)

        current = await bridge.resolve_selector(
            resource_id=document.resource_id,
            selector=document.current_commit,
        )
        assert current.kind == "native"
        old = await bridge.resolve_selector(
            resource_id=document.resource_id,
            selector=document.lineage[0].legacy_git_oid,
        )
        assert old.kind == "bridge"
        assert old.fixed_git_oid == fixture["unrelated_tip"]
        old_prefix = await bridge.resolve_selector(
            resource_id=document.resource_id,
            selector=document.lineage[0].legacy_git_oid[:7],
        )
        assert old_prefix.kind == "bridge"
        with pytest.raises(SelectorUnknownError):
            await bridge.resolve_selector(
                resource_id=document.resource_id,
                selector=fixture["unrelated_tip"],
            )


async def test_atomic_retry_chronology_selector_shapes_and_legacy_unchanged(tmp_path):
    async with _fresh_schema(tmp_path) as pool:
        fixture = await _make_fixture(pool, tmp_path)
        bridge = LegacyRevisionBridge(pool, git=fixture["git"])
        backfill = NativeRevisionBackfill(pool, git=fixture["git"], bridge=bridge)
        run, inventory = await backfill.prepare_run(
            namespace_id=fixture["namespace_id"],
            fixed_ref=fixture["unrelated_tip"],
            coverage_version="c9-backfill",
        )
        async with pool.acquire() as conn:
            legacy_before = await conn.fetchrow(
                """
                SELECT path, current_commit, source, created_at
                  FROM documents
                 WHERE id = $1
                """,
                fixture["document_one"],
            )
            assert await conn.fetchval(
                "SELECT count(*) FROM native_resources WHERE namespace_id = $1",
                fixture["namespace_id"],
            ) == 0

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE documents SET created_at = $2 WHERE id = $1",
                fixture["document_one"],
                fixture["created_at"] + timedelta(seconds=1),
            )
        with pytest.raises(MigrationInventoryDriftError):
            await bridge.prepare_run(
                namespace_id=fixture["namespace_id"],
                fixed_ref=fixture["unrelated_tip"],
                coverage_version="c9-backfill",
            )
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE documents SET created_at = $2 WHERE id = $1",
                fixture["document_one"],
                fixture["created_at"],
            )

        async def fail_before_commit(name: str):
            if name == "authority.before_commit":
                raise RuntimeError("rollback")

        failing = NativeRevisionBackfill(
            pool,
            git=fixture["git"],
            bridge=bridge,
            failpoint=fail_before_commit,
        )
        with pytest.raises(BackfillFailpointError):
            await failing.backfill_run(run.run_id)
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM native_resources WHERE namespace_id = $1",
                fixture["namespace_id"],
            ) == 0
            assert await conn.fetchval(
                "SELECT count(*) FROM legacy_revision_mappings"
            ) == 0
            assert await conn.fetchval(
                "SELECT count(*) FROM m1_reference_payloads WHERE namespace_id = $1",
                fixture["namespace_id"],
            ) == 1
            assert await conn.fetchval(
                "SELECT status FROM native_revision_migration_items WHERE run_id = $1 AND legacy_document_id = $2",
                run.run_id,
                fixture["document_one"],
            ) == "pending"

        result = await backfill.backfill_run(run.run_id)
        assert result.status == "complete"
        async with pool.acquire() as conn:
            counts_before = await conn.fetch(
                """
                SELECT 'resource' AS kind, count(*)::int AS n FROM native_resources
                 WHERE namespace_id = $1
                UNION ALL SELECT 'revision', count(*)::int FROM native_revisions
                 WHERE namespace_id = $1
                UNION ALL SELECT 'manifest', count(*)::int FROM native_payload_manifests
                 WHERE namespace_id = $1
                UNION ALL SELECT 'mapping', count(*)::int FROM legacy_revision_mappings
                 WHERE namespace_id = $1
                UNION ALL SELECT 'item', count(*)::int FROM native_revision_migration_items
                 WHERE namespace_id = $1
                ORDER BY kind
                """,
                fixture["namespace_id"],
            )
            chronology = await conn.fetchrow(
                """
                SELECT rs.created_at AS resource_created,
                       rs.updated_at AS resource_updated,
                       rv.occurred_at AS revision_occurred,
                       a.occurred_at AS activity_occurred,
                       a.action AS activity_action,
                       a.actor AS activity_actor,
                       a.subject AS activity_subject,
                       a.summary AS activity_summary,
                       a.changed_path_from AS activity_path_from,
                       a.changed_path_to AS activity_path_to,
                       i.occurred_at AS invalidation_occurred
                  FROM native_resources rs
                  JOIN native_revisions rv
                    ON rv.resource_id = rs.resource_id
                   AND rv.revision_id = rs.head_revision_id
                  JOIN native_revision_activity a
                    ON a.revision_id = rv.revision_id
                  JOIN native_invalidation_intents i
                    ON i.revision_id = rv.revision_id
                 WHERE rs.resource_id = $1
                """,
                fixture["document_one"],
            )
            revision_id = await conn.fetchval(
                "SELECT head_revision_id FROM native_resources WHERE resource_id = $1",
                fixture["document_one"],
            )
            copied_alias = await conn.fetchrow(
                """
                SELECT old_path, created_revision_id
                  FROM native_resource_path_aliases
                 WHERE namespace_id = $1 AND resource_id = $2
                """,
                fixture["namespace_id"],
                fixture["document_one"],
            )
        assert chronology["resource_created"] == legacy_before["created_at"]
        assert chronology["resource_updated"] == fixture["current_dt"]
        assert chronology["revision_occurred"] == fixture["current_dt"]
        assert chronology["activity_occurred"] == fixture["current_dt"]
        assert chronology["invalidation_occurred"] == fixture["current_dt"]
        assert chronology["activity_action"] == "create"
        assert chronology["activity_actor"] == "akb-native-revision-migration"
        assert chronology["activity_subject"] is None
        assert chronology["activity_summary"] is None
        assert chronology["activity_path_from"] is None
        assert chronology["activity_path_to"] == "renamed.md"
        assert copied_alias["old_path"] == "same.md"
        assert copied_alias["created_revision_id"] == revision_id

        repeated = await backfill.backfill_run(run.run_id)
        assert repeated.status == "complete"
        async with pool.acquire() as conn:
            counts_after = await conn.fetch(
                """
                SELECT 'resource' AS kind, count(*)::int AS n FROM native_resources
                 WHERE namespace_id = $1
                UNION ALL SELECT 'revision', count(*)::int FROM native_revisions
                 WHERE namespace_id = $1
                UNION ALL SELECT 'manifest', count(*)::int FROM native_payload_manifests
                 WHERE namespace_id = $1
                UNION ALL SELECT 'mapping', count(*)::int FROM legacy_revision_mappings
                 WHERE namespace_id = $1
                UNION ALL SELECT 'item', count(*)::int FROM native_revision_migration_items
                 WHERE namespace_id = $1
                ORDER BY kind
                """,
                fixture["namespace_id"],
            )
            legacy_after = await conn.fetchrow(
                "SELECT path, current_commit, source, created_at FROM documents WHERE id = $1",
                fixture["document_one"],
            )
        assert counts_after == counts_before
        assert legacy_after == legacy_before

        current = await bridge.resolve_selector(
            resource_id=fixture["document_one"],
            selector=fixture["current_oid"],
        )
        assert current.kind == "native"
        assert current.native_revision_id == revision_id
        document_one_inventory = next(
            item for item in inventory.documents
            if item.resource_id == fixture["document_one"]
        )
        old_oid = document_one_inventory.lineage[0].legacy_git_oid
        old = await bridge.resolve_selector(
            resource_id=fixture["document_one"],
            selector=old_oid,
        )
        assert old.kind == "bridge"
        assert old.path_at_revision == "same.md"
        unique_prefix = await bridge.resolve_selector(
            resource_id=fixture["document_one"],
            selector=old_oid[:7],
        )
        assert unique_prefix.kind == "bridge"
        unique_long_prefix = await bridge.resolve_selector(
            resource_id=fixture["document_one"],
            selector=old_oid[:39],
        )
        assert unique_long_prefix.kind == "bridge"
        for invalid_selector in (revision_id[:7], revision_id[:39]):
            with pytest.raises(SelectorUnknownError):
                await bridge.resolve_selector(
                    resource_id=fixture["document_one"],
                    selector=invalid_selector,
                )
        for invalid_selector in ("123456", "ABCDEF0"):
            with pytest.raises(SelectorInvalidError):
                await bridge.resolve_selector(
                    resource_id=fixture["document_one"],
                    selector=invalid_selector,
                )
        with pytest.raises(SelectorUnknownError):
            await bridge.resolve_selector(
                resource_id=fixture["document_one"],
                selector=fixture["other_oid"],
            )
        with pytest.raises(SelectorInvalidError):
            await bridge.resolve_selector(
                resource_id=fixture["document_one"],
                selector="not-hex",
            )
        with pytest.raises(SelectorUnknownError):
            await bridge.resolve_selector(
                resource_id=fixture["document_two"],
                selector=fixture["current_oid"],
            )

        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO legacy_revision_mappings
                    (namespace_id, resource_id, legacy_git_oid, path_at_revision,
                     resolution, run_id, lineage_ordinal)
                VALUES ($1, $2, $3, 'synthetic-a.md', 'bridge', $4, 100),
                       ($1, $2, $5, 'synthetic-b.md', 'bridge', $4, 101)
                """,
                fixture["namespace_id"],
                fixture["document_one"],
                "abc1234" + "1" * 33,
                run.run_id,
                "abc1234" + "2" * 33,
            )
            await conn.execute(
                """
                INSERT INTO legacy_revision_mappings
                    (namespace_id, resource_id, legacy_git_oid, path_at_revision,
                     resolution, run_id, lineage_ordinal)
                VALUES ($1, $2, $3, 'collision.md', 'bridge', $4, 102)
                """,
                fixture["namespace_id"],
                fixture["document_one"],
                revision_id,
                run.run_id,
            )
        with pytest.raises(SelectorAmbiguousError):
            await bridge.resolve_selector(
                resource_id=fixture["document_one"],
                selector="abc1234",
            )
        with pytest.raises(SelectorAmbiguousError):
            await bridge.resolve_selector(
                resource_id=fixture["document_one"],
                selector=revision_id,
            )

        repository = NativeRevisionMigrationRepository(pool)
        async with pool.acquire() as conn:
            with pytest.raises(NativeRevisionIdCollisionError):
                await repository.allocate_native_revision_id(
                    conn,
                    resource_id=fixture["document_one"],
                    retained_legacy_oids=set(),
                    revision_id_factory=lambda: old_oid,
                    attempts=1,
                )


async def test_completed_item_is_not_demoted_by_a_queued_failure(tmp_path):
    async with _fresh_schema(tmp_path) as pool:
        git, fixtures = await _make_compact_failpoint_fixtures(pool, tmp_path)
        fixture = fixtures[0]
        repository = NativeRevisionMigrationRepository(pool)
        bridge = LegacyRevisionBridge(pool, git=git, repository=repository)
        backfill = NativeRevisionBackfill(
            pool,
            git=git,
            bridge=bridge,
            repository=repository,
        )
        run, inventory = await backfill.prepare_run(
            namespace_id=fixture["namespace_id"],
            fixed_ref=fixture["fixed_ref"],
            coverage_version="c9-concurrency",
        )
        document = inventory.documents[0]
        result = await backfill.backfill_run(run.run_id)
        assert result.status == "complete"

        async with pool.acquire() as conn:
            async with conn.transaction():
                item = await repository.get_item(
                    run.run_id,
                    document.resource_id,
                    conn=conn,
                    for_update=True,
                )
                assert item is not None and item.status == "complete"
                failure = asyncio.create_task(
                    backfill._mark_failed(
                        run.run_id,
                        document.resource_id,
                        "authority_publish_failed",
                    )
                )
                await asyncio.sleep(0)

        await failure
        item = await repository.get_item(run.run_id, document.resource_id)
        assert item is not None
        assert item.status == "complete"
        assert item.native_head_revision_id is not None
        assert item.error_code is None


async def test_completed_run_replay_preserves_terminal_state_and_selector(tmp_path):
    async with _fresh_schema(tmp_path) as pool:
        git, fixtures = await _make_compact_failpoint_fixtures(pool, tmp_path)
        fixture = fixtures[0]
        repository = NativeRevisionMigrationRepository(pool)
        bridge = LegacyRevisionBridge(pool, git=git, repository=repository)
        backfill = NativeRevisionBackfill(
            pool,
            git=git,
            bridge=bridge,
            repository=repository,
        )
        run, inventory = await backfill.prepare_run(
            namespace_id=fixture["namespace_id"],
            fixed_ref=fixture["fixed_ref"],
            coverage_version="c9-replay",
        )
        document = inventory.documents[0]
        first = await backfill.backfill_run(run.run_id)
        assert first.status == "complete"
        before = await repository.get_run(run.run_id)
        assert before is not None
        assert before.status == "complete"
        assert before.completed_at is not None
        visible_before = await bridge.resolve_selector(
            resource_id=document.resource_id,
            selector=document.current_commit,
        )
        assert visible_before.kind == "native"

        await asyncio.sleep(0.01)
        replay = await backfill.backfill_run(run.run_id)
        assert replay.status == "complete"
        after = await repository.get_run(run.run_id)
        assert after is not None
        assert (
            after.status,
            after.created_at,
            after.started_at,
            after.completed_at,
            after.error,
        ) == (
            before.status,
            before.created_at,
            before.started_at,
            before.completed_at,
            before.error,
        )
        visible_after = await bridge.resolve_selector(
            resource_id=document.resource_id,
            selector=document.current_commit,
        )
        assert visible_after.kind == "native"


async def test_alias_mutation_after_prepare_fails_closed_before_publication(tmp_path):
    async with _fresh_schema(tmp_path) as pool:
        git, fixtures = await _make_compact_failpoint_fixtures(pool, tmp_path)
        fixture = fixtures[0]
        repository = NativeRevisionMigrationRepository(pool)
        bridge = LegacyRevisionBridge(pool, git=git, repository=repository)
        backfill = NativeRevisionBackfill(
            pool,
            git=git,
            bridge=bridge,
            repository=repository,
        )
        run, inventory = await backfill.prepare_run(
            namespace_id=fixture["namespace_id"],
            fixed_ref=fixture["fixed_ref"],
            coverage_version="c9-alias-freeze",
        )
        document = inventory.documents[0]
        assert [alias.old_ref for alias in document.aliases] == [fixture["alias"]]

        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO resource_aliases (vault_id, resource_type, old_ref, resource_id)
                VALUES ($1, 'document', 'post-freeze.md', $2)
                """,
                fixture["namespace_id"],
                document.resource_id,
            )

        with pytest.raises(MigrationInventoryDriftError):
            await backfill.backfill_run(run.run_id)

        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM native_resources WHERE resource_id = $1",
                document.resource_id,
            ) == 0
            assert await conn.fetchval(
                """
                SELECT count(*)
                  FROM native_resource_path_aliases
                 WHERE resource_id = $1
                   AND old_path = 'post-freeze.md'
                """,
                document.resource_id,
            ) == 0


async def test_completed_backfill_bridges_multi_commit_frozen_activity_semantics(tmp_path):
    async with _fresh_schema(tmp_path) as pool:
        fixture = await _make_fixture(pool, tmp_path)
        backfill = NativeRevisionBackfill(pool, git=fixture["git"])
        run, _ = await backfill.prepare_run(
            namespace_id=fixture["namespace_id"],
            fixed_ref=fixture["unrelated_tip"],
            coverage_version="c9-public-activity-continuity",
        )
        assert (await backfill.backfill_run(run.run_id)).status == "complete"

        backend = NativeRevisionBackend(pool=pool, legacy_git=fixture["git"])
        activity = await backend.vault_activity(
            fixture["vault_name"],
            max_count=20,
            since=None,
            path=None,
        )
        frozen_hashes = [
            fixture["current_oid"][:12],
            fixture["move_oid"][:12],
        ]
        expected_by_hash = {
            entry["hash"]: entry
            for entry in await asyncio.to_thread(
                fixture["git"].vault_log,
                fixture["vault_name"],
                max_count=20,
            )
            if entry["hash"] in frozen_hashes
        }
        bridged = [entry for entry in activity if entry["hash"] in frozen_hashes]

        assert [entry["hash"] for entry in bridged] == frozen_hashes
        assert bridged == [expected_by_hash[commit] for commit in frozen_hashes]
        assert fixture["later_file_tip"][:12] not in {entry["hash"] for entry in activity}


async def test_native_move_keeps_completed_legacy_paths_for_historical_reads(tmp_path):
    async with _fresh_schema(tmp_path) as pool:
        fixture = await _make_fixture(pool, tmp_path)
        backfill = NativeRevisionBackfill(pool, git=fixture["git"])
        run, _ = await backfill.prepare_run(
            namespace_id=fixture["namespace_id"],
            fixed_ref=fixture["unrelated_tip"],
            coverage_version="c9-native-move-historical-reads",
        )
        assert (await backfill.backfill_run(run.run_id)).status == "complete"

        documents = NativeDocumentService(pool=pool, legacy_git=fixture["git"])
        backend = NativeRevisionBackend(
            pool=pool,
            document_service=documents,
            legacy_git=fixture["git"],
        )
        moved = await documents.move(
            fixture["vault_name"],
            "renamed.md",
            collection="native-cutover",
            slug="moved-document",
            agent_id="native-writer",
        )
        assert moved.path == "native-cutover/moved-document.md"

        for alias in ("same.md", "renamed.md", moved.path):
            current = await documents.get(fixture["vault_name"], alias)
            assert current.path == moved.path
            assert current.current_commit == moved.commit_hash

        version = await backend.document_version(
            fixture["vault_name"],
            "same.md",
            fixture["initial_oid"],
        )
        assert version is not None
        assert version[1] == "new v1\n"

        history = await backend.document_history(
            fixture["vault_name"],
            "same.md",
            limit=20,
        )
        assert [entry["hash"] for entry in history["history"]] == [
            moved.commit_hash,
            fixture["current_oid"][:12],
            fixture["move_oid"][:12],
            fixture["initial_oid"][:12],
        ]

        diff = await backend.document_diff(
            fixture["vault_name"],
            moved.path,
            fixture["initial_oid"],
        )
        assert diff is not None
        assert diff["file"] == "same.md"
        assert diff["type"] == "modified"
        assert "-old resource" in diff["diff"]
        assert "+new v1" in diff["diff"]


async def test_manual_fixed_ref_history_missing_repo_is_stable_error(tmp_path):
    git = GitService(storage_path=str(tmp_path / "missing-git"))
    with pytest.raises(FixedRefHistoryError):
        git.manual_fixed_ref_history(
            "missing-vault",
            "a" * 40,
            "missing.md",
            current_commit="b" * 40,
        )
