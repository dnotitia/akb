"""Focused PostgreSQL checks for the operator-only C9 final-ref reconcile."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import asyncpg
import pytest
from git import Repo

from app.services.git_service import GitService
from app.services.legacy_revision_bridge import LegacyRevisionBridge, SelectorUnknownError
from app.services.native_revision_backfill import (
    BackfillFailpointError,
    NativeRevisionBackfill,
)
from app.repositories.native_revision_migration_repo import NativeRevisionMigrationRepository
from app.repositories.native_revision_repo import NativeRevisionRepository
from app.services.native_revision_reconcile import (
    NativeRevisionReconcile,
    ReconcileIntegrityError,
)


pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[1]
_INIT_SQL = (_BACKEND / "app" / "db" / "init.sql").read_text(encoding="utf-8")
_MIGRATIONS = _BACKEND / "app" / "db" / "migrations"
_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:5433/akb",  # pragma: allowlist secret
)

# Revisions of one Resource are a parent chain, and the chain is the order.
# ``occurred_at`` is not: reconcile copies it from the legacy Git commit, Git
# commit times are whole seconds, and a fixture that commits twice inside one
# second gives both Revisions the *same* instant.  Sorting on
# ``occurred_at, revision_id`` then let the 40-hex content address decide, which
# is why this file's move assertion flipped on an unchanged tree (akb#399).
_LINEAGE_ORDERED_REVISIONS = """
    WITH RECURSIVE lineage AS (
        SELECT revision_id, parent_revision_id, 0 AS depth
          FROM native_revisions
         WHERE resource_id = $1 AND parent_revision_id IS NULL
        UNION ALL
        SELECT child.revision_id, child.parent_revision_id, parent.depth + 1
          FROM native_revisions child
          JOIN lineage parent ON child.parent_revision_id = parent.revision_id
         WHERE child.resource_id = $1
    )
    SELECT r.revision_id, r.parent_revision_id, r.action, r.path_from, r.path_to
      FROM native_revisions r
      JOIN lineage l ON l.revision_id = r.revision_id
     WHERE r.resource_id = $1
     ORDER BY l.depth
