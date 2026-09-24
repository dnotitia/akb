"""The index statistics and block summaries that bounded search relies on.

CREATE INDEX, CREATE INDEX CONCURRENTLY and REINDEX build a term's posting
blocks in one pass. So does every pg_restore, and so does the backfill
runbook's `--index`. vchord_bm25 0.3.0 wrote a full 128-posting block's score
summary before it recorded the block's best posting, and a bounded scan
(finite `bm25_catalog.bm25_limit`) skips a block by that summary (akb#679).
Its VACUUM also subtracted a length code instead of a length from the sum the
average document length comes from (akb#684). The image deploy/postgres builds
carries both fixes.

The exact scan (`bm25_limit = -1`) reads every posting and is the reference.
A block summary is exact when it is written. A later large change in the
average document length can still make one underestimate, so these compare
right after the index is written.

Requires the extension-capable server (AKB_VCHORD_TEST_DSN, the image
deploy/postgres builds). Each test creates and drops its own database.
"""

from __future__ import annotations

import contextlib
import os
import random
import re
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


async def _insert(conn: asyncpg.Connection, vectors: list[str], *, first: int = 0) -> None:
    """Insert rows one statement at a time: the insert path."""
    vault = uuid.UUID(int=0x679)
    await conn.executemany(
        """
        INSERT INTO vector_index.chunks
            (chunk_id, source_type, source_id, vault_id, section_path, content, chunk_index, sparse_bm25)
        VALUES ($1, 'document', $1, $2, '', '', $3, $4::bm25_catalog.bm25vector)
        """,
        [(uuid.UUID(int=first + i + 1), vault, first + i, vector) for i, vector in enumerate(vectors)],
    )


async def _load(conn: asyncpg.Connection, vectors: list[str]) -> None:
    """Insert the rows, then rebuild the index over them: the build path."""
    await _insert(conn, vectors)
    await conn.execute(f"REINDEX INDEX {_INDEX}")
    await conn.execute("ANALYZE vector_index.chunks")


async def _metapage(conn: asyncpg.Connection) -> dict[str, int]:
    text = await conn.fetchval("SELECT bm25_catalog.bm25_page_inspect($1::regclass, 0)", _INDEX)
    return {key: int(re.search(rf"\b{key}: (\d+)", text).group(1))
            for key in ("doc_cnt", "doc_term_cnt", "sealed_doc_id")}


_RANKED = f"""
    SELECT score FROM (
      SELECT c.sparse_bm25 <&> bm25_catalog.to_bm25query(
               '{_INDEX}'::regclass, $1::bm25_catalog.bm25vector) AS score
      FROM vector_index.chunks c
      WHERE c.sparse_bm25 IS NOT NULL
      ORDER BY score LIMIT {_TOP}) ranked
    WHERE score < 0
"""


async def _top_scores(conn: asyncpg.Connection, query: str, budget: int) -> list[float]:
    async with conn.transaction():
        await conn.execute('SET LOCAL search_path TO "$user", public, bm25_catalog')
        await conn.execute("SET LOCAL enable_seqscan = off")  # the index scan is what is under test
        await conn.execute(f"SET LOCAL bm25_catalog.bm25_limit = {budget}")
        if budget != -1:
            plan = "\n".join(r[0] for r in await conn.fetch("EXPLAIN " + _RANKED, query))
            assert "Index Scan using idx_vi_chunks_bm25" in plan, plan
        rows = await conn.fetch(_RANKED, query)
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
        exact = await _top_scores(conn, "{10:1}", -1)
        bounded = await _top_scores(conn, "{10:1}", _BOUNDED)

    assert len(exact) == _TOP
    assert exact[:3] == [exact[0]] * 3 and exact[0] < exact[3]  # the three block ends lead
    assert bounded == exact


async def test_blocks_the_seal_writes_keep_their_best_posting():
    """Rows that arrive by insert reach sealed blocks through the seal path.

    That path records a block's best posting before it flushes the block, so it
    never had akb#679. The rows of the case above go in one insert at a time,
    with the growing segment limited to one page so that inserts seal it, and
    rows without term 10 follow until a seal has taken all 400.
    """
    ends = {127, 255, 383}
    vectors = [
        "{10:1}" if i in ends else "{10:1,%d:19}" % (100_000 + i)
        for i in range(400)
    ] + ["{30:1,%d:19}" % (200_000 + i) for i in range(1_000)]
    async with _database() as conn:
        await conn.execute("LOAD 'vchord_bm25'")  # its settings exist once the library is loaded
        await conn.execute("SET bm25_catalog.segment_growing_max_page_size = 1")
        await _insert(conn, vectors)
        await conn.execute("RESET bm25_catalog.segment_growing_max_page_size")
        await conn.execute("ANALYZE vector_index.chunks")
        sealed = (await _metapage(conn))["sealed_doc_id"]
        exact = await _top_scores(conn, "{10:1}", -1)
        bounded = await _top_scores(conn, "{10:1}", _BOUNDED)

    assert sealed >= 400, sealed  # every row holding term 10 went through a seal
    assert bounded == exact


async def test_vacuum_takes_back_the_length_an_insert_added():
    """VACUUM leaves the sum of document lengths as the remaining rows have it (akb#684).

    The metapage's doc_term_cnt is that sum, and BM25 takes the average document
    length from it. An insert adds the exact length. Upstream 0.3.0's VACUUM
    subtracted the document's one-byte length code instead: at a length of 64
    that code is 50, so 14 of every deleted document's 64 stayed. The index
    stores only the code, so the fix subtracts the length the code stands for,
    which at a bucket start such as 64 is all of it.
    """
    async with _database() as conn:
        await _load(conn, ["{10:1,%d:9}" % (100_000 + i) for i in range(200)])
        before = await _metapage(conn)
        await _insert(conn, ["{20:64}"] * 500, first=1_000)
        assert (await _metapage(conn))["doc_term_cnt"] == before["doc_term_cnt"] + 500 * 64
        await conn.execute("DELETE FROM vector_index.chunks WHERE chunk_index >= 1000")
        await conn.execute("VACUUM vector_index.chunks")
        after = await _metapage(conn)

    assert after["doc_cnt"] == before["doc_cnt"]
    assert after["doc_term_cnt"] == before["doc_term_cnt"], after["doc_term_cnt"] - before["doc_term_cnt"]


async def test_a_corpus_built_by_create_index_ranks_like_the_exact_scan():
    """A skewed random corpus, compared with the exact scan query by query.

    The same shape as the measurement on a production-sized index. With this
    seed, the upstream 0.3.0 build gives 14 of the terms with at least 128
    postings a worse bounded top-90, and queries of several terms go wrong too.
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

    queries = ["{%d:1}" % term for term in terms]
    for _ in range(300):
        picked = sorted(rnd.sample(terms, rnd.randint(2, 3)))
        queries.append("{" + ",".join(f"{t}:1" for t in picked) + "}")

    async with _database() as conn:
        await _load(conn, vectors)
        worse = {}
        for query in queries:
            ranks = _worse_ranks(await _top_scores(conn, query, _BOUNDED), await _top_scores(conn, query, -1))
            if ranks:
                worse[query] = ranks

    assert worse == {}, f"{len(worse)} of {len(queries)} queries rank worse bounded: {dict(list(worse.items())[:10])}"
