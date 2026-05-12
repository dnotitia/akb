"""Unit-ish tests for CollectionRepository helpers.

These talk to a real Postgres (no mocks); pgvector isn't required because
CollectionRepository only touches `collections`, `documents`, and
`vault_files`. The test bootstraps schema from `backend/app/db/init.sql`
and tears down each test's ephemeral vault.

DSN comes from `AKB_TEST_DSN` (default
`postgresql://akb:akb@localhost:15432/akb` to match the dev override used
during task 1 work). Skip the module if the DB isn't reachable so
running the full test suite without a local Postgres still works.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from app.repositories.document_repo import CollectionRepository
from app.repositories.vault_repo import VaultRepository

_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",
)


async def _can_connect(dsn: str) -> bool:
    try:
        conn = await asyncpg.connect(dsn, timeout=2.0)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


@pytest_asyncio.fixture
async def pool():
    if not await _can_connect(_DSN):
        pytest.skip(f"Postgres not reachable at {_DSN}")
    pool = await asyncpg.create_pool(dsn=_DSN, min_size=1, max_size=4)
    # Apply schema. init.sql is idempotent (uses CREATE TABLE IF NOT
    # EXISTS), so it's safe to re-run against an existing DB.
    init_sql = (
        Path(__file__).resolve().parents[1] / "app" / "db" / "init.sql"
    ).read_text()
    async with pool.acquire() as conn:
        await conn.execute(init_sql)
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def vault_id(pool):
    """Ephemeral vault per test; cascades clean up collections + docs."""
    vault_repo = VaultRepository(pool)
    name = f"_test_collection_repo_{uuid.uuid4().hex[:8]}"
    vid = await vault_repo.create(
        name=name,
        description="ephemeral test vault",
        git_path=f"/tmp/{name}.git",
        owner_id=None,
    )
    try:
        yield vid
    finally:
        async with pool.acquire() as conn:
            # documents has no ON DELETE CASCADE from vaults? Actually it
            # does (init.sql line 93). vault_files too. So a single
            # DELETE clears the tree.
            await conn.execute("DELETE FROM vaults WHERE id = $1", vid)


@pytest.mark.asyncio
async def test_create_empty_inserts_new_row(pool, vault_id):
    repo = CollectionRepository(pool)
    cid, created = await repo.create_empty(vault_id, "alpha")
    assert isinstance(cid, uuid.UUID)
    assert created is True

    rows = await repo.list_by_vault(vault_id)
    paths = {r["path"] for r in rows}
    assert "alpha" in paths


@pytest.mark.asyncio
async def test_create_empty_is_idempotent(pool, vault_id):
    repo = CollectionRepository(pool)
    cid1, created1 = await repo.create_empty(vault_id, "beta", summary="first")
    cid2, created2 = await repo.create_empty(vault_id, "beta", summary="ignored")
    assert created1 is True
    assert created2 is False
    assert cid1 == cid2


@pytest.mark.asyncio
async def test_delete_by_id_removes_row(pool, vault_id):
    repo = CollectionRepository(pool)
    cid, _ = await repo.create_empty(vault_id, "gamma")
    await repo.delete_by_id(cid)
    rows = await repo.list_by_vault(vault_id)
    assert "gamma" not in {r["path"] for r in rows}


@pytest.mark.asyncio
async def test_list_docs_under_returns_only_prefix_matches(pool, vault_id):
    """`list_docs_under(vault_id, "a")` must return docs whose path
    starts with `a/`, never sibling prefixes like `ab/` or `_a/`."""
    repo = CollectionRepository(pool)
    # Seed the collection rows so docs can be inserted with collection_id
    # NULL (allowed). We don't need real collections to test path prefix
    # matching, just three docs.
    async with pool.acquire() as conn:
        for path, title in [
            ("a/x.md", "x"),
            ("a/y.md", "y"),
            ("b/z.md", "z"),
            # Two extra path shapes that must NOT match `a`:
            ("ab/other.md", "other"),
            ("_a/leading.md", "leading"),
        ]:
            await conn.execute(
                """
                INSERT INTO documents (id, vault_id, path, title)
                VALUES ($1, $2, $3, $4)
                """,
                uuid.uuid4(), vault_id, path, title,
            )

    rows = await repo.list_docs_under(vault_id, "a")
    paths = sorted(r["path"] for r in rows)
    assert paths == ["a/x.md", "a/y.md"]


@pytest.mark.asyncio
async def test_list_docs_under_escapes_like_metachars(pool, vault_id):
    """`%` and `_` in the user-supplied path must not widen the LIKE
    match. A folder literally named `a_b` should only match `a_b/...`,
    not `aXb/...`."""
    repo = CollectionRepository(pool)
    async with pool.acquire() as conn:
        for path in ("a_b/in.md", "aXb/not-a-match.md"):
            await conn.execute(
                """
                INSERT INTO documents (id, vault_id, path, title)
                VALUES ($1, $2, $3, 't')
                """,
                uuid.uuid4(), vault_id, path,
            )

    rows = await repo.list_docs_under(vault_id, "a_b")
    paths = sorted(r["path"] for r in rows)
    assert paths == ["a_b/in.md"]


@pytest.mark.asyncio
async def test_list_files_under_includes_folder_and_descendants(pool, vault_id):
    """`list_files_under(vault_id, "media")` should return files whose
    `collection` is exactly `media` OR starts with `media/`."""
    repo = CollectionRepository(pool)
    async with pool.acquire() as conn:
        for coll, name, key in [
            ("media",        "logo.png",    "k1"),
            ("media/sub",    "deep.png",    "k2"),
            ("media-extra",  "decoy.png",   "k3"),
            ("other",        "other.png",   "k4"),
        ]:
            await conn.execute(
                """
                INSERT INTO vault_files
                    (id, vault_id, collection, name, s3_key)
                VALUES ($1, $2, $3, $4, $5)
                """,
                uuid.uuid4(), vault_id, coll, name, key,
            )

    rows = await repo.list_files_under(vault_id, "media")
    names = sorted(r["name"] for r in rows)
    assert names == ["deep.png", "logo.png"]