"""


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
    spec = importlib.util.spec_from_file_location(f"migration_c9_reconcile_{filename}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@asynccontextmanager
async def _fresh_schema(tmp_path: Path):
    if not await _reachable():
        # The DB-free unit job has nothing on the default DSN, so skipping there
        # is correct. On the live-PG gate it is not: a skip and a pass read the
        # same, and every assertion in this file is about what PostgreSQL stores.
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"the real-PG gate requires a reachable Postgres at {_DSN}")
        pytest.skip(f"Postgres not reachable at {_DSN}")

    name = f"akb_c9_reconcile_{uuid.uuid4().hex[:12]}"
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


async def _make_fixture(pool, tmp_path: Path, *, document_count: int = 1) -> dict:
    if document_count not in {1, 2}:
        raise ValueError("the reconcile fixture supports one or two documents")
    git = GitService(storage_path=str(tmp_path / "git"))
    vault_name = f"c9-reconcile-{uuid.uuid4().hex}"
    git.init_vault(vault_name)
    initial_oid = git.commit_file(
        vault_name,
        "doc.md",
        "r1 body\n",
        "[create] doc.md\n\nagent: legacy-writer\naction: create\nsummary: create doc",
    )
    initial_dt = Repo(str(git._bare_path(vault_name))).commit(initial_oid).committed_datetime
    initial_oids = [initial_oid]
    document_paths = ["doc.md"]
    if document_count == 2:
        second_oid = git.commit_file(
            vault_name,
            "second.md",
            "second r1 body\n",
            "[create] second.md\n\nagent: legacy-writer\naction: create\nsummary: create second doc",
        )
        initial_oids.append(second_oid)
        document_paths.append("second.md")
        second_dt = Repo(str(git._bare_path(vault_name))).commit(second_oid).committed_datetime
    else:
        second_dt = None
    fixed_r1 = git.commit_file(vault_name, "r1-tip.md", "tip\n", "fixed R1 tip")

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
        document_ids = [uuid.uuid4() for _ in range(document_count)]
        for index, (document_id, path, oid) in enumerate(
            zip(document_ids, document_paths, initial_oids, strict=True)
        ):
            committed_at = initial_dt if index == 0 else second_dt
            assert committed_at is not None
            await conn.execute(
                """
                INSERT INTO documents
                    (id, vault_id, path, title, created_at, updated_at,
                     current_commit, source)
                VALUES ($1, $2, $3, $4, $5, $5, $6, 'manual')
                """,
                document_id,
                namespace_id,
                path,
                path.removesuffix(".md"),
                committed_at - timedelta(seconds=1),
                oid,
            )

    bridge = LegacyRevisionBridge(pool, git=git)
    backfill = NativeRevisionBackfill(pool, git=git, bridge=bridge)
    r1, inventory = await backfill.prepare_run(
        namespace_id=namespace_id,
        fixed_ref=fixed_r1,
        coverage_version="c9-native-genesis-v1",
    )
    assert len(inventory.documents) == document_count
    assert (await backfill.backfill_run(r1.run_id)).status == "complete"
    document_id = document_ids[0]
    return {
        "pool": pool,
        "git": git,
        "vault_name": vault_name,
        "namespace_id": namespace_id,
        "document_id": document_id,
        "document_ids": tuple(document_ids),
        "document_paths": tuple(document_paths),
        "initial_oid": initial_oid,
        "initial_oids": tuple(initial_oids),
        "fixed_r1": fixed_r1,
        "r1": r1,
        "bridge": bridge,
    }


async def _advance_fixture(fixture: dict, *, move: bool, revision: str = "r2") -> dict:
    git = fixture["git"]
    vault_name = fixture["vault_name"]
    document_ids = fixture.get("document_ids", (fixture["document_id"],))
    document_paths = fixture.get("document_paths", ("doc.md",))
    update_oids = []
    final_oids = []
    final_paths = []
    for index, (document_id, path) in enumerate(zip(document_ids, document_paths, strict=True)):
        body = f"{revision} body\n" if index == 0 else f"{revision} body {index}\n"
        update_oid = git.commit_file(
            vault_name,
            path,
            body,
            f"[update] {path}\n\nagent: legacy-writer\naction: update\nsummary: {revision} update {path}",
        )
        final_oid = update_oid
        final_path = path
        if move and index == 0:
            final_oid = git.move_file(
                vault_name,
                path,
                "renamed.md",
                "[move] doc.md -> renamed.md\n\nagent: legacy-writer\naction: move\nsummary: move doc",
            )
            final_path = "renamed.md"
        update_oids.append(update_oid)
        final_oids.append(final_oid)
        final_paths.append(final_path)
        async with fixture["pool"].acquire() as conn:
            await conn.execute(
                """
                UPDATE documents
                   SET path = $2, current_commit = $3, updated_at = NOW()
                 WHERE id = $1
                """,
                document_id,
                final_path,
                final_oid,
            )
    fixed_ref = git.commit_file(
        vault_name,
        f"{revision}-tip.md",
        "tip\n",
        f"fixed {revision.upper()} tip",
    )
    result = {
        **fixture,
        "update_oid": update_oids[0],
        "update_oids": tuple(update_oids),
        "final_oid": final_oids[0],
        "final_oids": tuple(final_oids),
        "final_path": final_paths[0],
        "final_paths": tuple(final_paths),
        "fixed_r2": fixed_ref,
    }
    if revision != "r2":
        result[f"fixed_{revision}"] = fixed_ref
    return result


async def _advance_move_update_suffix(fixture: dict, *, repeated: bool) -> dict:
    git = fixture["git"]
    vault_name = fixture["vault_name"]
    current_path = "doc.md"
    suffix_oids = []

    move_oid = git.move_file(
        vault_name,
        current_path,
        "draft.md",
        "[move] doc.md -> draft.md\n\nagent: legacy-writer\naction: move\nsummary: first move",
    )
    suffix_oids.append(move_oid)
    current_path = "draft.md"
    update_oid = git.commit_file(
        vault_name,
        current_path,
        "r2 body\n",
        "[update] draft.md\n\nagent: legacy-writer\naction: update\nsummary: update after move",
    )
    suffix_oids.append(update_oid)

    if repeated:
        move_oid = git.move_file(
            vault_name,
            current_path,
            "renamed.md",
            "[move] draft.md -> renamed.md\n\nagent: legacy-writer\naction: move\nsummary: second move",
        )
        suffix_oids.append(move_oid)
        current_path = "renamed.md"
        update_oid = git.commit_file(
            vault_name,
            current_path,
            "r3 body\n",
            "[update] renamed.md\n\nagent: legacy-writer\naction: update\nsummary: update after second move",
        )
        suffix_oids.append(update_oid)

    final_oid = suffix_oids[-1]
    async with fixture["pool"].acquire() as conn:
        await conn.execute(
            """
            UPDATE documents
               SET path = $2, current_commit = $3, updated_at = NOW()
             WHERE id = $1
            """,
            fixture["document_id"],
            current_path,
            final_oid,
        )
    fixed_ref = git.commit_file(
        vault_name,
        "suffix-tip.md",
        "tip\n",
        "fixed move/update suffix tip",
    )
    return {
        **fixture,
        "suffix_oids": tuple(suffix_oids),
        "final_oid": final_oid,
        "final_path": current_path,
        "fixed_suffix": fixed_ref,
    }


async def _authority_snapshot(pool, namespace_id: uuid.UUID, resource_id: uuid.UUID) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT 'resource' AS kind, resource_id::text AS subject,
                   current_path AS value, head_revision_id AS extra
              FROM native_resources WHERE namespace_id = $1 AND resource_id = $2
            UNION ALL
            SELECT 'revision', revision_id, action, parent_revision_id
              FROM native_revisions WHERE namespace_id = $1 AND resource_id = $2
            UNION ALL
            SELECT 'mapping', legacy_git_oid, resolution,
                   COALESCE(native_revision_id, '')
              FROM legacy_revision_mappings WHERE namespace_id = $1 AND resource_id = $2
            UNION ALL
            SELECT 'activity', revision_id, action, actor
              FROM native_revision_activity WHERE namespace_id = $1 AND resource_id = $2
            ORDER BY kind, subject
            """,
            namespace_id,
            resource_id,
        )
    return [dict(row) for row in rows]


