"""Tests for CollectionService.create.

Mirrors the bootstrap pattern from `test_collection_repo.py`: hits a real
Postgres reachable via `AKB_TEST_DSN` (auto-skip otherwise), applies the
idempotent `init.sql`, and creates an ephemeral vault per test so the
table cascade cleans everything up.

`CollectionService` reaches into `app.db.postgres.get_pool()` for both
the repo wiring and the `emit_event` transaction. We monkeypatch that
function in the service module to hand back the test pool, so the
service code under test is exercised verbatim.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

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
    backend_dir = Path(__file__).resolve().parents[1]
    init_sql = (backend_dir / "app" / "db" / "init.sql").read_text()
    async with pool.acquire() as conn:
        await conn.execute(init_sql)
    # `events` lives in migration 015, not init.sql, but `emit_event`
    # is part of the service contract — apply it so the table exists.
    # The migration is idempotent (CREATE TABLE IF NOT EXISTS etc).
    import importlib.util
    mig_path = backend_dir / "app" / "db" / "migrations" / "015_events_outbox.py"
    spec = importlib.util.spec_from_file_location("mig_015", str(mig_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    async with pool.acquire() as conn:
        await module.migrate(conn=conn)
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def vault_id(pool):
    vault_repo = VaultRepository(pool)
    name = f"_test_collection_service_{uuid.uuid4().hex[:8]}"
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
            await conn.execute("DELETE FROM vaults WHERE id = $1", vid)


@pytest_asyncio.fixture
async def vault_name(pool, vault_id):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT name FROM vaults WHERE id = $1", vault_id)
    return row["name"]


@pytest_asyncio.fixture
async def service(pool, monkeypatch):
    """Wire the service module's `get_pool` to the test pool.

    The service calls `get_pool()` twice — once for `_repos`, once for the
    transactional `emit_event` block. Returning the same test pool from
    both calls is enough to keep the service code under test unchanged.
    """
    from app.services import collection_service as cs

    async def _fake_get_pool():
        return pool

    monkeypatch.setattr(cs, "get_pool", _fake_get_pool)
    return cs.CollectionService()


@pytest.mark.asyncio
async def test_create_normalizes_and_returns_created_true(
    service, vault_name, pool, vault_id
):
    result = await service.create(
        vault=vault_name,
        path="  /specs/  ",
        summary="design specs",
        agent_id="alice",
    )
    assert result["ok"] is True
    assert result["created"] is True
    assert result["collection"]["path"] == "specs"
    assert result["collection"]["name"] == "specs"
    assert result["collection"]["summary"] == "design specs"
    assert result["collection"]["doc_count"] == 0

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT path, name, summary FROM collections WHERE vault_id=$1 AND path=$2",
            vault_id, "specs",
        )
    assert row is not None
    assert row["path"] == "specs"
    assert row["name"] == "specs"
    assert row["summary"] == "design specs"


@pytest.mark.asyncio
async def test_create_idempotent(service, vault_name):
    first = await service.create(
        vault=vault_name, path="docs/api", summary="v1", agent_id=None,
    )
    second = await service.create(
        vault=vault_name, path="docs/api", summary="ignored", agent_id=None,
    )
    assert first["created"] is True
    assert second["created"] is False
    # Path should round-trip identically on the no-op call too.
    assert second["collection"]["path"] == "docs/api"
    assert second["collection"]["name"] == "api"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    ["", "   ", "/", "../etc", "a/../b", "a/./b", "x\x00y"],
)
async def test_create_rejects_invalid_path(service, vault_name, bad):
    from app.services.collection_service import InvalidPathError

    with pytest.raises(InvalidPathError):
        await service.create(
            vault=vault_name, path=bad, summary=None, agent_id=None,
        )


@pytest.mark.asyncio
async def test_create_emits_event(service, vault_name, pool, vault_id):
    await service.create(
        vault=vault_name, path="events/probe", summary=None, agent_id="bob",
    )
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT kind, ref_type, ref_id, actor_id, payload
              FROM events
             WHERE vault_id = $1 AND kind = 'collection.create'
                   AND ref_id = $2
             ORDER BY id DESC
             LIMIT 1
            """,
            vault_id, "events/probe",
        )
    assert row is not None
    assert row["kind"] == "collection.create"
    assert row["ref_type"] == "collection"
    assert row["ref_id"] == "events/probe"
    assert row["actor_id"] == "bob"


@pytest.mark.asyncio
async def test_create_unknown_vault_raises_not_found(service):
    from app.exceptions import NotFoundError

    missing = f"_no_such_vault_{uuid.uuid4().hex[:8]}"
    with pytest.raises(NotFoundError):
        await service.create(
            vault=missing, path="x", summary=None, agent_id=None,
        )
