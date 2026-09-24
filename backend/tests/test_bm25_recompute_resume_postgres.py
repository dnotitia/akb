"""PostgreSQL proof that an interrupted BM25 recompute resumes (akb#616).

The claim is not "it eventually finishes" — the old implementation did that
too, by starting over. The claim is that work already paid for survives the
process that paid for it, and the only way to show that is to kill a scan
mid-corpus, run it again in a fresh call, and count how many documents the
second pass had to tokenize. A resumed run touches the remainder; a restarted
one touches everything, and the two are indistinguishable from the published
stats alone.

Everything here uses a substitute tokenizer. Kiwi's output is not what is
under test, and a deterministic `split()` makes the expected document
frequencies something the assertions can state outright rather than approximate.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from pathlib import Path

import asyncpg
import pytest

from app.services import sparse_encoder

pytestmark = pytest.mark.asyncio

_DSN = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@localhost:15432/akb")

# Twelve documents over a four-word vocabulary, chosen so every term has a
# different document frequency: alpha 12, beta 6, gamma 4, delta 3. A resumed
# run that double-counted a batch, or dropped one, lands on none of them.
_CORPUS = [
    " ".join(
        ["alpha"]
        + (["beta"] if i % 2 == 0 else [])
        + (["gamma"] if i % 3 == 0 else [])
        + (["delta"] if i % 4 == 0 else [])
    )
    for i in range(12)
]
_EXPECTED_DF = {"alpha": 12, "beta": 6, "gamma": 4, "delta": 3}
_EXPECTED_TOTAL_LENGTH = sum(len(d.split()) for d in _CORPUS)


async def _can_connect(dsn: str) -> bool:
    try:
        conn = await asyncpg.connect(dsn, timeout=2)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


def _database_dsn(name: str) -> str:
    base, _ = _DSN.rsplit("/", 1)
    return f"{base}/{name}"


@contextlib.asynccontextmanager
async def _fresh_database():
    if not await _can_connect(_DSN):
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"Required PostgreSQL is not reachable at {_DSN}")
        pytest.skip(f"Postgres not reachable at {_DSN}")

    admin = await asyncpg.connect(_DSN)
    name = f"akb_bm25_resume_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    pool = await asyncpg.create_pool(_database_dsn(name), min_size=1, max_size=4)
    previous_pool = None
    try:
        init_sql = (
            Path(__file__).resolve().parents[1] / "app" / "db" / "init.sql"
        ).read_text()
        async with pool.acquire() as conn:
            await conn.execute(init_sql)
        from app.db import postgres as postgres_module

        previous_pool = postgres_module._pool
        postgres_module._pool = pool
        await postgres_module._apply_migrations()
        async with pool.acquire() as conn:
            await _seed(conn)
        yield pool
    finally:
        from app.db import postgres as postgres_module

        postgres_module._pool = previous_pool
        await pool.close()
        with contextlib.suppress(asyncpg.PostgresError):
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def _seed(conn) -> None:
    vault_id = await conn.fetchval(
        "INSERT INTO vaults(name, git_path) VALUES($1, $2) RETURNING id",
        f"bm25-resume-{uuid.uuid4().hex[:8]}",
        f"/tmp/bm25-resume-{uuid.uuid4().hex[:8]}.git",
    )
    # Sequential ids so the keyset cursor walks the corpus in a known order;
    # a random uuid4 per row would make "the first six documents" meaningless.
    for i, content in enumerate(_CORPUS):
        await conn.execute(
            """
            INSERT INTO chunks(
                id, source_type, source_id, vault_id,
                section_path, content, chunk_index
            ) VALUES($1, 'document', $2, $3, '', $4, $5)
            """,
            uuid.UUID(int=i + 1),
            uuid.uuid4(),
            vault_id,
            content,
            i,
        )


class _Tokenizer:
    """Counting substitute for `_tokenize_uncached`, optionally fatal.

    `fail_after` counts documents, not batches, so a run can be cut in the
    middle of a batch — the case that decides whether a half-applied batch can
    leave the cursor and the term counts disagreeing.
    """

    def __init__(self, fail_after: int | None = None):
        self.calls = 0
        self._fail_after = fail_after

    async def __call__(self, text: str) -> list[str]:
        self.calls += 1
        if self._fail_after is not None and self.calls > self._fail_after:
            raise RuntimeError("tokenizer died mid-corpus")
        return text.split()


async def _run_state(pool) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM bm25_recompute_run WHERE id = 1")
    return dict(row) if row else None


async def _published(pool) -> dict:
    async with pool.acquire() as conn:
        stats = dict(
            await conn.fetchrow(
                "SELECT total_docs, avgdl, source_revision, source_chunk_count"
                "  FROM bm25_stats WHERE id = 1"
            )
        )
        stats["df"] = {
            r["term"]: r["df"]
            for r in await conn.fetch(
                "SELECT term, df FROM bm25_vocab WHERE df > 0 ORDER BY term"
            )
        }
    return stats


async def test_an_interrupted_scan_publishes_nothing_but_keeps_its_progress(monkeypatch):
    async with _fresh_database() as pool:
        tok = _Tokenizer(fail_after=7)
        monkeypatch.setattr(sparse_encoder, "_tokenize_uncached", tok)

        with pytest.raises(RuntimeError):
            await sparse_encoder.recompute_stats(batch_size=3)

        published = await _published(pool)
        assert published["total_docs"] == 0, "a partial scan must publish nothing"
        assert published["df"] == {}

        run = await _run_state(pool)
        assert run is not None, "the interrupted run must leave its progress behind"
        assert run["cursor_chunk_id"] == uuid.UUID(int=6), (
            "the batch that raised is rolled back, so the cursor sits at the end "
            "of the last batch that committed"
        )
        assert run["total_docs"] == 6
        assert run["source_chunk_count"] == 6

        async with pool.acquire() as conn:
            partial = {
                r["term"]: r["df"]
                for r in await conn.fetch("SELECT term, df FROM bm25_recompute_terms")
            }
        # Documents 0..5 only: alpha 6, beta 3, gamma 2, delta 2.
        assert partial == {"alpha": 6, "beta": 3, "gamma": 2, "delta": 2}, (
            "the accumulator must agree with the cursor exactly — a batch's terms "
            "and its cursor commit together or not at all"
        )


async def test_the_second_pass_tokenizes_only_what_is_left(monkeypatch):
    async with _fresh_database() as pool:
        monkeypatch.setattr(sparse_encoder, "_tokenize_uncached", _Tokenizer(fail_after=7))
        with pytest.raises(RuntimeError):
            await sparse_encoder.recompute_stats(batch_size=3)

        resumed = _Tokenizer()
        monkeypatch.setattr(sparse_encoder, "_tokenize_uncached", resumed)
        result = await sparse_encoder.recompute_stats(batch_size=3)

        assert resumed.calls == 6, (
            f"resuming must tokenize the remaining six documents, not all twelve; "
            f"tokenized {resumed.calls}"
        )
        assert result["total_docs"] == 12
        assert (await _published(pool))["df"] == _EXPECTED_DF


async def test_a_resumed_run_publishes_what_an_uninterrupted_one_would(monkeypatch):
    async with _fresh_database() as pool:
        monkeypatch.setattr(sparse_encoder, "_tokenize_uncached", _Tokenizer())
        clean = await sparse_encoder.recompute_stats(batch_size=3)
        clean_published = await _published(pool)

    async with _fresh_database() as pool:
        # Each substitute counts its OWN calls, and a resumed pass only sees
        # what is left — so the second cut is at 4 of the remaining 9, not at 9
        # of the original 12. Getting this wrong makes the run finish quietly
        # and the test assert nothing.
        monkeypatch.setattr(sparse_encoder, "_tokenize_uncached", _Tokenizer(fail_after=5))
        with pytest.raises(RuntimeError):
            await sparse_encoder.recompute_stats(batch_size=3)
        assert (await _run_state(pool))["total_docs"] == 3

        monkeypatch.setattr(sparse_encoder, "_tokenize_uncached", _Tokenizer(fail_after=4))
        with pytest.raises(RuntimeError):
            await sparse_encoder.recompute_stats(batch_size=3)
        assert (await _run_state(pool))["total_docs"] == 6

        last = _Tokenizer()
        monkeypatch.setattr(sparse_encoder, "_tokenize_uncached", last)
        twice_resumed = await sparse_encoder.recompute_stats(batch_size=3)
        resumed_published = await _published(pool)
        assert last.calls == 6
        assert twice_resumed["resumed"] == 2

    assert twice_resumed["total_docs"] == clean["total_docs"] == 12
    assert twice_resumed["avgdl"] == clean["avgdl"]
    assert clean["avgdl"] == pytest.approx(_EXPECTED_TOTAL_LENGTH / 12)
    assert resumed_published["df"] == clean_published["df"] == _EXPECTED_DF


async def test_a_finished_run_leaves_no_progress_behind(monkeypatch):
    async with _fresh_database() as pool:
        monkeypatch.setattr(sparse_encoder, "_tokenize_uncached", _Tokenizer())
        await sparse_encoder.recompute_stats(batch_size=5)

        assert await _run_state(pool) is None, (
            "a completed run must clear its progress, or the next recompute "
            "would resume a corpus it already finished"
        )
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM bm25_recompute_terms") == 0


async def test_a_tokenizer_change_discards_the_partial_run_instead_of_resuming(monkeypatch):
    async with _fresh_database() as pool:
        monkeypatch.setattr(sparse_encoder, "_tokenize_uncached", _Tokenizer(fail_after=7))
        with pytest.raises(RuntimeError):
            await sparse_encoder.recompute_stats(batch_size=3)

        # The partial counts were produced by a tokenizer that no longer exists.
        monkeypatch.setattr(sparse_encoder, "tokenizer_info", lambda: ("kiwi", "99.9.9"))
        restarted = _Tokenizer()
        monkeypatch.setattr(sparse_encoder, "_tokenize_uncached", restarted)
        result = await sparse_encoder.recompute_stats(batch_size=3)

        assert restarted.calls == 12, (
            "counts from a different tokenizer are not resumable work; the run "
            f"must start over, but it tokenized only {restarted.calls}"
        )
        assert result["total_docs"] == 12
        assert (await _published(pool))["df"] == _EXPECTED_DF


async def test_the_invalidation_boundary_is_the_one_the_run_started_with(monkeypatch):
    async with _fresh_database() as pool:
        monkeypatch.setattr(sparse_encoder, "_tokenize_uncached", _Tokenizer(fail_after=7))
        with pytest.raises(RuntimeError):
            await sparse_encoder.recompute_stats(batch_size=3)
        started_with = (await _run_state(pool))["source_revision"]

        # A chunk written while the run is interrupted advances the sequence.
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE chunks SET content = content || ' epsilon' WHERE id = $1",
                uuid.UUID(int=1),
            )
            moved_to = await sparse_encoder._current_corpus_revision(conn)
        assert moved_to > started_with

        monkeypatch.setattr(sparse_encoder, "_tokenize_uncached", _Tokenizer())
        result = await sparse_encoder.recompute_stats(batch_size=3)

        assert result["source_revision"] == started_with, (
            "re-capturing the revision on resume would silently narrow the window "
            "in which writes made during the scan are revisited"
        )
        async with pool.acquire() as conn:
            stored = await conn.fetchval(
                "SELECT source_revision FROM bm25_stats WHERE id = 1"
            )
        assert int(stored) == started_with
        assert await sparse_encoder._should_recompute() is True, (
            "the next tick must still see the write that landed mid-scan"
        )


async def test_a_recompute_draws_term_ids_only_for_new_terms(monkeypatch):
    """A pass over known terms leaves `bm25_term_id_seq` where it was (akb#687).

    The recompute used to call `nextval()` for every term it saw and let
    `ON CONFLICT` discard the ids of the ones already known, so every pass
    advanced the sequence by the whole vocabulary. The BM25 index extension
    sizes its per-term arrays by the largest id, and its VACUUM cleanup walks
    all of them with the metapage locked.
    """
    async with _fresh_database() as pool:
        monkeypatch.setattr(sparse_encoder, "_tokenize_uncached", _Tokenizer())

        async def vocab_and_sequence():
            async with pool.acquire() as conn:
                vocab = {r["term"]: r["term_id"] for r in await conn.fetch("SELECT term, term_id FROM bm25_vocab")}
                return vocab, await conn.fetchval("SELECT last_value FROM bm25_term_id_seq")

        await sparse_encoder.recompute_stats(batch_size=5)
        vocab, sequence = await vocab_and_sequence()
        assert set(vocab) == set(_EXPECTED_DF)

        await sparse_encoder.recompute_stats(batch_size=5)
        assert await vocab_and_sequence() == (vocab, sequence), "a pass over known terms drew term ids"

        async with pool.acquire() as conn:
            vault_id = await conn.fetchval("SELECT vault_id FROM chunks LIMIT 1")
            await conn.execute(
                "INSERT INTO chunks(id, source_type, source_id, vault_id, section_path, content, chunk_index)"
                " VALUES($1, 'document', $2, $3, '', 'alpha epsilon', 99)",
                uuid.UUID(int=99), uuid.uuid4(), vault_id,
            )
        await sparse_encoder.recompute_stats(batch_size=5)
        after, after_sequence = await vocab_and_sequence()
        assert set(after) - set(vocab) == {"epsilon"}
        assert after_sequence == sequence + 1, "one new term, one new id"
        assert {t: after[t] for t in vocab} == vocab, "known terms keep their ids"
