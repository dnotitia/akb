"""An index built over existing rows ranks like the exact scan (akb#679).

CREATE INDEX, CREATE INDEX CONCURRENTLY and REINDEX build a term's posting
blocks in one pass. So does every pg_restore, and so does the backfill
runbook's `--index`. vchord_bm25 0.3.0 wrote a full 128-posting block's score
summary before it recorded the block's best posting, and a bounded scan
(finite `bm25_catalog.bm25_limit`) skips a block by that summary. The image
deploy/postgres builds carries the fix. These pin it against the exact scan
(`bm25_limit = -1`), which reads every posting and is the reference.

Requires the extension-capable server (AKB_VCHORD_TEST_DSN, the image
deploy/postgres builds). Each test creates and drops its own database.
"""

from __future__ import annotations

import contextlib
import os
import random
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest

from app.services.vector_store.pgvector import PgvectorStore

pytestmark = pytest.mark.asyncio

_DSN = os.environ.get("AKB_VCHORD_TEST_DSN", "")
_INDEX = "vector_index.idx_vi_chunks_bm25"
_TOP = 90
_BOUNDED = 100


@contextlib.asynccontextmanager
async def _database() -> AsyncIterator[asyncpg.Connection]:
    if not _DSN:
        pytest.skip("AKB_VCHORD_TEST_DSN is required for VectorChord coverage")
    base, _ = _DSN.rsplit("/", 1)
    name = f"akb_vchord_build_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(_DSN)
    await admin.execute(f'CREATE DATABASE "{name}"')
    conn = await asyncpg.connect(f"{base}/{name}")
    try:
        store = PgvectorStore(dsn=f"{base}/{name}", schema="vector_index", dense_dim=4, sparse_shape="vchord")
        await store._do_ensure(conn)  # the index, built empty as a new database builds it
        yield conn
    finally:
        await conn.close()
        with contextlib.suppress(asyncpg.PostgresError):
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def _load(conn: asyncpg.Connection, vectors: list[str]) -> None:
    """Insert the rows, then rebuild the index over them: the build path."""
    vault = uuid.UUID(int=0x679)
    await conn.executemany(
        """
        INSERT INTO vector_index.chunks
            (chunk_id, source_type, source_id, vault_id, section_path, content, chunk_index, sparse_bm25)
        VALUES ($1, 'document', $1, $2, '', '', $3, $4::bm25_catalog.bm25vector)
        """,
        [(uuid.UUID(int=i + 1), vault, i, vector) for i, vector in enumerate(vectors)],
    )
    await conn.execute(f"REINDEX INDEX {_INDEX}")
    await conn.execute("ANALYZE vector_index.chunks")


async def _top_scores(conn: asyncpg.Connection, term: int, budget: int) -> list[float]:
    async with conn.transaction():
        await conn.execute('SET LOCAL search_path TO "$user", public, bm25_catalog')
        await conn.execute("SET LOCAL enable_seqscan = off")  # the index scan is what is under test
        await conn.execute(f"SET LOCAL bm25_catalog.bm25_limit = {budget}")
        rows = await conn.fetch(
            f"""
            SELECT score FROM (
              SELECT c.sparse_bm25 <&> bm25_catalog.to_bm25query(
                       '{_INDEX}'::regclass, $1::bm25_catalog.bm25vector) AS score
              FROM vector_index.chunks c
              WHERE c.sparse_bm25 IS NOT NULL
              ORDER BY score LIMIT {_TOP}) ranked
            WHERE score < 0
            """,
            "{%d:1}" % term,
        )
    return [row["score"] for row in rows]


def _worse_ranks(bounded: list[float], exact: list[float]) -> int:
    """Ranks where the bounded page scores worse than the exact one.

    Scores are negative and ascending, so worse is greater. Comparing scores
    rather than row ids ignores ties, which either scan may order differently.
    """
    padded = bounded + [0.0] * (len(exact) - len(bounded))
    return sum(1 for b, e in zip(padded, exact) if b > e + 1e-6)


async def test_blocks_whose_best_posting_comes_last_stay_reachable():
    """Every full block's best posting is its last one, the case the build got wrong.

    Term 10 appears in 400 rows: three full blocks and a partial one. The rows
    that close the full blocks are the shortest and so score highest. The
    upstream build flushed each of those blocks before recording that posting,
    saved a best score of 0 for all three, and a bounded scan skipped them: its
    page held the 16 rows of the partial block and none of the best matches.
    """
    ends = {127, 255, 383}
    vectors = [
        "{10:1}" if i in ends else "{10:1,%d:19}" % (100_000 + i)
        for i in range(400)
    ]
    async with _database() as conn:
        await _load(conn, vectors)
        exact = await _top_scores(conn, 10, -1)
        bounded = await _top_scores(conn, 10, _BOUNDED)

    assert len(exact) == _TOP
    assert exact[:3] == [exact[0]] * 3 and exact[0] < exact[3]  # the three block ends lead
    assert bounded == exact


async def test_a_corpus_built_by_create_index_ranks_like_the_exact_scan():
    """A skewed random corpus, compared term by term against the exact scan.

    The same shape as the measurement on a production-sized index. With this
    seed, the upstream 0.3.0 build gives 14 of the terms with at least 128
    postings a worse bounded top-90.
    """
    rnd = random.Random(679)
    vectors = []
    for _ in range(20_000):
        counts: dict[int, int] = {}
        for _ in range(rnd.randint(3, 60)):
            term = int(3000 * rnd.random() ** 3)  # low ids are common
            counts[term] = counts.get(term, 0) + rnd.choice((1, 1, 1, 2, 3))
        vectors.append("{" + ",".join(f"{k}:{v}" for k, v in sorted(counts.items())) + "}")
    postings: dict[int, int] = {}
    for vector in vectors:
        for part in vector.strip("{}").split(","):
            term = int(part.split(":")[0])
            postings[term] = postings.get(term, 0) + 1
    terms = sorted(t for t, n in postings.items() if n >= 128)
    assert len(terms) > 1_000

    async with _database() as conn:
        await _load(conn, vectors)
        worse = {}
        for term in terms:
            ranks = _worse_ranks(await _top_scores(conn, term, _BOUNDED), await _top_scores(conn, term, -1))
            if ranks:
                worse[term] = ranks

    assert worse == {}, f"{len(worse)} of {len(terms)} terms rank worse bounded: {dict(list(worse.items())[:10])}"
