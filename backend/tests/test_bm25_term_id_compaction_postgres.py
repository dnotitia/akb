"""Renumbering BM25 term ids densely, against PostgreSQL with the extension (akb#687).

`scripts/compact_bm25_term_ids.py` replaces a sparse term-id space with the
ranks of its ids. A term id is a label: BM25 scores come from tf, df, N and
the average document length. So the proof is that every query ranks the same
documents with the same scores afterwards, while the index's per-term arrays
shrink to the vocabulary.

The command is driven through its entry point, as an operator runs it, and
the rankings come from an oracle of this file's own: the exact scan
(`bm25_catalog.bm25_limit = -1`) over the index. Ties may reorder; the scores
of every rank, and the documents above the last score, may not.

The fence test runs the real indexing worker. Its encode step is wrapped so
that the renumbering commits between the encoding and the write — the window
the vocabulary epoch closes — which makes the interleaving deterministic.

Requires the extension-capable server (AKB_VCHORD_TEST_DSN, the image
deploy/postgres builds). Each test creates and drops its own database.
"""

from __future__ import annotations

import contextlib
import os
import random
import re
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock

import asyncpg
import pytest

from app.config import settings
from app.db import postgres as postgres_module
from app.services import embed_worker, sparse_encoder
from app.services.bm25_maintenance import BM25_VOCAB_EPOCH_LOCK_KEY
from app.services.vector_store.pgvector import PgvectorStore
from scripts import compact_bm25_term_ids as compact

pytestmark = pytest.mark.asyncio

_DSN = os.environ.get("AKB_VCHORD_TEST_DSN", "")
_SCHEMA = "vector_index"
_INDEX = f"{_SCHEMA}.idx_vi_chunks_bm25"
_INIT_SQL = Path(__file__).resolve().parents[1] / "app" / "db" / "init.sql"
_TOP = 20
_TOLERANCE = 1e-6


@dataclass
class _Installation:
    pool: asyncpg.Pool
    store: PgvectorStore
    dsn: str


async def _split(text: str) -> list[str]:
    """Kiwi is not what is under test; a word split is deterministic."""
    return text.split()


@contextlib.asynccontextmanager
async def _installation(monkeypatch, *, shape: str = "vchord") -> AsyncIterator[_Installation]:
    """A database the way the backend leaves it, with this process pointed at it."""
    if not _DSN:
        pytest.skip("AKB_VCHORD_TEST_DSN is required for VectorChord coverage")
    admin = await asyncpg.connect(_DSN)
    name = f"akb_compact_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    dsn = f"{_DSN.rsplit('/', 1)[0]}/{name}"
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=8)
    previous_pool = postgres_module._pool
    try:
        async with pool.acquire() as conn:
            await conn.execute(_INIT_SQL.read_text())
        postgres_module._pool = pool
        await postgres_module._apply_migrations()
        store = PgvectorStore(
            dsn=None, schema=_SCHEMA, dense_dim=4, sparse_shape=shape,
            get_main_pool=postgres_module.get_pool,
        )
        async with pool.acquire() as conn:
            await store._do_ensure(conn)
        store._ensured_collection = True

        # The command reads the process configuration, as it does when run.
        monkeypatch.setattr(type(settings), "asyncpg_dsn", property(lambda _settings: dsn))
        monkeypatch.setattr(settings, "vector_store_driver", "pgvector")
        monkeypatch.setattr(settings, "vector_store_dsn", "")
        monkeypatch.setattr(settings, "vector_store_schema", _SCHEMA)
        monkeypatch.setattr(settings, "vector_store_sparse_shape", shape)
        monkeypatch.setattr(settings, "_sparse_shape_decision", None)
        monkeypatch.setattr(settings, "embed_base_url", "")
        # The command closes the process pool on the way out; here it is ours.
        monkeypatch.setattr(compact, "close_pool", AsyncMock())
        monkeypatch.setattr(sparse_encoder, "tokenize", _split)
        monkeypatch.setattr(embed_worker, "get_vector_store", lambda: store)
        yield _Installation(pool=pool, store=store, dsn=dsn)
    finally:
        postgres_module._pool = previous_pool
        await pool.close()
        with contextlib.suppress(asyncpg.PostgresError):
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


