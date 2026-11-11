"""Live-PostgreSQL check of the Native `created_by` display-name lookup.

The unit test pins which query runs for which token; this one asks PostgreSQL
what it does with them. Every statement the lookup sends must be answerable
from an index of the real `users` table, and both token forms, plus a username
that looks like a UUID, must still resolve against real rows.

The predicate it replaced, `id::text = $1 OR username = $1`, is planned here
too: it stays a sequential scan even with sequential scans disabled, which is
what makes the index assertion mean something.

Runs in a disposable database. Registered in the `pgvector e2e (live DB)` CI
job, which sets `AKB_TEST_DSN`.
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import pytest

from app.services.native_document_service import NativeDocumentService

pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[1]
_INIT_SQL = (_BACKEND / "app" / "db" / "init.sql").read_text()
_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",  # pragma: allowlist secret
)


async def _can_connect(dsn: str) -> bool:
    try:
        conn = await asyncpg.connect(dsn, timeout=2)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


@asynccontextmanager
async def _users_database():
    if not await _can_connect(_DSN):
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"REQUIRE_REAL_PG=1 but Postgres is not reachable at {_DSN}")
        pytest.skip(f"Postgres not reachable at {_DSN}")
    admin = await asyncpg.connect(_DSN)
    name = f"akb_created_by_name_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    base, _ = _DSN.rsplit("/", 1)
    dsn = f"{base}/{name}"
    try:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(_INIT_SQL)
        finally:
            await conn.close()
        yield dsn
    finally:
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


class _RecordingPool:
    """The real pool, keeping every statement the lookup sends."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self.statements: list[tuple[str, object]] = []

    @asynccontextmanager
    async def acquire(self):
        async with self._pool.acquire() as conn:
            pool = self

            class _Conn:
                async def fetchval(self, query, value):
                    pool.statements.append((query, value))
                    return await conn.fetchval(query, value)

            yield _Conn()


def _plan_nodes(node: dict):
    yield node["Node Type"], node.get("Index Name")
    for child in node.get("Plans", []):
        yield from _plan_nodes(child)


async def _plan(conn, query: str, value: object) -> list[tuple[str, str | None]]:
    raw = await conn.fetchval("EXPLAIN (FORMAT JSON) " + query, value)
    plan = json.loads(raw) if isinstance(raw, str) else raw
    return list(_plan_nodes(plan[0]["Plan"]))


async def test_both_token_forms_resolve_through_indexes():
    owner_id = uuid.uuid4()
    lookalike = str(uuid.uuid4())
    async with _users_database() as dsn:
        setup = await asyncpg.connect(dsn)
        try:
            await setup.executemany(
                """
                INSERT INTO users (id, username, email, password_hash, display_name)
                VALUES ($1, $2, $3, 'unusable', $4)
                """,
                [
                    (owner_id, "owner", "owner@example.test", "Owner"),
                    (uuid.uuid4(), lookalike, "bot@example.test", None),
                ],
            )
        finally:
            await setup.close()

        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
        try:
            recording = _RecordingPool(pool)
            service = NativeDocumentService(pool=recording)

            assert await service._created_by_name(str(owner_id)) == "Owner"
            assert await service._created_by_name("owner") == "Owner"
            # No user has this id, so the username lookup answers.
            assert await service._created_by_name(lookalike) == lookalike
            assert await service._created_by_name("external-author") is None
            assert await service._created_by_name(str(uuid.uuid4())) is None

            async with pool.acquire() as conn:
                await conn.execute("SET enable_seqscan = off")
                for query, value in recording.statements:
                    nodes = await _plan(conn, query, value)
                    assert ("Seq Scan", None) not in nodes, (query, nodes)
                    assert {index for _, index in nodes} & {"users_pkey", "users_username_key"}, (query, nodes)

                # The replaced predicate, for contrast: no index can serve it.
                replaced = await _plan(
                    conn,
                    "SELECT COALESCE(display_name, username) FROM users "
                    "WHERE id::text = $1 OR username = $1 LIMIT 1",
                    "owner",
                )
                assert ("Seq Scan", None) in replaced
        finally:
            await pool.close()