async def test_operator_reconcile_is_explicitly_constructed():
    assert NativeRevisionReconcile.coverage_version == "c9-native-reconcile-v1"


async def test_update_only_publishes_one_replace_and_keeps_r1_immutable(tmp_path):
    async with _fresh_schema(tmp_path) as pool:
        fixture = await _advance_fixture(await _make_fixture(pool, tmp_path), move=False)
        before_r1 = await _authority_snapshot(pool, fixture["namespace_id"], fixture["document_id"])
        service = NativeRevisionReconcile(pool, git=fixture["git"], bridge=fixture["bridge"])

        result = await service.reconcile(
            namespace_id=fixture["namespace_id"],
            fixed_ref=fixture["fixed_r2"],
        )

        assert result.status == "complete"
        assert result.changed_items == 1
        assert result.items[0].action == "replace"
        async with pool.acquire() as conn:
            revision = await conn.fetchrow(
                """
                SELECT revision_id, parent_revision_id, action
                  FROM native_revisions
                 WHERE resource_id = $1 AND action <> 'create'
                """,
                fixture["document_id"],
            )
            head = await conn.fetchrow(
                """
                SELECT rs.current_path, rs.head_revision_id, pm.digest,
                       pm.byte_size
                  FROM native_resources rs
                  JOIN native_revisions rv ON rv.resource_id = rs.resource_id
                   AND rv.revision_id = rs.head_revision_id
                  JOIN native_payload_manifests pm
                    ON pm.payload_manifest_id = rv.payload_manifest_id
                 WHERE rs.resource_id = $1
                """,
                fixture["document_id"],
            )
            mappings = await conn.fetch(
                """
                SELECT legacy_git_oid, resolution, native_revision_id
                  FROM legacy_revision_mappings
                 WHERE resource_id = $1
                 ORDER BY lineage_ordinal
                """,
                fixture["document_id"],
            )
        assert revision["action"] == "replace"
        old_head = next(row["extra"] for row in before_r1 if row["kind"] == "resource")
        assert revision["parent_revision_id"] == old_head
        assert head["current_path"] == "doc.md"
        assert head["head_revision_id"] == revision["revision_id"]
        assert head["digest"] == hashlib.sha256(b"r2 body\n").hexdigest()
        assert [row["legacy_git_oid"] for row in mappings] == [
            fixture["initial_oid"],
            fixture["final_oid"],
        ]
        assert [row["resolution"] for row in mappings] == ["native", "native"]
        after_r1 = await _authority_snapshot(pool, fixture["namespace_id"], fixture["document_id"])
        assert all(
            row in after_r1
            for row in before_r1
            if row["kind"] != "resource"
        )


async def test_update_then_final_move_collapses_to_one_move_and_binds_suffix(tmp_path):
    async with _fresh_schema(tmp_path) as pool:
        fixture = await _advance_fixture(await _make_fixture(pool, tmp_path), move=True)
        service = NativeRevisionReconcile(pool, git=fixture["git"], bridge=fixture["bridge"])

        result = await service.reconcile(
            namespace_id=fixture["namespace_id"],
            fixed_ref=fixture["fixed_r2"],
        )

        assert result.items[0].action == "move"
        async with pool.acquire() as conn:
            revisions = await conn.fetch(
                _LINEAGE_ORDERED_REVISIONS,
                fixture["document_id"],
            )
            mappings = await conn.fetch(
                """
                SELECT legacy_git_oid, resolution, native_revision_id
                  FROM legacy_revision_mappings
                 WHERE resource_id = $1
                 ORDER BY lineage_ordinal
                """,
                fixture["document_id"],
            )
            alias = await conn.fetchrow(
                """
                SELECT old_path, created_revision_id
                  FROM native_resource_path_aliases
                 WHERE resource_id = $1 AND retired_revision_id IS NULL
                """,
                fixture["document_id"],
            )
        assert len(revisions) == 2
        assert revisions[-1]["action"] == "move"
        assert revisions[-1]["parent_revision_id"] == revisions[0]["revision_id"]
        assert revisions[-1]["path_from"] == "doc.md"
        assert revisions[-1]["path_to"] == "renamed.md"
        assert [row["legacy_git_oid"] for row in mappings] == [
            fixture["initial_oid"],
            fixture["update_oid"],
            fixture["final_oid"],
        ]
        assert [row["resolution"] for row in mappings] == ["native", "bridge", "native"]
        assert mappings[-1]["native_revision_id"] == revisions[-1]["revision_id"]
        assert alias["old_path"] == "doc.md"
        assert alias["created_revision_id"] == revisions[-1]["revision_id"]


