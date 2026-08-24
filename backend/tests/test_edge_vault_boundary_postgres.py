"""Live PostgreSQL backstop for the one-vault edge invariant."""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest


pytestmark = pytest.mark.asyncio
_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",  # pragma: allowlist secret
)


@pytest.fixture
async def connection():
    try:
        conn = await asyncpg.connect(_DSN, timeout=2.0)
    except (OSError, asyncpg.PostgresError):
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"REQUIRE_REAL_PG=1 but Postgres is not reachable at {_DSN}")
        pytest.skip(f"Postgres not reachable at {_DSN}")

    from app.db.postgres import _load_migration

    migration = _load_migration("083_edge_vault_boundary.py")
    assert migration is not None
    await migration.migrate(conn)

    names = [
        f"edge-boundary-a-{uuid.uuid4().hex[:10]}",
        f"edge-boundary-b-{uuid.uuid4().hex[:10]}",
    ]
    ids = [
        await conn.fetchval(
            "INSERT INTO vaults(name, git_path) VALUES($1, $2) RETURNING id",
            name,
            f"/tmp/{name}.git",
        )
        for name in names
    ]
    try:
        yield conn, names, ids
    finally:
        await conn.execute("DELETE FROM vaults WHERE id = ANY($1::uuid[])", ids)
        await conn.close()


async def _insert_edge(conn, vault_id, source: str, target: str) -> uuid.UUID:
    return await conn.fetchval(
        """
        INSERT INTO edges(
            vault_id, source_uri, target_uri, relation_type,
            source_type, target_type, kind
        )
        VALUES($1, $2, $3, 'related_to', 'doc', 'doc', 'explicit')
        RETURNING id
        """,
        vault_id,
        source,
        target,
    )


async def test_trigger_accepts_same_vault_and_rejects_cross_vault(connection):
    conn, names, ids = connection
    first, second = names
    valid_source = f"akb://{first}/doc/a.md"
    valid_target = f"akb://{first}/doc/b.md"

    edge_id = await _insert_edge(conn, ids[0], valid_source, valid_target)
    assert edge_id is not None

    with pytest.raises(asyncpg.CheckViolationError, match="owning vault"):
        await _insert_edge(
            conn,
            ids[0],
            valid_source,
            f"akb://{second}/doc/private.md",
        )


async def test_trigger_rejects_owner_mismatch_and_valid_to_invalid_update(connection):
    conn, names, ids = connection
    first, second = names
    source = f"akb://{first}/doc/a.md"
    target = f"akb://{first}/doc/b.md"

    with pytest.raises(asyncpg.CheckViolationError, match="owning vault"):
        await _insert_edge(conn, ids[1], source, target)

    edge_id = await _insert_edge(conn, ids[0], source, target)
    with pytest.raises(asyncpg.CheckViolationError, match="owning vault"):
        await conn.execute(
            "UPDATE edges SET target_uri = $2 WHERE id = $1",
            edge_id,
            f"akb://{second}/doc/private.md",
        )
