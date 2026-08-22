"""Real-PostgreSQL regressions for literal document suffix lookup."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from app.services.kg_service import _resolve_doc_ref


pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[1]
_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",  # pragma: allowlist secret
)


async def _can_connect() -> bool:
    try:
        conn = await asyncpg.connect(_DSN, timeout=2.0)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


@pytest_asyncio.fixture
async def pool():
    if not await _can_connect():
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"REQUIRE_REAL_PG=1 but Postgres is not reachable at {_DSN}")
        pytest.skip(f"Postgres not reachable at {_DSN}")

    pool = await asyncpg.create_pool(dsn=_DSN, min_size=1, max_size=4)
    async with pool.acquire() as conn:
        await conn.execute((_BACKEND / "app" / "db" / "init.sql").read_text())
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def vault_id(pool):
    name = f"_test_kg_document_ref_{uuid.uuid4().hex[:8]}"
    async with pool.acquire() as conn:
        vault_id = await conn.fetchval(
            "INSERT INTO vaults (name, git_path) VALUES ($1, $2) RETURNING id",
            name,
            f"/tmp/{name}.git",
        )
    try:
        yield vault_id
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM vaults WHERE id = $1", vault_id)


async def _insert_document(conn, vault_id: uuid.UUID, path: str) -> uuid.UUID:
    document_id = uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO documents (id, vault_id, path, title)
        VALUES ($1, $2, $3, $4)
        """,
        document_id,
        vault_id,
        path,
        path,
    )
    return document_id


@pytest.mark.parametrize(
    ("target_path", "ref", "near_path"),
    [
        pytest.param(
            "literal/trailing" + "\\",
            "trailing" + "\\",
            "literal/trailingx",
            id="trailing-backslash",
        ),
        pytest.param(
            "literal/100%",
            "100%",
            "literal/100x",
            id="percent",
        ),
        pytest.param(
            "literal/under_score",
            "under_score",
            "literal/underXscore",
            id="underscore",
        ),
    ],
)
async def test_non_uri_suffix_reference_is_literal(
    pool,
    vault_id: uuid.UUID,
    target_path: str,
    ref: str,
    near_path: str,
):
    async with pool.acquire() as conn:
        # Insert the wildcard near-miss first so an unescaped LIKE pattern
        # would return the wrong row instead of the requested document.
        near_id = await _insert_document(conn, vault_id, near_path)
        target_id = await _insert_document(conn, vault_id, target_path)

        resolved = await _resolve_doc_ref(conn, vault_id, ref)

    assert resolved == target_id
    assert resolved != near_id


async def test_suffix_reference_keeps_exact_suffix_and_near_miss_behavior(
    pool,
    vault_id: uuid.UUID,
):
    async with pool.acquire() as conn:
        near_id = await _insert_document(conn, vault_id, "nested/funapi.md")
        exact_id = await _insert_document(conn, vault_id, "nested/api.md")

        assert await _resolve_doc_ref(conn, vault_id, "api.md") == exact_id
        assert await _resolve_doc_ref(conn, vault_id, "api.md") != near_id
        assert await _resolve_doc_ref(conn, vault_id, "pi.md") is None


async def test_exact_path_precedes_suffix_fallback(pool, vault_id: uuid.UUID):
    async with pool.acquire() as conn:
        exact_id = await _insert_document(conn, vault_id, "api.md")
        await _insert_document(conn, vault_id, "nested/api.md")

        assert await _resolve_doc_ref(conn, vault_id, "api.md") == exact_id
