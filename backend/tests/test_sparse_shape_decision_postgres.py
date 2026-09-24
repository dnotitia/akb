"""`vector_store_sparse_shape: auto` decided against real servers.

A new database gets `vchord` where the server can give the `vchord_bm25`
extension to AKB's role, and `posting` where it cannot. A database that already
serves a shape keeps it, and the first successful schema setup records the
shape, so the answer never depends on the order in which things happen to be
looked at again.

The case this exists for is an upgrade. The code default used to be `posting`,
and the settings of most installations never name a shape. Deciding `vchord`
for such a database would hit the populated-table guard in `_do_ensure`, and
every search, dense included, would come back empty while readiness stayed
green.

Two servers, because the decision turns on what the server provides:
`AKB_VCHORD_TEST_DSN` provides `vchord_bm25`, and `AKB_TEST_DSN`, the stock
pgvector image, does not. Every test creates its own database and drops it.
Where a server is not configured the test skips, and says why.
"""

from __future__ import annotations

import contextlib
import os
import uuid

import asyncpg
import pytest

from app.services.vector_store.pgvector import PgvectorStore
from app.services.vector_store.sparse_shape_state import (
    SparseShapeUndecidable,
    decide_sparse_shape,
    recorded_sparse_shape,
)

pytestmark = pytest.mark.asyncio

_VCHORD = os.environ.get("AKB_VCHORD_TEST_DSN", "")
_PLAIN = os.environ.get("AKB_TEST_DSN", "")
_SCHEMA = "vector_index"


async def _connect_or_skip(server: str) -> asyncpg.Connection:
    """The repository's convention: unreachable skips, unless REQUIRE_REAL_PG=1."""
    if not server:
        pytest.skip("이 서버의 DSN 이 없다 — AKB_VCHORD_TEST_DSN/AKB_TEST_DSN 을 설정할 것")
    try:
        return await asyncpg.connect(server, timeout=5.0)
    except (OSError, asyncpg.PostgresError):
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail("REQUIRE_REAL_PG=1 but Postgres is not reachable at the configured DSN")
        pytest.skip("Postgres unreachable at the configured DSN")


def _dsn(server: str, database: str, *, user: str | None = None, password: str | None = None) -> str:
    base, _ = server.rsplit("/", 1)
    if user is not None:
        scheme, rest = base.split("://", 1)
        host = rest.split("@", 1)[1]
        base = f"{scheme}://{user}:{password}@{host}"
    return f"{base}/{database}"


@contextlib.asynccontextmanager
async def _database(
    server: str, *, plain_role: bool = False, precreate: tuple[str, ...] = (), grant_usage: bool = False
):
    """A fresh database; with `plain_role`, a connection as a non-superuser owner.

    `precreate` names extensions a superuser creates before AKB's role arrives,
    which is how a DBA provides one the role cannot create itself, and
    `grant_usage` is the GRANT that makes `vchord_bm25` usable by that role."""
    admin = await _connect_or_skip(server)
    name = f"akb_shape_{uuid.uuid4().hex[:12]}"
    role = f"akb_shape_role_{uuid.uuid4().hex[:8]}"
    password = uuid.uuid4().hex
    await admin.execute(f'CREATE DATABASE "{name}"')
    try:
        if plain_role:
            await admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{password}' NOSUPERUSER")
            await admin.execute(f'ALTER DATABASE "{name}" OWNER TO "{role}"')
        setup = await asyncpg.connect(_dsn(server, name))
        try:
            # `vector` first, as the product's entrypoints do: AKB needs it in
            # every shape, and a plain role cannot create it everywhere either.
            for extension in ("vector", *precreate) if plain_role else precreate:
                await setup.execute(f"CREATE EXTENSION IF NOT EXISTS {extension}")
            if grant_usage:
                await setup.execute(f'GRANT USAGE ON SCHEMA bm25_catalog TO "{role}"')
        finally:
            await setup.close()
        dsn = _dsn(server, name, user=role, password=password) if plain_role else _dsn(server, name)
        conn = await asyncpg.connect(dsn)
        try:
            yield conn
        finally:
            await conn.close()
    finally:
        with contextlib.suppress(asyncpg.PostgresError):
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        if plain_role:
            with contextlib.suppress(asyncpg.PostgresError):
                await admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        await admin.close()


async def _ensure(conn, shape: str) -> None:
    """Schema setup as startup runs it, for one shape."""
    async with conn.transaction():
        await PgvectorStore(dsn=None, schema=_SCHEMA, dense_dim=4, sparse_shape=shape)._do_ensure(conn)