async def _reconciled_head_and_history(pool, tmp_path, monkeypatch, *, make_at: str, advance_at: str):
    """Build the reconcile fixture with the legacy clock pinned.

    ``GIT_AUTHOR_DATE``/``GIT_COMMITTER_DATE`` are what a legacy vault's commit
    times actually are to us — reconcile copies them into ``occurred_at``
    verbatim — so pinning them here builds the real state through the real
    writer instead of forging rows the immutability trigger rightly refuses.
    """
    for name in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"):
        monkeypatch.setenv(name, make_at)
    fixture = await _make_fixture(pool, tmp_path)
    for name in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"):
        monkeypatch.setenv(name, advance_at)
    fixture = await _advance_fixture(fixture, move=True)

    service = NativeRevisionReconcile(pool, git=fixture["git"], bridge=fixture["bridge"])
    await service.reconcile(
        namespace_id=fixture["namespace_id"],
        fixed_ref=fixture["fixed_r2"],
    )
    resource_id = fixture["document_id"]
    async with pool.acquire() as conn:
        head_revision_id = await conn.fetchval(
            "SELECT head_revision_id FROM native_resources WHERE resource_id = $1",
            resource_id,
        )
        instants = await conn.fetchval(
            "SELECT count(DISTINCT occurred_at) FROM native_revisions WHERE resource_id = $1",
            resource_id,
        )
    history = await NativeRevisionRepository(pool).list_history(resource_id=resource_id)
    return head_revision_id, instants, history


def _assert_history_is_the_chain(head_revision_id, history):
    assert [row["action"] for row in history] == ["move", "create"]
    assert history[0]["revision_id"] == head_revision_id
    assert history[1]["revision_id"] == history[0]["parent_revision_id"]


async def test_history_is_the_chain_when_two_revisions_share_one_second(tmp_path, monkeypatch):
    """Git commit times are whole seconds, so one instant can hold both Revisions.

    The fixture already reaches this state on roughly 4 runs in 10 by committing
    twice inside the same second; pinning the legacy clock makes it every run.
    ``occurred_at`` then carries no order, and whatever breaks the tie decides
    what the caller is told is newest.

    Sorting on ``revision_id`` gets that right half the time by luck, so the
    loop below keeps building fixtures until it has the half it gets wrong — a
    head whose content address sorts *below* its own parent's.  Asserting
    against a case that only shows up on a coin flip is how this file came to
    have a test that flipped on an unchanged tree (akb#399).
    """
    async with _fresh_schema(tmp_path) as pool:
        instant = "2026-01-02T03:04:05+00:00"
        for _ in range(24):
            head_revision_id, instants, history = await _reconciled_head_and_history(
                pool, tmp_path, monkeypatch, make_at=instant, advance_at=instant
            )
            assert instants == 1, "the pinned legacy clock did not collapse to one instant"
            parent_revision_id = next(
                row["revision_id"] for row in history if row["revision_id"] != head_revision_id
            )
            if head_revision_id < parent_revision_id:
                break
        else:
            pytest.fail("could not build a head whose content address sorts below its parent's")

        _assert_history_is_the_chain(head_revision_id, history)


async def test_move_then_update_collapses_to_move_and_resolves_suffix_mappings(tmp_path):
    async with _fresh_schema(tmp_path) as pool:
        fixture = await _advance_move_update_suffix(
            await _make_fixture(pool, tmp_path),
            repeated=False,
        )
        result = await NativeRevisionReconcile(
            pool,
            git=fixture["git"],
            bridge=fixture["bridge"],
        ).reconcile(
            namespace_id=fixture["namespace_id"],
            fixed_ref=fixture["fixed_suffix"],
        )

        assert result.status == "complete"
        assert result.items[0].action == "move"
        async with pool.acquire() as conn:
            revision = await conn.fetchrow(
                """
                SELECT revision_id, parent_revision_id, action, path_from, path_to
                  FROM native_revisions
                 WHERE resource_id = $1 AND action <> 'create'
                """,
                fixture["document_id"],
            )
            mappings = await conn.fetch(
                """
                SELECT legacy_git_oid, path_at_revision, resolution, native_revision_id
                  FROM legacy_revision_mappings
                 WHERE resource_id = $1
                 ORDER BY lineage_ordinal
                """,
                fixture["document_id"],
            )
        assert revision["action"] == "move"
        assert revision["path_from"] == "doc.md"
        assert revision["path_to"] == "draft.md"
        assert [row["legacy_git_oid"] for row in mappings] == [
            fixture["initial_oid"],
            *fixture["suffix_oids"],
        ]
        assert [row["path_at_revision"] for row in mappings] == [
            "doc.md",
            "draft.md",
            "draft.md",
        ]
        assert [row["resolution"] for row in mappings] == ["native", "bridge", "native"]

        intermediate = await fixture["bridge"].resolve_selector(
            resource_id=fixture["document_id"],
            selector=fixture["suffix_oids"][0],
        )
        final = await fixture["bridge"].resolve_selector(
            resource_id=fixture["document_id"],
            selector=fixture["final_oid"],
        )
        assert intermediate.kind == "bridge"
        assert intermediate.path_at_revision == "draft.md"
        assert intermediate.native_revision_id is None
        assert final.kind == "native"
        assert final.native_revision_id == revision["revision_id"]