@dataclass
class _Corpus:
    vocabulary: list[str]
    ids: dict[str, int]
    documents: dict[str, Counter]


async def _seed(conn, *, terms: int, documents: int, max_id: int, seed: int, shape: str = "vchord") -> _Corpus:
    """A vocabulary whose ids are spread over `max_id`, and documents over it.

    The ids are a random sample of the range, shuffled over the terms, so an
    id says nothing about how common its term is. The sequence stands past the
    largest id, as it does after the id-burning allocators of akb#664/#687.
    Document content is the terms themselves, so the indexing worker's encoder
    reproduces exactly the vectors written here.
    """
    rnd = random.Random(seed)
    vocabulary = [f"w{i:05d}" for i in range(terms)]
    ids = rnd.sample(range(1, max_id + 1), terms)
    ids[0] = max_id  # the largest id is certainly in use
    rnd.shuffle(ids)
    term_ids = dict(zip(vocabulary, ids))
    await conn.executemany(
        "INSERT INTO bm25_vocab (term, term_id) VALUES ($1, $2)", list(term_ids.items()),
    )
    await conn.execute("SELECT setval('bm25_term_id_seq', $1)", max_id + 1_000)

    corpus = _Corpus(vocabulary=vocabulary, ids=term_ids, documents={})
    rows = []
    vault = uuid.UUID(int=0x687)
    for position in range(documents):
        counts: Counter = Counter()
        for _ in range(rnd.randint(3, 40)):
            counts[vocabulary[int(terms * rnd.random() ** 3)]] += 1  # low indices are common
        chunk_id = uuid.UUID(int=rnd.getrandbits(128), version=4)
        corpus.documents[str(chunk_id)] = counts
        content = " ".join(term for term in sorted(counts) for _ in range(counts[term]))
        rows.append((chunk_id, vault, content, position, counts))

    if shape == "vchord":
        await conn.executemany(
            f"""
            INSERT INTO {_SCHEMA}.chunks
                (chunk_id, source_type, source_id, vault_id, section_path, content, chunk_index, sparse_bm25)
            VALUES ($1, 'document', $1, $2, '', $3, $4, $5::bm25_catalog.bm25vector)
            """,
            [(c, v, text, p, _literal(counts, term_ids)) for c, v, text, p, counts in rows],
        )
        await conn.execute(f"REINDEX INDEX {_INDEX}")  # the build path: settled statistics
    else:
        await conn.executemany(
            f"""
            INSERT INTO {_SCHEMA}.chunks
                (chunk_id, source_type, source_id, vault_id, section_path, content, chunk_index)
            VALUES ($1, 'document', $1, $2, '', $3, $4)
            """,
            [(c, v, text, p) for c, v, text, p, _ in rows],
        )
        await conn.executemany(
            f"INSERT INTO {_SCHEMA}.posting (term_id, chunk_id, weight) VALUES ($1, $2, $3)",
            [
                (term_ids[term], c, round(rnd.uniform(0.2, 2.5), 3))
                for c, _, _, _, counts in rows for term in counts
            ],
        )
    await conn.execute(f"ANALYZE {_SCHEMA}.chunks")
    return corpus


def _literal(counts: Counter, term_ids: dict[str, int]) -> str:
    return "{" + ", ".join(
        f"{term_ids[term]}:{counts[term]}" for term in sorted(counts, key=term_ids.__getitem__)
    ) + "}"


