"""Session templates for tests that start with the same vector storage schema."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
import pytest
import pytest_asyncio

from tests.helpers.pg_database import (
    TestDatabase,
    database_template,
    initialize_application_schema,
    temporary_database,
)

_VCHORD_DSN = os.environ.get("AKB_VCHORD_TEST_DSN", "")


async def _initialize_vector_schema(conn: asyncpg.Connection, shape: str = "vchord") -> None:
    from app.services.vector_store.pgvector import PgvectorStore

    store = PgvectorStore(dsn=None, schema="vector_index", dense_dim=4, sparse_shape=shape)
    await store._do_ensure(conn)


@asynccontextmanager
async def _compaction_shape(base: TestDatabase, shape: str) -> AsyncIterator[TestDatabase]:
    async with temporary_database(
        _VCHORD_DSN, f"akb_compact_{shape}_template_", template=base,
    ) as database:
        conn = await asyncpg.connect(database.dsn)
        try:
            await _initialize_vector_schema(conn, shape)
        finally:
            await conn.close()
        yield database


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def compaction_template() -> AsyncIterator[dict[str, TestDatabase]]:
    if not _VCHORD_DSN:
        pytest.skip("AKB_VCHORD_TEST_DSN is required for VectorChord coverage")
    async with (
        database_template(
            _VCHORD_DSN, "akb_compact_template_", initialize_application_schema, required=True,
        ) as base,
        _compaction_shape(base, "vchord") as vchord,
        _compaction_shape(base, "posting") as posting,
    ):
        yield {"vchord": vchord, "posting": posting}


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def empty_vchord_template() -> AsyncIterator[TestDatabase]:
    if not _VCHORD_DSN:
        pytest.skip("AKB_VCHORD_TEST_DSN is required for VectorChord coverage")
    async with database_template(
        _VCHORD_DSN, "akb_vchord_template_", _initialize_vector_schema, required=True,
    ) as template:
        yield template


async def initialize_backfill_vocabulary(conn: asyncpg.Connection) -> None:
    """Materialize the vocabulary and posting weights used by the backfill fixture."""
    from app.db.postgres import _load_migration

    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bm25_vocab (
            term TEXT PRIMARY KEY,
            term_id BIGINT NOT NULL UNIQUE,
            df BIGINT NOT NULL DEFAULT 0,
            first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    await conn.execute("CREATE SEQUENCE IF NOT EXISTS bm25_term_id_seq AS BIGINT START 1")
    # The encoder reports, and the writers check, the numbering its ids are in
    # (akb#687) — migration 113 itself rather than a copy of it.
    await _load_migration("113_bm25_vocab_epoch.py").migrate(conn)
    # The pre-flip write path saturates against these, so they have to be
    # readable — the values do not matter here, only that `posting` encoding
    # produces something and `vchord` encoding ignores them.
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bm25_stats (
            id INTEGER PRIMARY KEY DEFAULT 1,
            total_docs BIGINT NOT NULL DEFAULT 0,
            avgdl DOUBLE PRECISION NOT NULL DEFAULT 0,
            tokenizer_name TEXT NOT NULL DEFAULT 'kiwi',
            tokenizer_version TEXT NOT NULL DEFAULT '0',
            k1 DOUBLE PRECISION NOT NULL DEFAULT 1.5,
            b DOUBLE PRECISION NOT NULL DEFAULT 0.75,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK (id = 1)
        )
        """
    )
    await conn.execute(
        "INSERT INTO bm25_stats (id, total_docs, avgdl) VALUES (1, 100, 3) "
        "ON CONFLICT (id) DO NOTHING"
    )


async def _initialize_backfill_schema(conn: asyncpg.Connection) -> None:
    await initialize_backfill_vocabulary(conn)
    await _initialize_vector_schema(conn, "posting")


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def corpus_template() -> AsyncIterator[TestDatabase]:
    if not _VCHORD_DSN:
        pytest.skip("AKB_VCHORD_TEST_DSN 미설정 — 확장이 있는 PostgreSQL 이 필요하다")
    async with database_template(
        _VCHORD_DSN, "akb_bfill_template_", _initialize_backfill_schema, required=True,
    ) as template:
        yield template