async def _forget_the_record(conn) -> None:
    """What a database set up by a version that did not record looks like."""
    await conn.execute(f'DROP TABLE IF EXISTS "{_SCHEMA}".install_state')


async def _put_chunk(conn) -> None:
    chunk = uuid.uuid4()
    await conn.execute(
        f"""
        INSERT INTO "{_SCHEMA}".chunks (chunk_id, source_type, source_id, vault_id, section_path, content, chunk_index)
        VALUES ($1, 'document', $1, $2, '', 'already indexed', 0)
        """,
        chunk, uuid.uuid4(),
    )


async def test_a_new_database_on_a_server_with_the_extension_gets_vchord_and_records_it():
    async with _database(_VCHORD) as conn:
        decision = await decide_sparse_shape(conn, schema=_SCHEMA, configured="auto")
        assert (decision.shape, decision.decided_by, decision.posting_table_present) == (
            "vchord", "new_database", False,
        )
        assert await recorded_sparse_shape(conn, schema=_SCHEMA) is None

        await _ensure(conn, decision.shape)
        assert await recorded_sparse_shape(conn, schema=_SCHEMA) == "vchord"
        assert await conn.fetchval("SELECT to_regclass('vector_index.idx_vi_chunks_bm25') IS NOT NULL")
        again = await decide_sparse_shape(conn, schema=_SCHEMA, configured="auto")
        assert (again.shape, again.decided_by) == ("vchord", "recorded")


async def test_a_new_database_on_a_server_without_the_extension_gets_posting():
    async with _database(_PLAIN) as conn:
        decision = await decide_sparse_shape(conn, schema=_SCHEMA, configured="auto")
        assert (decision.shape, decision.decided_by) == ("posting", "new_database")
        assert "vchord_bm25" in decision.note

        await _ensure(conn, decision.shape)
        assert await recorded_sparse_shape(conn, schema=_SCHEMA) == "posting"


async def test_a_role_that_cannot_create_the_extension_gets_posting_and_is_told_how():
    """The extension's control file says `superuser = true`."""
    async with _database(_VCHORD, plain_role=True) as conn:
        decision = await decide_sparse_shape(conn, schema=_SCHEMA, configured="auto")
        assert (decision.shape, decision.decided_by) == ("posting", "new_database")
        assert "superuser" in decision.note and "CREATE EXTENSION vchord_bm25" in decision.note


async def test_an_extension_a_superuser_provided_serves_a_plain_role():
    async with _database(_VCHORD, plain_role=True, precreate=("vchord_bm25",), grant_usage=True) as conn:
        decision = await decide_sparse_shape(conn, schema=_SCHEMA, configured="auto")
        assert (decision.shape, decision.decided_by) == ("vchord", "new_database")
        # And the role can then set the shape up itself.
        await _ensure(conn, decision.shape)
        assert await recorded_sparse_shape(conn, schema=_SCHEMA) == "vchord"


async def test_an_extension_the_role_cannot_use_is_refused_with_the_grant():
    """Created is not usable. Deciding `vchord` here failed the schema setup with
    "permission denied for schema bm25_catalog"; recording `posting` instead would
    quietly undo what whoever created the extension meant."""
    async with _database(_VCHORD, plain_role=True, precreate=("vchord_bm25",)) as conn:
        with pytest.raises(SparseShapeUndecidable, match="GRANT USAGE ON SCHEMA bm25_catalog"):
            await decide_sparse_shape(conn, schema=_SCHEMA, configured="auto")


async def test_an_installation_that_serves_posting_keeps_it():
    """The upgrade: no record, a populated posting schema, a server that could do vchord."""
    async with _database(_VCHORD) as conn:
        await _ensure(conn, "posting")
        await _put_chunk(conn)
        await _forget_the_record(conn)
        decision = await decide_sparse_shape(conn, schema=_SCHEMA, configured="auto")
        assert (decision.shape, decision.decided_by, decision.posting_table_present) == (
            "posting", "existing_posting", True,
        )


async def test_an_installation_that_serves_vchord_keeps_it():
    async with _database(_VCHORD) as conn:
        await _ensure(conn, "vchord")
        await _put_chunk(conn)
        await _forget_the_record(conn)
        decision = await decide_sparse_shape(conn, schema=_SCHEMA, configured="auto")
        assert (decision.shape, decision.decided_by, decision.posting_table_present) == (
            "vchord", "existing_bm25_index", False,
        )