def _queries(corpus: _Corpus, *, seed: int) -> list[tuple[str, ...]]:
    """Common, middling, rare and absent terms alone, and in twos and threes."""
    rnd = random.Random(seed)
    common = corpus.vocabulary[: len(corpus.vocabulary) // 10]
    singles = [(corpus.vocabulary[i],) for i in (0, 1, 2, 5, 10, 30, 100, 300, 1000) if i < len(corpus.vocabulary)]
    singles.append((corpus.vocabulary[-1],))
    pairs = [tuple(sorted(rnd.sample(common, 2))) for _ in range(10)]
    triples = [tuple(sorted(rnd.sample(corpus.vocabulary[: len(corpus.vocabulary) // 3], 3))) for _ in range(10)]
    return list(dict.fromkeys(singles + pairs + triples))


_RANKED = f"""
    SELECT id, score FROM (
      SELECT c.chunk_id::text AS id,
             c.sparse_bm25 <&> bm25_catalog.to_bm25query(
                 '{_INDEX}'::regclass, $1::text::bm25_catalog.bm25vector) AS score
        FROM {_SCHEMA}.chunks c
       WHERE c.sparse_bm25 IS NOT NULL
       ORDER BY score LIMIT {_TOP}) ranked
     WHERE score < 0
     ORDER BY score, id
"""


async def _ranking(conn, terms: tuple[str, ...]) -> list[tuple[str, float]]:
    """The exact top-k for these terms, through the ids the vocabulary gives now."""
    ids = sorted(r["term_id"] for r in await conn.fetch(
        "SELECT term_id FROM bm25_vocab WHERE term = ANY($1::text[])", list(terms),
    ))
    if not ids:
        return []
    async with conn.transaction():
        await conn.execute('SET LOCAL search_path TO "$user", public, bm25_catalog')
        await conn.fetchval("SELECT '{}'::bm25_catalog.bm25vector IS NOT NULL")  # loads the extension's settings
        await conn.execute("SET LOCAL bm25_catalog.bm25_limit = -1")
        rows = await conn.fetch(_RANKED, "{" + ", ".join(f"{i}:1" for i in ids) + "}")
    return [(row["id"], row["score"]) for row in rows]


async def _posting_ranking(conn, terms: tuple[str, ...]) -> list[tuple[str, float]]:
    rows = await conn.fetch(
        f"""
        SELECT p.chunk_id::text AS id, sum(p.weight::float8) AS score
          FROM {_SCHEMA}.posting p
          JOIN bm25_vocab v ON v.term_id = p.term_id
         WHERE v.term = ANY($1::text[])
         GROUP BY p.chunk_id
         ORDER BY score DESC, id
         LIMIT {_TOP}
        """,
        list(terms),
    )
    return [(row["id"], -row["score"]) for row in rows]  # ascending, like the vchord oracle


def _assert_same_ranking(before, after, query) -> None:
    """Scores per rank within the tolerance; the documents above the boundary exact."""
    assert len(after) == len(before), query
    assert [s for _, s in after] == pytest.approx([s for _, s in before], rel=_TOLERANCE, abs=_TOLERANCE), query
    if not before:
        return
    boundary = before[-1][1]
    above = {doc for doc, score in before if score < boundary - _TOLERANCE}
    assert above == {doc for doc, score in after if score < boundary - _TOLERANCE}, query
    now = dict(after)
    for doc, score in before:
        if doc in now:
            assert now[doc] == pytest.approx(score, rel=_TOLERANCE, abs=_TOLERANCE), (query, doc)


async def _metapage(conn) -> dict[str, int]:
    text = await conn.fetchval("SELECT bm25_catalog.bm25_page_inspect($1::regclass, 0)", _INDEX)
    return {
        "doc_cnt": int(re.search(r"\bdoc_cnt: (\d+)", text).group(1)),
        "doc_term_cnt": int(re.search(r"\bdoc_term_cnt: (\d+)", text).group(1)),
        # Twice in the page: the index's, and its sealed segment's.
        "term_id_cnt": max(int(n) for n in re.findall(r"\bterm_id_cnt: (\d+)", text)),
    }


async def _vectors(conn) -> dict[str, str]:
    return {
        row["id"]: row["vector"]
        for row in await conn.fetch(f"SELECT chunk_id::text AS id, sparse_bm25::text AS vector FROM {_SCHEMA}.chunks")
    }


def _terms_of(vector: str, names: dict[int, str]) -> Counter:
    counts: Counter = Counter()
    for pair in vector.strip("{}").split(", "):
        if pair:
            term_id, tf = pair.split(":")
            counts[names[int(term_id)]] = int(tf)
    return counts


async def _vocabulary(conn) -> dict[str, int]:
    return {row["term"]: row["term_id"] for row in await conn.fetch("SELECT term, term_id FROM bm25_vocab")}


async def _state(conn) -> dict:
    """Everything the renumbering may change, for "nothing changed" assertions."""
    sequence = await conn.fetchrow("SELECT last_value, is_called FROM bm25_term_id_seq")
    return {
        "vocabulary": await _vocabulary(conn),
        "vectors": await _vectors(conn),
        "epoch": await conn.fetchval("SELECT epoch FROM bm25_vocab_epoch"),
        "sequence": (sequence["last_value"], sequence["is_called"]),
        "metapage": await _metapage(conn),
        "mapping": await conn.fetchval("SELECT to_regclass('bm25_term_id_remap') IS NOT NULL"),
    }


async def test_the_renumbering_is_dense_and_every_query_ranks_the_same(monkeypatch):
    async with _installation(monkeypatch) as install:
        async with install.pool.acquire() as conn:
            corpus = await _seed(conn, terms=3_000, documents=5_000, max_id=50_000_000, seed=687)
            queries = _queries(corpus, seed=687)
            before = {query: await _ranking(conn, query) for query in queries}
            meta_before = await _metapage(conn)
            size_before = await conn.fetchval("SELECT pg_relation_size($1::regclass)", _INDEX)
        assert meta_before["term_id_cnt"] == 50_000_001
        assert sum(1 for ranking in before.values() if ranking) >= len(queries) - 1

        assert await compact.main(["--apply"]) == 0

        async with install.pool.acquire() as conn:
            ids = await _vocabulary(conn)
            size = len(ids)
            assert size == 3_000
            assert sorted(ids.values()) == list(range(size))
            # Monotone: the terms keep their order.
            assert sorted(ids, key=ids.__getitem__) == sorted(corpus.ids, key=corpus.ids.__getitem__)
            names = {term_id: term for term, term_id in ids.items()}
            vectors = await _vectors(conn)
            assert {doc: _terms_of(vector, names) for doc, vector in vectors.items()} == corpus.documents
            assert all(
                int(pair.split(":")[0]) < size
                for vector in vectors.values() for pair in vector.strip("{}").split(", ") if pair
            )
            meta_after = await _metapage(conn)
            assert meta_after["term_id_cnt"] <= size
            assert meta_after["doc_cnt"] == meta_before["doc_cnt"]
            assert meta_after["doc_term_cnt"] == meta_before["doc_term_cnt"]
            assert await conn.fetchval("SELECT pg_relation_size($1::regclass)", _INDEX) < size_before / 10
            for query in queries:
                _assert_same_ranking(before[query], await _ranking(conn, query), query)
            assert await conn.fetchval("SELECT epoch FROM bm25_vocab_epoch") == 1
            assert await conn.fetchval("SELECT nextval('bm25_term_id_seq')") == size
            assert await conn.fetchval("SELECT count(*) FROM bm25_term_id_remap") == size


async def test_a_second_run_has_nothing_to_do(monkeypatch, capsys):
    async with _installation(monkeypatch) as install:
        async with install.pool.acquire() as conn:
            await _seed(conn, terms=200, documents=150, max_id=1_000_000, seed=2)
        assert await compact.main(["--apply"]) == 0
        async with install.pool.acquire() as conn:
            state = await _state(conn)
        capsys.readouterr()

        assert await compact.main(["--apply"]) == 0

        assert "already dense" in capsys.readouterr().out
        async with install.pool.acquire() as conn:
            assert await _state(conn) == state


async def test_the_dry_run_reports_and_changes_nothing(monkeypatch, capsys):
    async with _installation(monkeypatch) as install:
        async with install.pool.acquire() as conn:
            await _seed(conn, terms=200, documents=150, max_id=1_000_000, seed=3)
            state = await _state(conn)

        assert await compact.main([]) == 0

        out = capsys.readouterr().out
        assert "vchord" in out and "200 terms" in out and "1,000,000" in out
        assert "dry run" in out.lower()
        async with install.pool.acquire() as conn:
            assert await _state(conn) == state


async def test_a_chunk_encoded_before_the_renumbering_is_refused_then_stored_renumbered(monkeypatch):
    """The fence, with the renumbering committed between encode and write.

    The encoding holds ids from epoch 0, one of them for a term it registered
    itself. The write must not land: it goes to the retry path, and the next
    attempt stores the chunk under the new numbering.
    """
    async with _installation(monkeypatch) as install:
        async with install.pool.acquire() as conn:
            corpus = await _seed(conn, terms=400, documents=300, max_id=5_000_000, seed=4)
            vault = await conn.fetchval(
                "INSERT INTO vaults (name, git_path) VALUES ('compact', '/tmp/compact.git') RETURNING id"
            )
            content = " ".join([corpus.vocabulary[1], corpus.vocabulary[1], corpus.vocabulary[50], "latecomer"])
            chunk = await conn.fetchval(
                """
                INSERT INTO chunks (source_type, source_id, vault_id, content, chunk_index)
                VALUES ('document', gen_random_uuid(), $1, $2, 0) RETURNING id
                """,
                vault, content,
            )

        encode = sparse_encoder.encode_document_at_epoch
        renumbered: list[int] = []

        async def encode_then_renumber(text, *, sparse_shape=None):
            encoded = await encode(text, sparse_shape=sparse_shape)
            if not renumbered:
                renumbered.append(await compact.main(["--apply"]))
            return encoded

        monkeypatch.setattr(sparse_encoder, "encode_document_at_epoch", encode_then_renumber)

        assert await embed_worker._process_once() == 0
        assert renumbered == [0]
        async with install.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT vector_indexed_at, vector_last_error, vector_retry_count FROM chunks WHERE id = $1", chunk,
            )
            assert row["vector_indexed_at"] is None
            assert "renumbered" in row["vector_last_error"] and "epoch 0" in row["vector_last_error"]
            assert row["vector_retry_count"] == 1
            assert not await conn.fetchval(f"SELECT EXISTS (SELECT 1 FROM {_SCHEMA}.chunks WHERE chunk_id = $1)", chunk)
            await conn.execute("UPDATE chunks SET vector_next_attempt_at = NOW() WHERE id = $1", chunk)

        assert await embed_worker._process_once() == 1

        async with install.pool.acquire() as conn:
            ids = await _vocabulary(conn)
            stored = await conn.fetchval(f"SELECT sparse_bm25::text FROM {_SCHEMA}.chunks WHERE chunk_id = $1", chunk)
            assert await conn.fetchval("SELECT vector_indexed_at IS NOT NULL FROM chunks WHERE id = $1", chunk)
        assert stored == _literal(Counter(content.split()), ids)
        assert max(ids.values()) == len(ids) - 1  # the latecomer was renumbered with the rest


async def test_a_term_id_the_index_cannot_hold_abandons_on_the_first_failure(monkeypatch):
    """G1(a): `TermIdOutOfRange` is deterministic, so no retry is spent on it.

    One term is moved past 2^30 after the encoding, so the write refuses it.
    The chunk is abandoned on the first failure: no `next_attempt_at`, one
    error naming the id, the bound and the remedy — not ~8 retries over
    ~13.6h with an embed call each.
    """
    async with _installation(monkeypatch) as install:
        async with install.pool.acquire() as conn:
            corpus = await _seed(conn, terms=100, documents=0, max_id=100_000, seed=6)
            victim = corpus.vocabulary[0]
            # The vocabulary already numbers this term past what the index
            # holds: the worker encodes it as-is and the write refuses it.
            await conn.execute(
                "UPDATE bm25_vocab SET term_id = $1 WHERE term = $2",
                (1 << 30), victim,
            )
            vault = await conn.fetchval(
                "INSERT INTO vaults (name, git_path) VALUES ('past-index', '/tmp/past.git') RETURNING id"
            )
            content = " ".join([victim, victim, corpus.vocabulary[1]])
            chunk = await conn.fetchval(
                """
                INSERT INTO chunks (source_type, source_id, vault_id, content, chunk_index)
                VALUES ('document', gen_random_uuid(), $1, $2, 0) RETURNING id
                """,
                vault, content,
            )

        # The return counts successes; the terminal path `continue`s without
        # reaching `succeeded += 1`, so it returns 0 for this one-chunk
        # batch. What matters is the row state below: abandoned on the
        # first failure — retry count still 1 from the claim, no next
        # attempt — not ~8 retries over ~13.6h.
        assert await embed_worker._process_once() == 0
        async with install.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT vector_indexed_at, vector_last_error, vector_retry_count, "
                "vector_next_attempt_at, vector_abandoned_at IS NOT NULL AS abandoned "
                "FROM chunks WHERE id = $1",
                chunk,
            )
            assert row["vector_indexed_at"] is None
            assert row["vector_retry_count"] == 1
            assert row["vector_next_attempt_at"] is None
            assert row["abandoned"]
            assert "term id 1073741824" in row["vector_last_error"]
            assert "compact_bm25_term_ids" in row["vector_last_error"]
            assert not await conn.fetchval(f"SELECT EXISTS (SELECT 1 FROM {_SCHEMA}.chunks WHERE chunk_id = $1)", chunk)


async def test_indexing_hands_its_batch_back_while_a_renumbering_holds_the_fence(monkeypatch):
    """A renumbering holds the fence for its whole transaction; retries are not spent on it."""
    async with _installation(monkeypatch) as install:
        async with install.pool.acquire() as conn:
            corpus = await _seed(conn, terms=100, documents=50, max_id=100_000, seed=5)
            vault = await conn.fetchval(
                "INSERT INTO vaults (name, git_path) VALUES ('fence', '/tmp/fence.git') RETURNING id"
            )
            chunk = await conn.fetchval(
                """
                INSERT INTO chunks (source_type, source_id, vault_id, content, chunk_index)
                VALUES ('document', gen_random_uuid(), $1, $2, 0) RETURNING id
                """,
                vault, " ".join(corpus.vocabulary[:3]),
            )
        holder = await asyncpg.connect(install.dsn)
        try:
            await holder.execute("SELECT pg_advisory_lock($1)", BM25_VOCAB_EPOCH_LOCK_KEY)
            assert await embed_worker._process_once() == 0
            async with install.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT vector_indexed_at, vector_last_error, vector_retry_count,
                           vector_next_attempt_at <= NOW() AS due
                      FROM chunks WHERE id = $1
                    """,
                    chunk,
                )
            assert row["vector_indexed_at"] is None
            assert row["vector_last_error"] is None
            assert row["vector_retry_count"] == 0
            assert row["due"]
        finally:
            await holder.execute("SELECT pg_advisory_unlock_all()")
            await holder.close()

        assert await embed_worker._process_once() == 1


@pytest.mark.parametrize("driver", ["qdrant", "seahorse-db"])
async def test_a_driver_that_keeps_its_vectors_elsewhere_is_refused(monkeypatch, capsys, driver):
    async with _installation(monkeypatch) as install:
        async with install.pool.acquire() as conn:
            await _seed(conn, terms=100, documents=50, max_id=100_000, seed=6)
            state = await _state(conn)
        monkeypatch.setattr(settings, "vector_store_driver", driver)

        assert await compact.main(["--apply"]) == compact.EXIT_REFUSED

        assert driver in capsys.readouterr().out
        async with install.pool.acquire() as conn:
            assert await _state(conn) == state


async def test_term_ids_left_in_an_inactive_shape_are_refused(monkeypatch, capsys):
    """A `posting` table kept under `vchord` holds ids too; this command would strand them."""
    async with _installation(monkeypatch) as install:
        async with install.pool.acquire() as conn:
            corpus = await _seed(conn, terms=100, documents=50, max_id=100_000, seed=7)
            await conn.execute(
                f"""
                CREATE TABLE {_SCHEMA}.posting (
                    term_id BIGINT NOT NULL, chunk_id UUID NOT NULL, weight REAL NOT NULL,
                    PRIMARY KEY (term_id, chunk_id)
                )
                """
            )
            await conn.execute(
                f"INSERT INTO {_SCHEMA}.posting VALUES ($1, gen_random_uuid(), 1.0)",
                corpus.ids[corpus.vocabulary[0]],
            )
            state = await _state(conn)

        assert await compact.main(["--apply"]) == compact.EXIT_REFUSED

        assert "posting" in capsys.readouterr().out
        async with install.pool.acquire() as conn:
            assert await _state(conn) == state


async def test_vectors_left_under_posting_are_refused(monkeypatch, capsys):
    """The other direction: a `sparse_bm25` column filled while `posting` serves."""
    async with _installation(monkeypatch, shape="posting") as install:
        async with install.pool.acquire() as conn:
            corpus = await _seed(conn, terms=100, documents=50, max_id=100_000, seed=8, shape="posting")
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vchord_bm25")
            await conn.execute(f"ALTER TABLE {_SCHEMA}.chunks ADD COLUMN sparse_bm25 bm25_catalog.bm25vector")
            await conn.execute(
                f"UPDATE {_SCHEMA}.chunks SET sparse_bm25 = $1::bm25_catalog.bm25vector "
                f"WHERE chunk_id = (SELECT chunk_id FROM {_SCHEMA}.chunks ORDER BY chunk_id LIMIT 1)",
                "{%d:1}" % corpus.ids[corpus.vocabulary[0]],
            )
            vocabulary = await _vocabulary(conn)

        assert await compact.main(["--apply"]) == compact.EXIT_REFUSED

        assert "sparse_bm25" in capsys.readouterr().out
        async with install.pool.acquire() as conn:
            assert await _vocabulary(conn) == vocabulary


async def test_an_index_counting_rows_vacuum_has_not_removed_is_refused(monkeypatch, capsys):
    """The baseline must come from statistics of the live rows alone.

    The index counts an updated row twice until VACUUM takes the old version
    out. A rebuild counts it once, so the scores would differ for a reason that
    has nothing to do with the renumbering.
    """
    async with _installation(monkeypatch) as install:
        async with install.pool.acquire() as conn:
            corpus = await _seed(conn, terms=100, documents=50, max_id=100_000, seed=9)
            # A changed vector, so not a HOT update: the new version goes into
            # the index and the old one stays counted until VACUUM.
            await conn.execute(
                f"UPDATE {_SCHEMA}.chunks SET sparse_bm25 = $1::bm25_catalog.bm25vector "
                f"WHERE chunk_id = (SELECT chunk_id FROM {_SCHEMA}.chunks ORDER BY chunk_id LIMIT 1)",
                "{%d:5}" % corpus.ids[corpus.vocabulary[0]],
            )
            meta = await _metapage(conn)
        assert meta["doc_cnt"] == 51

        assert await compact.main(["--apply"]) == compact.EXIT_REFUSED
        assert "VACUUM" in capsys.readouterr().out

        async with install.pool.acquire() as conn:
            await conn.execute(f"VACUUM {_SCHEMA}.chunks")
        assert await compact.main(["--apply"]) == 0


async def test_a_failed_verification_changes_nothing(monkeypatch, capsys):
    async with _installation(monkeypatch) as install:
        async with install.pool.acquire() as conn:
            await _seed(conn, terms=300, documents=200, max_id=2_000_000, seed=10)
            state = await _state(conn)
        monkeypatch.setattr(compact, "_ranking_difference", lambda before, after: "injected difference")

        assert await compact.main(["--apply"]) == compact.EXIT_FAILED

        assert "injected difference" in capsys.readouterr().out
        async with install.pool.acquire() as conn:
            assert await _state(conn) == state
            assert await conn.fetchval("SELECT nextval('bm25_term_id_seq')") == 2_000_000 + 1_001


async def test_revert_restores_the_original_numbering(monkeypatch):
    async with _installation(monkeypatch) as install:
        async with install.pool.acquire() as conn:
            corpus = await _seed(conn, terms=500, documents=400, max_id=5_000_000, seed=11)
            queries = _queries(corpus, seed=11)
            before = {query: await _ranking(conn, query) for query in queries}
            state = await _state(conn)

        assert await compact.main(["--apply"]) == 0
        assert await compact.main(["--revert"]) == 0

        async with install.pool.acquire() as conn:
            now = await _state(conn)
            assert now["vocabulary"] == state["vocabulary"]
            assert now["vectors"] == state["vectors"]
            assert now["metapage"]["term_id_cnt"] == state["metapage"]["term_id_cnt"]
            assert now["metapage"]["doc_cnt"] == state["metapage"]["doc_cnt"]
            assert now["epoch"] == 2
            assert not now["mapping"]
            for query in queries:
                _assert_same_ranking(before[query], await _ranking(conn, query), query)
            assert await conn.fetchval("SELECT nextval('bm25_term_id_seq')") > max(corpus.ids.values())


async def test_revert_keeps_a_term_registered_after_the_renumbering(monkeypatch):
    """A term minted under the dense numbering has no old id to go back to.

    It takes the next id past the old numbering's largest, and the vector that
    uses it follows it there.
    """
    async with _installation(monkeypatch) as install:
        async with install.pool.acquire() as conn:
            corpus = await _seed(conn, terms=300, documents=200, max_id=3_000_000, seed=12)
        assert await compact.main(["--apply"]) == 0

        content = f"{corpus.vocabulary[0]} latecomer latecomer"
        indices, values = await sparse_encoder.encode_document(content, sparse_shape="vchord")
        async with install.pool.acquire() as conn:
            assert await conn.fetchval("SELECT term_id FROM bm25_vocab WHERE term = 'latecomer'") == 300
            await install.store.upsert_one(
                conn=conn, chunk_id=str(uuid.uuid4()), source_type="document",
                source_id=str(uuid.uuid4()), vault_id=str(uuid.UUID(int=0x687)),
                section_path="", content=content, chunk_index=0, dense=None,
                sparse_indices=indices, sparse_values=values,
            )
            await conn.execute(f"VACUUM {_SCHEMA}.chunks")

        assert await compact.main(["--revert"]) == 0

        async with install.pool.acquire() as conn:
            ids = await _vocabulary(conn)
            names = {term_id: term for term, term_id in ids.items()}
            vectors = await _vectors(conn)
        assert {term: ids[term] for term in corpus.ids} == corpus.ids
        assert ids["latecomer"] == max(corpus.ids.values()) + 1
        holding = [counts for counts in (_terms_of(v, names) for v in vectors.values()) if "latecomer" in counts]
        assert holding == [Counter(content.split())]


async def test_posting_ids_are_renumbered_and_score_the_same(monkeypatch):
    async with _installation(monkeypatch, shape="posting") as install:
        async with install.pool.acquire() as conn:
            corpus = await _seed(conn, terms=1_000, documents=800, max_id=10_000_000, seed=13, shape="posting")
            queries = _queries(corpus, seed=13)
            before = {query: await _posting_ranking(conn, query) for query in queries}
            postings = await conn.fetchval(f"SELECT count(*) FROM {_SCHEMA}.posting")

        assert await compact.main(["--apply"]) == 0

        async with install.pool.acquire() as conn:
            ids = await _vocabulary(conn)
            assert sorted(ids.values()) == list(range(len(ids)))
            assert await conn.fetchval(f"SELECT count(*) FROM {_SCHEMA}.posting") == postings
            assert await conn.fetchval(f"SELECT max(term_id) FROM {_SCHEMA}.posting") < len(ids)
            for query in queries:
                _assert_same_ranking(before[query], await _posting_ranking(conn, query), query)
            assert await conn.fetchval("SELECT epoch FROM bm25_vocab_epoch") == 1