async def test_repeated_move_update_suffix_resumes_replays_and_collapses_once(tmp_path):
    async with _fresh_schema(tmp_path) as pool:
        fixture = await _advance_move_update_suffix(
            await _make_fixture(pool, tmp_path),
            repeated=True,
        )
        fired = False

        async def failpoint(name: str):
            nonlocal fired
            if name == "authority.before_commit" and not fired:
                fired = True
                raise RuntimeError("interrupt repeated suffix publication")

        failing = NativeRevisionReconcile(
            pool,
            git=fixture["git"],
            bridge=fixture["bridge"],
            failpoint=failpoint,
        )
        with pytest.raises(BackfillFailpointError):
            await failing.reconcile(
                namespace_id=fixture["namespace_id"],
                fixed_ref=fixture["fixed_suffix"],
            )

        async with pool.acquire() as conn:
            run_id = await conn.fetchval(
                """
                SELECT run_id
                  FROM native_revision_migration_runs
                 WHERE namespace_id = $1 AND fixed_git_oid = $2
                """,
                fixture["namespace_id"],
                fixture["fixed_suffix"],
            )
            assert await conn.fetchval(
                "SELECT count(*) FROM native_revisions WHERE resource_id = $1",
                fixture["document_id"],
            ) == 1

        clean = NativeRevisionReconcile(pool, git=fixture["git"], bridge=fixture["bridge"])
        resumed = await clean.reconcile_run(run_id)
        assert resumed.status == "complete"
        assert resumed.items[0].action == "move"
        async with pool.acquire() as conn:
            revision = await conn.fetchrow(
                """
                SELECT revision_id, action, path_from, path_to
                  FROM native_revisions
                 WHERE resource_id = $1 AND action <> 'create'
                """,
                fixture["document_id"],
            )
            mappings = await conn.fetch(
                """
                SELECT legacy_git_oid, path_at_revision, resolution,
                       native_revision_id, run_id
                  FROM legacy_revision_mappings
                 WHERE resource_id = $1
                 ORDER BY lineage_ordinal
                """,
                fixture["document_id"],
            )
            before_replay = await conn.fetchrow(
                """
                SELECT
                    (SELECT count(*) FROM native_revisions) AS revisions,
                    (SELECT count(*) FROM legacy_revision_mappings) AS mappings,
                    (SELECT count(*) FROM native_revision_activity) AS activity,
                    (SELECT count(*) FROM native_resource_path_aliases) AS aliases
                """
            )

        assert revision["action"] == "move"
        assert revision["path_from"] == "doc.md"
        assert revision["path_to"] == "renamed.md"
        assert [row["legacy_git_oid"] for row in mappings] == [
            fixture["initial_oid"],
            *fixture["suffix_oids"],
        ]
        assert [row["path_at_revision"] for row in mappings] == [
            "doc.md",
            "draft.md",
            "draft.md",
            "renamed.md",
            "renamed.md",
        ]
        assert [row["resolution"] for row in mappings] == [
            "native",
            "bridge",
            "bridge",
            "bridge",
            "native",
        ]
        assert all(row["run_id"] == run_id for row in mappings[1:])
        assert mappings[-1]["native_revision_id"] == revision["revision_id"]

        for oid in fixture["suffix_oids"][:-1]:
            resolution = await fixture["bridge"].resolve_selector(
                resource_id=fixture["document_id"],
                selector=oid,
            )
            assert resolution.kind == "bridge"
            assert resolution.native_revision_id is None
        final = await fixture["bridge"].resolve_selector(
            resource_id=fixture["document_id"],
            selector=fixture["final_oid"],
        )
        assert final.kind == "native"
        assert final.native_revision_id == revision["revision_id"]

        replay = await clean.reconcile(
            namespace_id=fixture["namespace_id"],
            fixed_ref=fixture["fixed_suffix"],
        )
        assert replay.idempotent_replay is True
        async with pool.acquire() as conn:
            after_replay = await conn.fetchrow(
                """
                SELECT
                    (SELECT count(*) FROM native_revisions) AS revisions,
                    (SELECT count(*) FROM legacy_revision_mappings) AS mappings,
                    (SELECT count(*) FROM native_revision_activity) AS activity,
                    (SELECT count(*) FROM native_resource_path_aliases) AS aliases
                """
            )
        assert dict(after_replay) == dict(before_replay)


async def test_unchanged_final_ref_has_no_item_or_native_fact(tmp_path):
    async with _fresh_schema(tmp_path) as pool:
        fixture = await _make_fixture(pool, tmp_path)
        fixed_r2 = fixture["git"].commit_file(
            fixture["vault_name"], "unchanged-tip.md", "tip\n", "fixed R2 tip"
        )
        service = NativeRevisionReconcile(pool, git=fixture["git"], bridge=fixture["bridge"])
        before = await _authority_snapshot(pool, fixture["namespace_id"], fixture["document_id"])

        result = await service.reconcile(
            namespace_id=fixture["namespace_id"],
            fixed_ref=fixed_r2,
        )

        assert result.changed_items == 0
        assert result.unchanged_items == 1
        assert await _authority_snapshot(pool, fixture["namespace_id"], fixture["document_id"]) == before
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM native_revision_migration_items WHERE run_id = $1",
                result.run_id,
            ) == 0