async def test_a_migration_in_progress_stays_on_posting_until_switched_explicitly():
    """Both tables exist while the backfill runbook runs; the index is not the signal."""
    async with _database(_VCHORD) as conn:
        await _ensure(conn, "posting")
        await _put_chunk(conn)
        await _forget_the_record(conn)
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vchord_bm25")
        await conn.execute(
            "ALTER TABLE vector_index.chunks ADD COLUMN IF NOT EXISTS sparse_bm25 bm25_catalog.bm25vector"
        )
        await conn.execute(
            "CREATE INDEX idx_vi_chunks_bm25 ON vector_index.chunks USING bm25 (sparse_bm25 bm25_catalog.bm25_ops)"
        )
        decision = await decide_sparse_shape(conn, schema=_SCHEMA, configured="auto")
        assert (decision.shape, decision.decided_by, decision.posting_table_present) == (
            "posting", "existing_posting", True,
        )


async def test_a_later_auto_follows_the_last_configured_shape():
    """Switching to `vchord` by setting records it; going back to `auto` keeps it."""
    async with _database(_VCHORD) as conn:
        await _ensure(conn, "posting")
        # The runbook's end state, then the explicit switch.
        await _ensure(conn, "vchord")
        assert await recorded_sparse_shape(conn, schema=_SCHEMA) == "vchord"
        decision = await decide_sparse_shape(conn, schema=_SCHEMA, configured="auto")
        assert (decision.shape, decision.decided_by, decision.posting_table_present) == (
            "vchord", "recorded", True,
        )


async def test_an_arrays_installation_keeps_it():
    async with _database(_PLAIN) as conn:
        await _ensure(conn, "arrays")
        await _forget_the_record(conn)
        decision = await decide_sparse_shape(conn, schema=_SCHEMA, configured="auto")
        assert (decision.shape, decision.decided_by) == ("arrays", "existing_arrays")


async def test_populated_chunks_with_no_known_shape_are_refused_by_name():
    async with _database(_PLAIN) as conn:
        await conn.execute(f'CREATE SCHEMA "{_SCHEMA}"')
        await conn.execute(
            f"""
            CREATE TABLE "{_SCHEMA}".chunks (
                chunk_id UUID PRIMARY KEY, source_type TEXT NOT NULL, source_id UUID NOT NULL,
                vault_id UUID, section_path TEXT, content TEXT NOT NULL, chunk_index INTEGER NOT NULL
            )
            """
        )
        await _put_chunk(conn)
        with pytest.raises(SparseShapeUndecidable, match="vector_store_sparse_shape"):
            await decide_sparse_shape(conn, schema=_SCHEMA, configured="auto")


async def test_an_empty_table_left_by_an_earlier_start_is_still_new():
    async with _database(_VCHORD) as conn:
        await conn.execute(f'CREATE SCHEMA "{_SCHEMA}"')
        await conn.execute(
            f"""
            CREATE TABLE "{_SCHEMA}".chunks (
                chunk_id UUID PRIMARY KEY, source_type TEXT NOT NULL, source_id UUID NOT NULL,
                vault_id UUID, section_path TEXT, content TEXT NOT NULL, chunk_index INTEGER NOT NULL
            )
            """
        )
        decision = await decide_sparse_shape(conn, schema=_SCHEMA, configured="auto")
        assert (decision.shape, decision.decided_by) == ("vchord", "new_database")


async def test_a_configured_shape_is_reported_as_configured_and_recorded():
    async with _database(_VCHORD) as conn:
        decision = await decide_sparse_shape(conn, schema=_SCHEMA, configured="posting")
        assert (decision.shape, decision.decided_by) == ("posting", "configured")
        await _ensure(conn, decision.shape)
        assert await recorded_sparse_shape(conn, schema=_SCHEMA) == "posting"


async def test_the_settings_glue_decides_through_a_separate_dsn():
    """`vector_store_dsn` names the vector database; the decision reads that one."""
    from app.config import Settings
    from app.services.vector_store.factory import decide_sparse_shape_for_settings

    admin = await _connect_or_skip(_VCHORD)
    name = f"akb_shape_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    try:
        configured = Settings(document_revision_backend="bare_git", vector_store_driver="pgvector",
                              vector_store_dsn=_dsn(_VCHORD, name))
        decision = await decide_sparse_shape_for_settings(configured)
        assert decision is not None and decision.shape == "vchord"
        assert configured.effective_sparse_shape == "vchord"
        assert configured.bm25_external_stats_consumers == []
    finally:
        with contextlib.suppress(asyncpg.PostgresError):
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()
