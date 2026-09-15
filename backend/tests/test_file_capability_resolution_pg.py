"""The write-capability resolver, run against a real Postgres.

Every other test of this resolver hands it a canned row from a fake
connection, so the SQL is only ever checked by grepping its text. That is
enough to assert a substring and not enough to assert a meaning: a review
found that turning the `JOIN` into a `LEFT JOIN`, or moving the expiry
predicate a century into the past, left all of those tests green while the
resolver happily answered for a deleted File and an expired grant.

So these run the real statement against the real schema.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from app.exceptions import NotFoundError
from app.repositories.vault_repo import VaultRepository
from app.services import file_service as fs

_DSN = os.getenv("AKB_TEST_DSN", "postgresql://akb:akb@localhost:15432/akb")
_KEY = "team/deadbeef_report.bin"

# init.sql plus the migrations this contract needs: the transfer-intent table
# and the column naming the key a capability grants.
_MIGRATIONS = (
    "015_events_outbox.py",
    "019_s3_delete_outbox.py",
    "055_native_revision_m1_file_storage.py",
    "067_vault_file_upload_state.py",
    "104_file_write_capability_key.py",
)


async def _can_connect(dsn: str) -> bool:
    try:
        conn = await asyncpg.connect(dsn, timeout=2)
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
    async with pool.acquire() as conn:
        await conn.execute((backend_dir / "app" / "db" / "init.sql").read_text())
    import importlib.util
    for name in _MIGRATIONS:
        path = backend_dir / "app" / "db" / "migrations" / name
        spec = importlib.util.spec_from_file_location(name, str(path))
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
    name = f"_test_capability_{uuid.uuid4().hex[:8]}"
    vid = await VaultRepository(pool).create(
        name=name, description="ephemeral test vault", git_path=f"/tmp/{name}",
    )
    yield vid
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM vaults WHERE id = $1", vid)


@pytest_asyncio.fixture
async def service(pool, monkeypatch):
    async def _pool():
        return pool

    monkeypatch.setattr(fs, "get_pool", _pool)
    monkeypatch.setattr(fs, "measurement_enabled", lambda: False)
    return fs.FileService()


async def _grant(
    pool, vault_id, *, file_id=None, object_key=_KEY, method="PUT",
    ttl=3600, with_file_row=True, upload_state="pending",
) -> str:
    fid = file_id or uuid.uuid4()
    token = fs._new_capability_token()
    async with pool.acquire() as conn:
        if with_file_row:
            await conn.execute(
                """
                INSERT INTO vault_files
                    (id, vault_id, kind, upload_state, name, s3_key,
                     mime_type, size_bytes, created_by)
                VALUES ($1, $2, 'file', $3, 'report.bin', $4,
                        'application/pdf', 0, 'tester')
                """,
                fid, vault_id, upload_state, object_key or _KEY,
            )
        # The table's own CHECK splits the two lanes: a GET intent carries no
        # filename and a PUT intent must carry filename, mime_type and actor.
        await conn.execute(
            """
            INSERT INTO m1_file_transfer_intents (
                id, file_id, vault_id, method, filename, mime_type, actor_id,
                object_key, token_digest, expires_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9,
                      NOW() + ($10 * INTERVAL '1 second'))
            """,
            uuid.uuid4(), fid, vault_id, method,
            None if method == "GET" else "report.bin",
            None if method == "GET" else "application/pdf",
            None if method == "GET" else "tester",
            object_key, fs._capability_digest(token), ttl,
        )
    return token


async def test_a_live_grant_resolves_to_its_key(service, pool, vault_id):
    token = await _grant(pool, vault_id)
    resolved = await service.resolve_write_capability(token)
    assert resolved["object_key"] == _KEY
    assert resolved["mime_type"] == "application/pdf"


async def test_a_grant_whose_file_was_deleted_stops_working(service, pool, vault_id):
    """The join is the whole point: a presigned URL kept writing after its
    File was deleted, leaving bytes under a key nothing referenced."""
    fid = uuid.uuid4()
    token = await _grant(pool, vault_id, file_id=fid)
    assert await service.resolve_write_capability(token)

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM vault_files WHERE id = $1", fid)

    with pytest.raises(NotFoundError):
        await service.resolve_write_capability(token)


async def test_an_expired_grant_stops_working(service, pool, vault_id):
    token = await _grant(pool, vault_id, ttl=-1)
    with pytest.raises(NotFoundError):
        await service.resolve_write_capability(token)


async def test_a_grant_without_an_object_key_is_refused(service, pool, vault_id):
    """A measurement-lane PUT intent carries its bytes in the database and
    addresses no object store."""
    token = await _grant(pool, vault_id, object_key=None)
    with pytest.raises(NotFoundError):
        await service.resolve_write_capability(token)


async def test_a_download_grant_is_not_an_upload_grant(service, pool, vault_id):
    """`method` is what separates the two lanes, and it separates them in the
    statement rather than in the caller."""
    token = await _grant(pool, vault_id, method="GET", object_key=None)
    with pytest.raises(NotFoundError):
        await service.resolve_write_capability(token)


async def test_a_grant_in_another_vault_does_not_resolve(service, pool, vault_id):
    """The join is on (id, vault_id), not on id alone: a File row that exists
    under a different vault must not satisfy it."""
    other_name = f"_test_capability_other_{uuid.uuid4().hex[:8]}"
    other = await VaultRepository(pool).create(
        name=other_name, description="ephemeral test vault",
        git_path=f"/tmp/{other_name}",
    )
    try:
        fid = uuid.uuid4()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO vault_files
                    (id, vault_id, kind, upload_state, name, s3_key,
                     mime_type, size_bytes, created_by)
                VALUES ($1, $2, 'file', 'pending', 'report.bin', $3,
                        'application/pdf', 0, 'tester')
                """,
                fid, other, _KEY,
            )
            token = fs._new_capability_token()
            await conn.execute(
                """
                INSERT INTO m1_file_transfer_intents (
                    id, file_id, vault_id, method, filename, mime_type,
                    actor_id, object_key, token_digest, expires_at
                ) VALUES ($1, $2, $3, 'PUT', 'report.bin', 'application/pdf',
                          'tester', $4, $5, NOW() + INTERVAL '1 hour')
                """,
                uuid.uuid4(), fid, vault_id, _KEY,
                fs._capability_digest(token),
            )
        with pytest.raises(NotFoundError):
            await service.resolve_write_capability(token)
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM vaults WHERE id = $1", other)