async def test_terminal_replay_is_a_noop_and_before_commit_resumes(tmp_path):
    async with _fresh_schema(tmp_path) as pool:
        fixture = await _advance_fixture(await _make_fixture(pool, tmp_path), move=False)
        fired = False

        async def failpoint(name: str):
            nonlocal fired
            if name == "authority.before_commit" and not fired:
                fired = True
                raise RuntimeError("reconcile interruption")

        failing = NativeRevisionReconcile(
            pool,
            git=fixture["git"],
            bridge=fixture["bridge"],
            failpoint=failpoint,
        )
        with pytest.raises(BackfillFailpointError):
            await failing.reconcile(
                namespace_id=fixture["namespace_id"],
                fixed_ref=fixture["fixed_r2"],
            )
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM native_revisions WHERE resource_id = $1",
                fixture["document_id"],
            ) == 1
            assert await conn.fetchval(
                "SELECT count(*) FROM m1_reference_payloads WHERE namespace_id = $1",
                fixture["namespace_id"],
            ) == 2
            assert await conn.fetchval(
                """
                SELECT i.status
                  FROM native_revision_migration_items i
                  JOIN native_revision_migration_runs r
                    ON r.run_id = i.run_id AND r.namespace_id = i.namespace_id
                 WHERE i.namespace_id = $1
                   AND i.legacy_document_id = $2
                   AND r.fixed_git_oid = $3
                """,
                fixture["namespace_id"],
                fixture["document_id"],
                fixture["fixed_r2"],
            ) == "pending"

        clean = NativeRevisionReconcile(pool, git=fixture["git"], bridge=fixture["bridge"])
        resumed = await clean.reconcile(
            namespace_id=fixture["namespace_id"],
            fixed_ref=fixture["fixed_r2"],
        )
        replay = await clean.reconcile(
            namespace_id=fixture["namespace_id"],
            fixed_ref=fixture["fixed_r2"],
        )
        assert resumed.status == replay.status == "complete"
        assert replay.idempotent_replay is True
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM native_revisions WHERE resource_id = $1",
                fixture["document_id"],
            ) == 2
            assert await conn.fetchval(
                "SELECT count(*) FROM legacy_revision_mappings WHERE resource_id = $1",
                fixture["document_id"],
            ) == 2


async def test_multi_item_resume_sees_only_completed_current_run_items(tmp_path):
    async with _fresh_schema(tmp_path) as pool:
        fixture = await _advance_fixture(
            await _make_fixture(pool, tmp_path, document_count=2),
            move=False,
        )
        fired = False

        async def failpoint(name: str):
            nonlocal fired
            if name == "reconcile.after_item_commit" and not fired:
                fired = True
                raise RuntimeError("stop between committed reconcile items")

        failing = NativeRevisionReconcile(
            pool,
            git=fixture["git"],
            bridge=fixture["bridge"],
            failpoint=failpoint,
        )
        with pytest.raises(BackfillFailpointError):
            await failing.reconcile(
                namespace_id=fixture["namespace_id"],
                fixed_ref=fixture["fixed_r2"],
            )

        repository = NativeRevisionMigrationRepository(pool)
        async with pool.acquire() as conn:
            run_row = await conn.fetchrow(
                """
                SELECT run_id, status
                  FROM native_revision_migration_runs
                 WHERE namespace_id = $1 AND fixed_git_oid = $2
                """,
                fixture["namespace_id"],
                fixture["fixed_r2"],
            )
        assert run_row["status"] == "running"
        run_id = run_row["run_id"]
        items = await repository.list_items(run_id)
        assert [item.status for item in items].count("complete") == 1
        assert [item.status for item in items].count("pending") == 1
        completed_item = next(item for item in items if item.status == "complete")
        pending_item = next(item for item in items if item.status == "pending")
        final_oids = dict(zip(fixture["document_ids"], fixture["final_oids"], strict=True))
        completed_oid = final_oids[completed_item.native_resource_id]
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM native_revisions WHERE resource_id = $1",
                completed_item.native_resource_id,
            ) == 2
            assert await conn.fetchval(
                "SELECT count(*) FROM native_revisions WHERE resource_id = $1",
                pending_item.native_resource_id,
            ) == 1

        public_mappings = await repository.list_resource_mappings(
            namespace_id=fixture["namespace_id"],
            resource_id=completed_item.native_resource_id,
        )
        assert all(mapping.run_id != run_id for mapping in public_mappings)
        assert all(mapping.legacy_git_oid != completed_oid for mapping in public_mappings)
        assert (
            await repository.mapping_for_native_revision(
                namespace_id=fixture["namespace_id"],
                resource_id=completed_item.native_resource_id,
                native_revision_id=completed_item.native_head_revision_id,
            )
            is None
        )
        with pytest.raises(SelectorUnknownError):
            await fixture["bridge"].resolve_selector(
                resource_id=completed_item.native_resource_id,
                selector=completed_oid,
            )

        internal_mappings = await repository.list_resource_mappings_for_reconcile(
            namespace_id=fixture["namespace_id"],
            resource_id=completed_item.native_resource_id,
            run_id=run_id,
        )
        assert any(mapping.legacy_git_oid == completed_oid for mapping in internal_mappings)
        internal_head = await repository.mapping_for_native_revision_for_reconcile(
            namespace_id=fixture["namespace_id"],
            resource_id=completed_item.native_resource_id,
            native_revision_id=completed_item.native_head_revision_id,
            run_id=run_id,
        )
        assert internal_head is not None
        assert internal_head.run_id == run_id
        foreign_run_mappings = await repository.list_resource_mappings_for_reconcile(
            namespace_id=fixture["namespace_id"],
            resource_id=completed_item.native_resource_id,
            run_id=fixture["r1"].run_id,
        )
        assert all(mapping.legacy_git_oid != completed_oid for mapping in foreign_run_mappings)
        assert (
            await repository.mapping_for_native_revision_for_reconcile(
                namespace_id=fixture["namespace_id"],
                resource_id=completed_item.native_resource_id,
                native_revision_id=completed_item.native_head_revision_id,
                run_id=fixture["r1"].run_id,
            )
            is None
        )

        clean = NativeRevisionReconcile(pool, git=fixture["git"], bridge=fixture["bridge"])
        resumed = await clean.reconcile(
            namespace_id=fixture["namespace_id"],
            fixed_ref=fixture["fixed_r2"],
        )
        assert resumed.run_id == run_id
        assert resumed.status == "complete"
        assert resumed.completed_items == 1

        async with pool.acquire() as conn:
            before_replay = await conn.fetchrow(
                """
                SELECT
                    (SELECT count(*) FROM native_revisions) AS revisions,
                    (SELECT count(*) FROM legacy_revision_mappings) AS mappings,
                    (SELECT count(*) FROM native_revision_activity) AS activity,
                    (SELECT count(*) FROM native_invalidation_intents) AS invalidations
                """
            )
        replay = await clean.reconcile(
            namespace_id=fixture["namespace_id"],
            fixed_ref=fixture["fixed_r2"],
        )
        assert replay.idempotent_replay is True
        async with pool.acquire() as conn:
            after_replay = await conn.fetchrow(
                """
                SELECT
                    (SELECT count(*) FROM native_revisions) AS revisions,
                    (SELECT count(*) FROM legacy_revision_mappings) AS mappings,
                    (SELECT count(*) FROM native_revision_activity) AS activity,
                    (SELECT count(*) FROM native_invalidation_intents) AS invalidations
                """
            )
        assert dict(after_replay) == dict(before_replay)
        visible = await repository.mapping_for_native_revision(
            namespace_id=fixture["namespace_id"],
            resource_id=completed_item.native_resource_id,
            native_revision_id=completed_item.native_head_revision_id,
        )
        assert visible is not None
        assert visible.run_id == run_id
        resolution = await fixture["bridge"].resolve_selector(
            resource_id=completed_item.native_resource_id,
            selector=completed_oid,
        )
        assert resolution.kind == "native"


