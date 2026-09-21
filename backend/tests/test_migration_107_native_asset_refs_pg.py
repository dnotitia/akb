"""Live-PostgreSQL contract for migration 107 (Native document image refs).

What 107 adds is a *database* guarantee, so every assertion here is raw SQL
against a real database rather than a drive of the Python paths. That is the
point: a Native document's image reference has to survive a delete that is not
a row delete, refuse a resource that is not a document, and keep the Git arm's
behaviour untouched — none of which the service layer can promise on its own.

Covered:

  * the migration is idempotent, and leaves both arms' uniqueness intact;
  * a Git-arm reference still inserts, and still cascades from `documents`;
  * a Native reference to a *document* inserts, and one to a *file* is refused
    by the composite FK — the surface is pinned, not merely intended;
  * naming both arms, or neither, is refused;
  * retiring a Native document (`lifecycle = 'deleted'`, which leaves the row
    in place) drops that document's live references and no one else's;
  * deleting the `native_resources` row cascades.

Runs in a disposable database. Registered in the `pgvector e2e (live DB)` CI
job, which sets `AKB_TEST_DSN`; a test that only ever skipped would be a gate
that never fires.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import pytest

pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[1]
_INIT_SQL = (_BACKEND / "app" / "db" / "init.sql").read_text()
_MIGRATIONS = _BACKEND / "app" / "db" / "migrations"
_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",  # pragma: allowlist secret
)


def _load(filename: str):
    """Load a migration from source, not import_module — a stale .pyc would
    let a source regression pass."""
    path = _MIGRATIONS / filename
    spec = importlib.util.spec_from_file_location(f"mig_{filename[:3]}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _can_connect(dsn: str) -> bool:
    try:
        conn = await asyncpg.connect(dsn, timeout=2)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


@asynccontextmanager
async def _migrated_database(*, apply_107: bool = True):
    if not await _can_connect(_DSN):
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"REQUIRE_REAL_PG=1 but Postgres is not reachable at {_DSN}")
        pytest.skip(f"Postgres not reachable at {_DSN}")
    admin = await asyncpg.connect(_DSN)
    name = f"akb_native_asset_refs_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    base, _ = _DSN.rsplit("/", 1)
    conn = await asyncpg.connect(f"{base}/{name}")
    try:
        await conn.execute(_INIT_SQL)
        # `native_resources` is created by 048; 107's FK points at it.
        await _load("048_native_revision_core.py").migrate(conn)
        if apply_107:
            await _load("107_native_document_asset_refs.py").migrate(conn)
        # Every test's rows live in a transaction that is always rolled back.
        # 048 guards `native_resources` with a DEFERRABLE INITIALLY DEFERRED
        # trigger demanding a published Head, and publishing one means a valid
        # `native_revisions` row — which needs a payload manifest, an activity
        # event and an invalidation intent, four interlocking tables that 107
        # does not constrain. Staying inside an uncommitted transaction keeps
        # the fixture about this migration instead of about that ledger.
        tx = conn.transaction()
        await tx.start()
        try:
            yield conn
        finally:
            await tx.rollback()
    finally:
        await conn.close()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


async def _fixture(conn) -> dict:
    """One vault, one Git document, one Native document, one Native file, one asset."""
    vault_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO vaults (id, name, git_path) VALUES ($1, $2, $3)",
        vault_id, f"v{uuid.uuid4().hex[:8]}", "/tmp/vault",
    )
    git_doc = uuid.uuid4()
    await conn.execute(
        "INSERT INTO documents (id, vault_id, path, title) VALUES ($1, $2, $3, $4)",
        git_doc, vault_id, "git/doc.md", "git doc",
    )
    native_doc, native_file, other_doc = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    for resource_id, surface, path in (
        (native_doc, "document", "native/doc.md"),
        (other_doc, "document", "native/other.md"),
        (native_file, "file", "native/binary.bin"),
    ):
        await conn.execute(
            """
            INSERT INTO native_resources
                (resource_id, namespace_id, surface, content_profile, current_path)
            VALUES ($1, $2, $3, 'text', $4)
            """,
            resource_id, vault_id, surface, path,
        )
    asset_id, other_asset = uuid.uuid4(), uuid.uuid4()
    for aid in (asset_id, other_asset):
        await conn.execute(
            "INSERT INTO vault_files (id, vault_id, name, s3_key) VALUES ($1, $2, $3, $4)",
            aid, vault_id, "image.png", f"k/{aid}",
        )
    return {
        "vault_id": vault_id, "git_doc": git_doc, "native_doc": native_doc,
        "other_doc": other_doc, "native_file": native_file,
        "asset_id": asset_id, "other_asset": other_asset,
    }


async def _refs(conn, vault_id) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM document_asset_refs WHERE vault_id = $1", vault_id,
    )


async def test_migration_is_idempotent_and_keeps_both_arms_unique():
    async with _migrated_database() as conn:
        await _load("107_native_document_asset_refs.py").migrate(conn)
        indexes = {
            row["indexname"]
            for row in await conn.fetch(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'document_asset_refs'",
            )
        }
        assert {"uq_document_asset_refs_git", "uq_document_asset_refs_native"} <= indexes

        f = await _fixture(conn)
        await conn.execute(
            "INSERT INTO document_asset_refs (native_document_id, vault_id, asset_id)"
            " VALUES ($1, $2, $3)",
            f["native_doc"], f["vault_id"], f["asset_id"],
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            async with conn.transaction():          # savepoint
                await conn.execute(
                    "INSERT INTO document_asset_refs (native_document_id, vault_id, asset_id)"
                    " VALUES ($1, $2, $3)",
                    f["native_doc"], f["vault_id"], f["asset_id"],
                )


async def test_the_git_arm_still_inserts_and_still_cascades():
    async with _migrated_database() as conn:
        f = await _fixture(conn)
        await conn.execute(
            "INSERT INTO document_asset_refs (document_id, vault_id, asset_id)"
            " VALUES ($1, $2, $3)",
            f["git_doc"], f["vault_id"], f["asset_id"],
        )
        assert await _refs(conn, f["vault_id"]) == 1
        await conn.execute("DELETE FROM documents WHERE id = $1", f["git_doc"])
        assert await _refs(conn, f["vault_id"]) == 0


async def test_a_native_reference_may_name_a_document_but_never_a_file():
    async with _migrated_database() as conn:
        f = await _fixture(conn)
        await conn.execute(
            "INSERT INTO document_asset_refs (native_document_id, vault_id, asset_id)"
            " VALUES ($1, $2, $3)",
            f["native_doc"], f["vault_id"], f["asset_id"],
        )
        assert await _refs(conn, f["vault_id"]) == 1

        # The surface is part of the key, so a File resource cannot borrow the
        # reference that would make its bytes readable as an inline image.
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            async with conn.transaction():          # savepoint
                await conn.execute(
                    "INSERT INTO document_asset_refs (native_document_id, vault_id, asset_id)"
                    " VALUES ($1, $2, $3)",
                    f["native_file"], f["vault_id"], f["other_asset"],
                )


async def test_a_reference_names_exactly_one_arm():
    async with _migrated_database() as conn:
        f = await _fixture(conn)
        with pytest.raises(asyncpg.CheckViolationError):
            async with conn.transaction():          # savepoint
                await conn.execute(
                    "INSERT INTO document_asset_refs"
                    " (document_id, native_document_id, vault_id, asset_id)"
                    " VALUES ($1, $2, $3, $4)",
                    f["git_doc"], f["native_doc"], f["vault_id"], f["asset_id"],
                )
        with pytest.raises(asyncpg.CheckViolationError):
            async with conn.transaction():          # savepoint
                await conn.execute(
                    "INSERT INTO document_asset_refs (vault_id, asset_id) VALUES ($1, $2)",
                    f["vault_id"], f["asset_id"],
                )


async def test_retiring_a_native_document_drops_its_live_refs_and_no_others():
    """`delete_resource` sets lifecycle; the row stays, so no cascade fires."""
    async with _migrated_database() as conn:
        f = await _fixture(conn)
        for doc, asset in ((f["native_doc"], f["asset_id"]), (f["other_doc"], f["other_asset"])):
            await conn.execute(
                "INSERT INTO document_asset_refs (native_document_id, vault_id, asset_id)"
                " VALUES ($1, $2, $3)",
                doc, f["vault_id"], asset,
            )
        assert await _refs(conn, f["vault_id"]) == 2

        await conn.execute(
            "UPDATE native_resources SET lifecycle = 'deleted' WHERE resource_id = $1",
            f["native_doc"],
        )
        survivors = await conn.fetch(
            "SELECT native_document_id FROM document_asset_refs WHERE vault_id = $1",
            f["vault_id"],
        )
        assert [row["native_document_id"] for row in survivors] == [f["other_doc"]]


async def test_deleting_the_native_resource_row_cascades():
    async with _migrated_database() as conn:
        f = await _fixture(conn)
        await conn.execute(
            "INSERT INTO document_asset_refs (native_document_id, vault_id, asset_id)"
            " VALUES ($1, $2, $3)",
            f["native_doc"], f["vault_id"], f["asset_id"],
        )
        await conn.execute(
            "DELETE FROM native_resources WHERE resource_id = $1", f["native_doc"],
        )
        assert await _refs(conn, f["vault_id"]) == 0


async def test_publishing_the_same_reference_twice_is_a_no_op_for_either_arm():
    """The repository's own upsert, run against PostgreSQL rather than a fake.

    Each arm's uniqueness is a PARTIAL unique index, and `ON CONFLICT` refuses
    to match a partial index unless the statement repeats its predicate. The
    unit tests for this function drive a connection object that records SQL
    and never executes it, so the string looked correct while every insert
    failed against a real database — including the Git arm's, which this
    change was not supposed to touch.
    """
    from datetime import datetime, timedelta, timezone

    from app.repositories import vault_files_repo
    from app.repositories.vault_files_repo import DocumentAssetOwner

    async with _migrated_database() as conn:
        f = await _fixture(conn)
        retain = datetime.now(timezone.utc) + timedelta(days=1)
        owners = (
            DocumentAssetOwner(vault_id=f["vault_id"], document_id=f["git_doc"]),
            DocumentAssetOwner(vault_id=f["vault_id"], native_document_id=f["native_doc"]),
        )
        for owner in owners:
            for _ in range(2):  # the second call is what needs the arbiter
                await vault_files_repo.sync_document_asset_references(
                    conn,
                    owner=owner,
                    document_path="p/doc.md",
                    commit_hash="c" * 40,
                    asset_ids={f["asset_id"]},
                    retain_until=retain,
                )
        assert await _refs(conn, f["vault_id"]) == 2

        # Dropping the image removes that arm's reference and leaves the other.
        await vault_files_repo.sync_document_asset_references(
            conn,
            owner=owners[1],
            document_path="p/doc.md",
            commit_hash="d" * 40,
            asset_ids=set(),
            retain_until=retain,
        )
        rows = await conn.fetch(
            "SELECT document_id, native_document_id FROM document_asset_refs"
            " WHERE vault_id = $1",
            f["vault_id"],
        )
        assert [(r["document_id"], r["native_document_id"]) for r in rows] == [
            (f["git_doc"], None),
        ]


async def test_without_the_migration_a_native_reference_is_impossible():
    """The negative control: this is the state that broke every inline image."""
    async with _migrated_database(apply_107=False) as conn:
        columns = {
            row["column_name"]
            for row in await conn.fetch(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'document_asset_refs'",
            )
        }
        assert "native_document_id" not in columns