async def test_successive_reconciles_preserve_run_item_and_mapping_ownership(tmp_path):
    async with _fresh_schema(tmp_path) as pool:
        fixture = await _make_fixture(pool, tmp_path)
        after_r2 = await _advance_fixture(fixture, move=False)
        service = NativeRevisionReconcile(pool, git=after_r2["git"], bridge=after_r2["bridge"])
        r2_result = await service.reconcile(
            namespace_id=after_r2["namespace_id"],
            fixed_ref=after_r2["fixed_r2"],
        )
        assert r2_result.status == "complete"

        after_r3 = await _advance_fixture(after_r2, move=False, revision="r3")
        r3_result = await service.reconcile(
            namespace_id=after_r3["namespace_id"],
            fixed_ref=after_r3["fixed_r3"],
        )
        assert r3_result.status == "complete"

        async with pool.acquire() as conn:
            runs = await conn.fetch(
                """
                SELECT fixed_git_oid, run_id, status
                  FROM native_revision_migration_runs
                 WHERE namespace_id = $1
                 ORDER BY created_at, run_id
                """,
                after_r3["namespace_id"],
            )
            mappings = await conn.fetch(
                """
                SELECT legacy_git_oid, run_id, lineage_ordinal, resolution
                  FROM legacy_revision_mappings
                 WHERE resource_id = $1
                 ORDER BY lineage_ordinal
                """,
                after_r3["document_id"],
            )
            items = await conn.fetch(
                """
                SELECT run_id, status, native_head_revision_id
                  FROM native_revision_migration_items
                 WHERE legacy_document_id = $1
                 ORDER BY created_at, run_id
                """,
                after_r3["document_id"],
            )

        run_by_ref = {row["fixed_git_oid"]: row for row in runs}
        r1_run_id = after_r3["r1"].run_id
        r2_run_id = r2_result.run_id
        r3_run_id = r3_result.run_id
        assert set(run_by_ref) == {
            after_r3["fixed_r1"],
            after_r2["fixed_r2"],
            after_r3["fixed_r3"],
        }
        assert {row["run_id"] for row in runs} == {r1_run_id, r2_run_id, r3_run_id}
        assert all(row["status"] == "complete" for row in runs)
        assert [row["run_id"] for row in mappings] == [r1_run_id, r2_run_id, r3_run_id]
        assert [row["legacy_git_oid"] for row in mappings] == [
            after_r3["initial_oid"],
            after_r2["final_oid"],
            after_r3["final_oid"],
        ]
        assert [row["resolution"] for row in mappings] == ["native", "native", "native"]
        assert {row["run_id"] for row in items} == {r1_run_id, r2_run_id, r3_run_id}
        assert all(row["status"] == "complete" for row in items)
        assert all(row["native_head_revision_id"] is not None for row in items)


async def test_reconcile_rejects_missing_previously_migrated_document_without_facts(tmp_path):
    async with _fresh_schema(tmp_path) as pool:
        fixture = await _make_fixture(pool, tmp_path)
        before = await _authority_snapshot(pool, fixture["namespace_id"], fixture["document_id"])
        async with pool.acquire() as conn:
            before_counts = dict(
                await conn.fetchrow(
                    """
                    SELECT
                        (SELECT count(*) FROM native_revisions) AS revisions,
                        (SELECT count(*) FROM legacy_revision_mappings) AS mappings,
                        (SELECT count(*) FROM native_revision_activity) AS activity,
                        (SELECT count(*) FROM native_invalidation_intents) AS invalidations,
                        (SELECT count(*) FROM native_revision_migration_items) AS items,
                        (SELECT count(*) FROM native_revision_migration_runs) AS runs
                    """
                )
            )
            await conn.execute(
                "DELETE FROM documents WHERE id = $1",
                fixture["document_id"],
            )
        fixed_r2 = fixture["git"].commit_file(
            fixture["vault_name"], "missing-tip.md", "tip\n", "fixed R2 missing document"
        )

        with pytest.raises(ReconcileIntegrityError) as failure:
            await NativeRevisionReconcile(
                pool,
                git=fixture["git"],
                bridge=fixture["bridge"],
            ).reconcile(
                namespace_id=fixture["namespace_id"],
                fixed_ref=fixed_r2,
            )
        assert failure.value.code == "native_revision_reconcile_missing_document"
        async with pool.acquire() as conn:
            after_counts = dict(
                await conn.fetchrow(
                    """
                    SELECT
                        (SELECT count(*) FROM native_revisions) AS revisions,
                        (SELECT count(*) FROM legacy_revision_mappings) AS mappings,
                        (SELECT count(*) FROM native_revision_activity) AS activity,
                        (SELECT count(*) FROM native_invalidation_intents) AS invalidations,
                        (SELECT count(*) FROM native_revision_migration_items) AS items,
                        (SELECT count(*) FROM native_revision_migration_runs) AS runs
                    """
                )
            )
        assert after_counts == before_counts
        assert await _authority_snapshot(pool, fixture["namespace_id"], fixture["document_id"]) == before


async def test_reconcile_rejects_missing_resource_without_facts(tmp_path):
    async with _fresh_schema(tmp_path) as pool:
        fixture = await _advance_fixture(await _make_fixture(pool, tmp_path), move=True)
        new_oid = fixture["git"].commit_file(
            fixture["vault_name"],
            "new.md",
            "new body\n",
            "[create] new.md\n\nagent: legacy-writer\naction: create\nsummary: create new",
        )
        new_dt = Repo(str(fixture["git"]._bare_path(fixture["vault_name"]))).commit(new_oid).committed_datetime
        new_id = uuid.uuid4()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO documents
                    (id, vault_id, path, title, created_at, updated_at,
                     current_commit, source)
                VALUES ($1, $2, 'new.md', 'new', $3, $3, $4, 'manual')
                """,
                new_id,
                fixture["namespace_id"],
                new_dt - timedelta(seconds=1),
                new_oid,
            )
        fixed_r3 = fixture["git"].commit_file(
            fixture["vault_name"], "r3-tip.md", "tip\n", "fixed R3 tip"
        )
        async with pool.acquire() as conn:
            before_revisions = await conn.fetchval("SELECT count(*) FROM native_revisions")
        service = NativeRevisionReconcile(pool, git=fixture["git"], bridge=fixture["bridge"])
        with pytest.raises(ReconcileIntegrityError):
            await service.reconcile(
                namespace_id=fixture["namespace_id"],
                fixed_ref=fixed_r3,
            )
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM native_revisions") == before_revisions
            assert await conn.fetchval(
                "SELECT count(*) FROM native_resources WHERE resource_id = $1", new_id
            ) == 0


async def test_legacy_documents_and_git_are_unchanged_by_reconcile(tmp_path):
    async with _fresh_schema(tmp_path) as pool:
        fixture = await _advance_fixture(await _make_fixture(pool, tmp_path), move=True)
        before_git_head = fixture["git"].current_commit(fixture["vault_name"])
        before_document = await _authority_snapshot(pool, fixture["namespace_id"], fixture["document_id"])
        async with pool.acquire() as conn:
            before_legacy = dict(
                await conn.fetchrow(
                    "SELECT path, current_commit, source FROM documents WHERE id = $1",
                    fixture["document_id"],
                )
            )

        await NativeRevisionReconcile(
            pool,
            git=fixture["git"],
            bridge=fixture["bridge"],
        ).reconcile(
            namespace_id=fixture["namespace_id"],
            fixed_ref=fixture["fixed_r2"],
        )

        async with pool.acquire() as conn:
            after_legacy = dict(
                await conn.fetchrow(
                    "SELECT path, current_commit, source FROM documents WHERE id = $1",
                    fixture["document_id"],
                )
            )
        assert after_legacy == before_legacy
        assert fixture["git"].current_commit(fixture["vault_name"]) == before_git_head
        assert fixture["git"].read_file(
            fixture["vault_name"], "renamed.md", fixture["final_oid"]
        ) == "r2 body\n"
        assert any(row["kind"] == "resource" for row in before_document)
