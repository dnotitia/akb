"""BM25 sparse vector encoder with Kiwi (Korean morphological) tokenizer.

- Tokenization: Kiwi (한국어 형태소 분석). Only content-bearing morphemes are
  kept (nouns, verbs, foreign words, Hanja, numbers); stop-like particles are
  dropped by tag filtering.
- Vocab: each unique term gets a stable integer id in `bm25_vocab`. Ids are
  NEVER reassigned — vector-store sparse vectors reference them, so a mutation
  would corrupt every already-indexed chunk.
- External corpus stats (`bm25_stats`): N, avgdl, tokenizer version, plus
  document frequencies in `bm25_vocab`. Posting/arrays and other pre-baked
  consumers use these weights; Seahorse DB receives them at search time.
  VectorChord uses its own index statistics and does not read them for scoring.
- Query encoding goes through the same tokenizer + vocab; OOV terms are
  dropped silently.

Two doc/query weight conventions live behind the same public API,
selected by the driver and, for pgvector, the sparse shape:

  - **pre-baked** (pgvector posting/arrays, qdrant, seahorse-cloud): doc weight =
    saturated TF, query weight = IDF. The dot product yields BM25
    directly — the vector store doesn't need to know about BM25 at
    all, and pgvector's posting table just sums products.
  - **raw** (pgvector vchord, seahorse-db, seahorse-db-grpc): doc weight =
    raw TF (token count), query weight = 1.0. VectorChord computes BM25 from
    its index statistics. Seahorse DB computes BM25 from external
    (N, avgdl, df-per-query-term) metadata passed at search time.
    Sending pre-baked weights here causes double-saturation on the
    doc side AND double-IDF on the query side; the BM25 ranking
    becomes proportional to IDF² × saturated_TF instead of
    IDF × saturated_TF.

Both encodings tokenize and look up term ids identically — only the
weight definition differs. Adding a new driver means picking which of
the two it is in ``_use_raw_weights()``.

Kiwi is a hard dependency: if import or initialization fails the module
raises on first use. Falling back to a different tokenizer would produce
terms that don't match the vocab (indexed with Kiwi) and silently tank
recall — the noisy failure is intentional.
"""

from __future__ import annotations

import asyncio
import logging
import math
import multiprocessing
import os
import time
from collections import Counter, OrderedDict
from concurrent.futures import ProcessPoolExecutor
from typing import Iterable

import kiwipiepy
from kiwipiepy import Kiwi

from app.config import settings
from app.db.postgres import get_pool
from app.services import bm25_maintenance

logger = logging.getLogger("akb.sparse_encoder")


# Kiwi tag prefixes we keep as content-bearing terms.
# N* = nouns, V* = verbs/adjectives (lemma form), SL = foreign, SH = hanja, SN = number.
_KEEP_TAG_PREFIXES = ("N", "V", "SL", "SH", "SN")

# Module-level singleton; initialized on first access. Failure raises —
# sparse BM25 without Kiwi produces tokens incompatible with the indexed
# vocab, so it's strictly worse than surfacing the error.
_kiwi: Kiwi | None = None
_kiwi_version: str = kiwipiepy.__version__ if hasattr(kiwipiepy, "__version__") else "unknown"


_ENGLISH_STOPWORDS: frozenset[str] = frozenset({
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "been",
    "being",
    "by",
    "can",
    "could",
    "did",
    "do",
    "does",
    "doing",
    "for",
    "from",
    "had",
    "has",
    "have",
    "having",
    "he",
    "her",
    "hers",
    "him",
    "his",
    "how",
    "i",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "me",
    "my",
    "of",
    "on",
    "or",
    "our",
    "ours",
    "she",
    "should",
    "so",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "to",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "whom",
    "why",
    "will",
    "with",
    "would",
    "you",
    "your",
    "yours",
})


def _without_doubled_final_consonant(token: str) -> str:
    if len(token) < 3:
        return token
    if token[-1] != token[-2]:
        return token
    if token[-1] in "aeiou" or token.endswith(("ss", "ll", "zz")):
        return token
    return token[:-1]


def _english_token_variants(token: str) -> list[str]:
    """Return the original ASCII token plus conservative stem variants.

    Kiwi keeps English words as surface-form `SL` tokens, so `graduate`
    and `graduated` do not meet in the sparse BM25 leg. Keep the exact
    token for precision and add a small Porter-like variant set for common
    English inflections; non-ASCII tokens are left untouched.
    """
    if not token or not token.isascii() or not any(c.isalpha() for c in token):
        return [token]

    base = token.lower().strip("'")
    if not base:
        return []
    if base in _ENGLISH_STOPWORDS:
        return []

    variants = [base]

    def add(value: str) -> None:
        if len(value) >= 3 and value not in variants:
            variants.append(value)

    if len(base) <= 4:
        return variants

    if base.endswith("'s"):
        add(base[:-2])

    # Verb -ing / -ed inflections, independent of the plural/-s family below.
    if base.endswith("ing") and len(base) > 6:
        stem = base[:-3]
        add(stem)
        add(_without_doubled_final_consonant(stem))
        if stem.endswith(("at", "iz", "iv")):
            add(stem + "e")
    if base.endswith("ed") and len(base) > 5:
        stem = base[:-2]
        add(stem)
        add(_without_doubled_final_consonant(stem))
        if stem.endswith(("at", "iz", "iv")):
            add(stem + "e")

    # Plural / 3rd-person -s family, most specific suffix first so each word is
    # stemmed once ("churches" -> "church", never also "churche").
    if base.endswith(("ies", "ied")) and len(base) > 5:
        add(base[:-3] + "y")
    elif base.endswith("es") and len(base) > 5:
        add(base[:-2])
    elif base.endswith("s") and not base.endswith(("ss", "us", "is")):
        add(base[:-1])

    if base.endswith("e") and len(base) > 5:
        add(base[:-1])

    return variants


def _get_kiwi() -> Kiwi:
    global _kiwi
    if _kiwi is None:
        # One native Kiwi worker per process-pool child. Letting Kiwi size its
        # own native pool from host CPU count multiplies memory and threads by
        # the outer ProcessPoolExecutor size and ignores the pod budget.
        _kiwi = Kiwi(num_workers=1)
        logger.info("Kiwi tokenizer initialized (version=%s)", _kiwi_version)
    return _kiwi


def tokenizer_info() -> tuple[str, str]:
    """Return (name, version). Used for stats metadata."""
    return ("kiwi", _kiwi_version)


def _tokenize_sync(text: str) -> list[str]:
    """Pure-CPU tokenization. Runs in a worker thread via `tokenize()`."""
    if not text:
        return []
    result = _get_kiwi().tokenize(text)
    tokens: list[str] = []
    for tok in result:
        if not any(tok.tag.startswith(p) for p in _KEEP_TAG_PREFIXES):
            continue
        form = tok.form
        if form.isascii():
            tokens.extend(_english_token_variants(form))
        else:
            tokens.append(form)
    return tokens


# Bounded LRU keyed by the source text. Kiwi tokenization is the dominant
# CPU cost in indexing; even a modest hit rate (retried upserts, repeat
# queries, duplicate chunk content) avoids re-running it. Using the text
# directly as the key sidesteps any hash-collision risk; chunks are short
# enough that 2048 entries fit comfortably in memory.
_TOKEN_CACHE_MAX = 2048
_token_cache: "OrderedDict[str, list[str]]" = OrderedDict()


# ── Dedicated tokenizer process pool ──────────────────────────────────
# Kiwi's native tokenize() is CPU-bound and does NOT release the GIL (the old
# claim that it did was wrong for kiwipiepy). Running it via asyncio.to_thread
# therefore does NOT parallelize it: concurrent tokenizations serialize on the
# GIL and the worker threads starve the event-loop thread, so even /livez can't
# be scheduled → readiness/liveness probes time out → the pod is pulled from the
# Service → 503 flapping under search/indexing load. A ProcessPool gives each
# worker its own interpreter + GIL, so tokenization runs truly off the serving
# loop. Measured locally: 32 concurrent tokenizations froze the loop ~470ms via
# to_thread vs ~13ms via this pool (and ran ~4x faster).
_tokenizer_pool: ProcessPoolExecutor | None = None


def _init_tokenizer_worker() -> None:
    """Run once per child process: warm the Kiwi model so the first real
    tokenize() in that worker doesn't pay the model-load cost."""
    _get_kiwi()


def start_tokenizer_pool(processes: int | None = None) -> None:
    """Create the dedicated Kiwi tokenizer process pool. Idempotent.

    `spawn` is forced — forking a multithreaded asyncio server is unsafe. This
    is a SERVING dependency (query encode_query) as well as a worker dependency,
    so if API and indexing-worker tiers are ever split it must be started on the
    always-run path, not behind a worker gate.
    """
    global _tokenizer_pool
    if _tokenizer_pool is not None:
        return
    n = processes if (processes and processes > 0) else max(2, min(4, os.cpu_count() or 2))
    _tokenizer_pool = ProcessPoolExecutor(
        max_workers=n,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_init_tokenizer_worker,
    )
    logger.info("Kiwi tokenizer ProcessPool started (workers=%d, spawn)", n)


def stop_tokenizer_pool() -> None:
    """Tear down the tokenizer pool. Idempotent."""
    global _tokenizer_pool
    pool, _tokenizer_pool = _tokenizer_pool, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)
        logger.info("Kiwi tokenizer ProcessPool stopped")


async def tokenize(text: str) -> list[str]:
    """Tokenize text into content-bearing terms (lemma form for verbs).

    Kiwi is a sync C++ tokenizer that does NOT release the GIL during native
    work, so running it on the loop — or via asyncio.to_thread, which shares the
    loop process's GIL — starves request handling and health probes. We offload
    to a dedicated ProcessPool (each worker has its own GIL) when one is running;
    otherwise (tests / pool not started) we fall back to a thread. Result is
    LRU-cached by source text.
    """
    if not text:
        return []
    cached = _token_cache.get(text)
    if cached is not None:
        _token_cache.move_to_end(text)
        return cached
    pool = _tokenizer_pool
    if pool is not None:
        loop = asyncio.get_running_loop()
        tokens = await loop.run_in_executor(pool, _tokenize_sync, text)
    else:
        # No pool (tests / not started): thread fallback — correct but GIL-bound.
        tokens = await asyncio.to_thread(_tokenize_sync, text)
    _token_cache[text] = tokens
    _token_cache.move_to_end(text)
    while len(_token_cache) > _TOKEN_CACHE_MAX:
        _token_cache.popitem(last=False)
    return tokens


async def _tokenize_uncached(text: str) -> list[str]:
    """Tokenize without retaining source text/results in the API-process LRU.

    Corpus recomputation walks every chunk exactly once, so caching those rows
    has no reuse value and leaves the last 2,048 potentially-large chunks live
    in the worker RSS.  Query/document encoding keeps using :func:`tokenize`.
    """
    if not text:
        return []
    pool = _tokenizer_pool
    if pool is not None:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(pool, _tokenize_sync, text)
    return await asyncio.to_thread(_tokenize_sync, text)


# ── Vocab management (append-only) ────────────────────────────────


async def get_or_create_term_ids(terms: Iterable[str]) -> dict[str, int]:
    """Return {term: term_id} for given terms. New terms get fresh ids from
    the sequence. Existing terms are looked up. df is NOT incremented here —
    df/N/avgdl are rebuilt by `recompute_stats()`.

    Almost every term an encoder sees already exists, so existing terms are
    READ, not upserted. `ON CONFLICT DO UPDATE` with a no-op SET still locks
    each existing row until its transaction ends, writes a new row version and
    calls `nextval()` for every term — so encoders sharing common terms queued
    on the same rows. On a live 2.1M-chunk sweep that queue was 42% of the
    writers' sampled wait, and the vocabulary had taken 161M updates for 953k
    rows. A plain read takes no row lock; only unseen terms are inserted.
    """
    uniq = list({t for t in terms if t})
    if not uniq:
        return {}

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT term, term_id FROM bm25_vocab WHERE term = ANY($1::text[])",
            uniq,
        )
        ids = {r["term"]: int(r["term_id"]) for r in rows}
        missing = [t for t in uniq if t not in ids]
        if missing:
            # ORDER BY gives concurrent callers inserting overlapping new
            # terms one acquisition order, so they wait instead of deadlocking.
            # DO NOTHING waits for a conflicting uncommitted insert to finish
            # and then skips the term; the follow-up read below collects it.
            rows = await conn.fetch(
                """
                INSERT INTO bm25_vocab (term, term_id)
                SELECT t, nextval('bm25_term_id_seq')
                  FROM (SELECT unnest($1::text[]) AS t ORDER BY 1) src
                ON CONFLICT (term) DO NOTHING
                RETURNING term, term_id
                """,
                missing,
            )
            ids.update((r["term"], int(r["term_id"])) for r in rows)
            raced = [t for t in missing if t not in ids]
            if raced:
                rows = await conn.fetch(
                    "SELECT term, term_id FROM bm25_vocab WHERE term = ANY($1::text[])",
                    raced,
                )
                ids.update((r["term"], int(r["term_id"])) for r in rows)
    unresolved = [t for t in uniq if t not in ids]
    if unresolved:
        # The vocabulary is append-only, so this cannot happen by design. If it
        # does, refuse: `encode_document` would otherwise drop the terms and
        # store a vector that silently misses part of the document.
        raise RuntimeError(f"bm25 vocabulary lost {len(unresolved)} term(s) mid-call")
    return ids


async def lookup_term_ids(terms: Iterable[str]) -> dict[str, int]:
    """Lookup existing term_ids without creating. OOV terms are absent from result."""
    uniq = list({t for t in terms if t})
    if not uniq:
        return {}
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT term, term_id FROM bm25_vocab WHERE term = ANY($1::text[])",
            uniq,
        )
    return {r["term"]: int(r["term_id"]) for r in rows}


# ── Stats ─────────────────────────────────────────────────────────


# Stats change only when recompute_stats() runs (manual / scheduled). Caching
# for 60s eliminates a PG round-trip per chunk during indexing without risking
# meaningfully stale IDF at query time.
_STATS_TTL_SECS = 60.0
_stats_cache: tuple[float, dict] | None = None


async def load_stats() -> dict:
    global _stats_cache
    now = time.monotonic()
    if _stats_cache and (now - _stats_cache[0] < _STATS_TTL_SECS):
        return _stats_cache[1]

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT total_docs, avgdl, k1, b, tokenizer_name, tokenizer_version FROM bm25_stats WHERE id = 1"
        )
    stats = dict(row) if row else {
        "total_docs": 0, "avgdl": 0.0, "k1": settings.bm25_k1, "b": settings.bm25_b,
        "tokenizer_name": "kiwi", "tokenizer_version": "0",
    }
    _stats_cache = (now, stats)
    return stats


def _invalidate_stats_cache() -> None:
    global _stats_cache
    _stats_cache = None


async def load_df_for_terms(term_ids: Iterable[int]) -> dict[int, int]:
    """Get df (document frequency) for a set of term ids."""
    ids = list({int(t) for t in term_ids})
    if not ids:
        return {}
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT term_id, df FROM bm25_vocab WHERE term_id = ANY($1::bigint[])",
            ids,
        )
    return {int(r["term_id"]): int(r["df"]) for r in rows}


# ── BM25 sparse vector computation ────────────────────────────────


def _idf(df: int, total_docs: int) -> float:
    """BM25 IDF (Lucene variant, always >= 0 via log1p of positive ratio)."""
    if total_docs <= 0:
        return 0.0
    return math.log(1.0 + (total_docs - df + 0.5) / (df + 0.5))


# Drivers whose backing store computes BM25 internally and expects
# raw TF on the doc side + weight=1.0 on the query side. Every gRPC
# / REST transport that points at the same Coral catalog belongs in
# this set — the BM25 implementation is server-side and the same
# regardless of how the bytes got there. Forgetting to add a new
# Coral transport here resurrects the 0.7.7 double-IDF /
# double-saturation bug; the parametrised regression test in
# ``backend/tests/test_sparse_weight_convention.py`` is the gate.
_RAW_WEIGHT_DRIVERS: frozenset[str] = frozenset({
    "seahorse-db",       # REST/JSONL transport (0.7.x)
    "seahorse-db-grpc",  # gRPC transport, same Coral (0.8.0)
})


# Pgvector sparse shapes whose store computes BM25 itself. The convention is
# not a property of the driver alone once one driver can store the terms in
# more than one way: `pgvector` bakes the weights for `posting` and `arrays`
# and must NOT for `vchord`, whose index owns k1/b and recomputes the score.
_RAW_WEIGHT_SHAPES: frozenset[str] = frozenset({"vchord"})


def _use_raw_weights(sparse_shape: str | None = None) -> bool:
    """True when the active store computes BM25 internally and expects raw TF
    on the doc side + weight=1.0 on the query side.

    Keyed on the driver AND, for pgvector, on the sparse shape. Reading the
    driver alone was correct while every pgvector shape baked its weights;
    it stops being correct the moment one of them does not, and getting it
    wrong is the 0.7.7 double-saturation bug again — silently, with no error,
    just worse ranking. `test_sparse_weight_convention.py` holds the matrix.

    The shape is only consulted for `pgvector`: it is that driver's setting,
    and a value left over from a previous driver must not change what another
    driver's encoder produces."""
    if settings.vector_store_driver in _RAW_WEIGHT_DRIVERS:
        return True
    if settings.vector_store_driver == "pgvector":
        shape = sparse_shape if sparse_shape is not None else settings.effective_sparse_shape
        return shape in _RAW_WEIGHT_SHAPES
    return False


async def encode_document(
    text: str, *, sparse_shape: str | None = None
) -> tuple[list[int], list[float]]:
    """Encode a document chunk to a sparse (indices, values) tuple.

    Weight convention depends on the active driver and sparse shape (see module
    docstring). Both branches share tokenization + vocab insertion.
    """
    tokens = await tokenize(text)
    if not tokens:
        return [], []

    term_counts = Counter(tokens)
    vocab = await get_or_create_term_ids(term_counts.keys())

    if _use_raw_weights(sparse_shape):
        # Raw positive integer TF, represented as floats by the shared API.
        # VectorChord owns saturation, document length and index statistics.
        # Seahorse DB applies BM25 using metadata supplied by its driver.
        # Neither branch may pre-saturate TF or load external stats here.
        raw_indices: list[int] = []
        raw_values: list[float] = []
        for term, tf in term_counts.items():
            tid = vocab.get(term)
            if tid is not None:
                raw_indices.append(tid)
                raw_values.append(float(tf))
        return raw_indices, raw_values

    # Document encoding does not read df; IDF belongs to the query side. The
    # ids and frequencies are gathered exactly as the raw branch gathers them,
    # so a caller holding raw frequencies can derive these same weights later
    # (`saturate_for_posting`).
    indices: list[int] = []
    tfs: list[float] = []
    for term, tf in term_counts.items():
        tid = vocab.get(term)
        if tid is None:
            continue
        indices.append(tid)
        tfs.append(float(tf))
    return indices, _saturate(tfs, await load_stats(), dl=sum(term_counts.values()))


def _saturate(tfs: list[float], stats: dict, *, dl: float | None = None) -> list[float]:
    """Raw term frequencies → the pre-saturated document weights `posting` stores.

    For each term t in document d:
      doc_weight[t] = TF(t,d) * (k1 + 1) / (TF(t,d) + k1 * (1 - b + b*|d|/avgdl))
    and query_weight[t] = IDF(t), so the dot product at query time is BM25.
    total_docs is only needed for query-side IDF (see encode_query).

    `dl` is the document's token count. Left out, it is the sum of `tfs`, which
    is the same number whenever every term received an id — and
    `get_or_create_term_ids` either assigns every one or raises.
    """
    avgdl_raw = float(stats.get("avgdl") or 0)
    avgdl = avgdl_raw if avgdl_raw > 0 else 1.0
    k1 = float(stats.get("k1") or settings.bm25_k1)
    b = float(stats.get("b") or settings.bm25_b)
    length = sum(tfs) if dl is None else dl
    dl_norm = 1 - b + b * (length / avgdl)
    # tf>0, k1>0, dl_norm>0 → denom>0; no zero guard needed.
    return [float(tf * (k1 + 1) / (tf + k1 * dl_norm)) for tf in tfs]


async def saturate_for_posting(tfs: list[float]) -> list[float]:
    """The weights `posting` stores for these raw frequencies, under current stats.

    The vchord shape keeps `posting` current with this while the way back to it
    is retained (akb#615), from the frequencies it already encoded rather than
    by tokenizing the text a second time.
    """
    return _saturate(tfs, await load_stats())


async def encode_query(
    text: str, *, sparse_shape: str | None = None
) -> tuple[list[int], list[float]]:
    """Encode a query to a sparse (indices, values) tuple. OOV terms
    are dropped; no new terms are registered.

    Weight convention depends on the active driver and sparse shape (see module
    docstring).
    """
    tokens = await tokenize(text)
    if not tokens:
        return [], []
    uniq = list(set(tokens))
    vocab = await lookup_term_ids(uniq)
    if not vocab:
        return [], []

    if _use_raw_weights(sparse_shape):
        # One weight per known term, regardless of query repetition.
        # VectorChord computes IDF from its index; Seahorse DB obtains the
        # external df metadata in its search driver, not in this encoder.
        # Known terms absent from the current index still pass through.
        indices = list(vocab.values())
        values = [1.0] * len(indices)
        return indices, values

    df_map = await load_df_for_terms(vocab.values())
    stats = await load_stats()
    total_docs = int(stats.get("total_docs") or 0)
    if total_docs <= 0:
        return list(vocab.values()), [1.0] * len(vocab)

    indices_idf: list[int] = []
    values_idf: list[float] = []
    for term, tid in vocab.items():
        df = df_map.get(tid, 0)
        w = _idf(df, total_docs)
        if w <= 0:
            continue
        indices_idf.append(tid)
        values_idf.append(float(w))
    return indices_idf, values_idf


# ── Corpus stats recompute ────────────────────────────────────────


# Keep the private name as a rolling-compatibility alias for callers/tests
# that still import it. The value is owned by the shared maintenance module.
_BM25_RECOMPUTE_LOCK_KEY = bm25_maintenance.BM25_RECOMPUTE_LOCK_KEY


async def _open_run(conn, tname: str, tver: str) -> dict:
    """Resume the run in flight, or start a new one, returning its state.

    A partial run is resumable only while it means the same thing. The
    tokenizer identity is what decides that: counts produced by a different
    tokenizer describe a different vocabulary, so they are discarded rather
    than added to. Nothing else disqualifies a resume — in particular age does
    not, because the run carries the `source_revision` it started with and a
    scan that finishes late publishes stats the very next tick will recompute.

    That revision is deliberately NOT re-captured here. It is the boundary that
    makes writes landing during the scan get revisited; moving it forward on
    resume would silently narrow the window and let a mid-scan write go
    uncounted until something else happened to touch the corpus.
    """
    row = await conn.fetchrow("SELECT * FROM bm25_recompute_run WHERE id = 1")
    if (
        row is not None
        and row["tokenizer_name"] == tname
        and row["tokenizer_version"] == tver
    ):
        resumed = int(row["resumed"]) + 1
        await conn.execute(
            "UPDATE bm25_recompute_run SET resumed = $1, updated_at = NOW()"
            " WHERE id = 1",
            resumed,
        )
        logger.info(
            "BM25 recompute resuming: %d documents already counted "
            "(resume #%d, started %s)",
            int(row["total_docs"]),
            resumed,
            row["started_at"].isoformat() if row["started_at"] else "?",
        )
        return dict(row) | {"resumed": resumed}

    async with conn.transaction():
        await conn.execute("TRUNCATE bm25_recompute_terms")
        fresh = await conn.fetchrow(
            """
            INSERT INTO bm25_recompute_run (
                id, tokenizer_name, tokenizer_version, source_revision
            ) VALUES (1, $1, $2, $3)
            ON CONFLICT (id) DO UPDATE SET
                tokenizer_name     = EXCLUDED.tokenizer_name,
                tokenizer_version  = EXCLUDED.tokenizer_version,
                source_revision    = EXCLUDED.source_revision,
                cursor_chunk_id    = NULL,
                total_docs         = 0,
                total_length       = 0,
                source_chunk_count = 0,
                resumed            = 0,
                started_at         = NOW(),
                updated_at         = NOW()
            RETURNING *
            """,
            tname,
            tver,
            await _current_corpus_revision(conn),
        )
    return dict(fresh)


async def recompute_stats(
    batch_size: int = 500,
    *,
    defer_if_vector_queue: bool = False,
) -> dict:
    """Rebuild df (per term) and (total_docs, avgdl). Safe to run repeatedly.

    Streams chunks in keyset-paginated batches and accumulates document
    frequency in PostgreSQL.  The old process-global ``Counter`` retained every
    unique term until the end of the scan, so the function described itself as
    bounded while backend RSS still grew with corpus vocabulary.  Only one
    batch's terms live in Python memory.

    **The scan is resumable; the publish is not, and that asymmetry is the
    point** (akb#616).  Each batch commits its term contributions and its
    advanced cursor in ONE transaction, so an interruption — a deploy, an OOM,
    a dropped connection — costs one batch instead of the whole corpus.  The
    final write stays atomic because half a corpus's df is worse than none, and
    it is seconds of work against hours of scan.

    Held under a session-scoped PG advisory lock for the duration of the
    scan AND write. Without this, two replicas would each spend minutes
    tokenizing the corpus and then race on the final UPDATE — wasting
    CPU and letting the loser overwrite newer counts with older ones.
    `pg_try_advisory_lock` makes the loser bail out cheaply instead of
    waiting for the leader to finish.  With a durable cursor it does one more
    thing: the replica that takes the lock next continues the departing one's
    scan rather than starting over.

    ``defer_if_vector_queue`` is enabled by the background refresher. Direct
    callers retain the historical manual/initialization behavior and may
    intentionally rebuild stats while chunks are waiting for vector indexing.
    """
    pool = await get_pool()
    tname, tver = tokenizer_info()

    # Hold one connection for the whole call so the session-scoped
    # advisory lock outlives every batch SELECT. Releasing on conn close.
    async with pool.acquire() as lock_conn:
        got = await lock_conn.fetchval(
            "SELECT pg_try_advisory_lock($1)", _BM25_RECOMPUTE_LOCK_KEY
        )
        if not got:
            logger.info(
                "BM25 recompute skipped: another replica holds the lock"
            )
            return {
                "total_docs": None,
                "avgdl": None,
                "vocab_size": None,
                "tokenizer": f"{tname}@{tver}",
                "skipped": True,
                "skip_reason": bm25_maintenance.BM25_RECOMPUTE_SKIP_REASON_LOCK_HELD,
            }
        try:
            # Take the exclusive legacy lock before observing the queue. The
            # bulk maintenance guard holds a shared lock on this same key, so
            # this ordering keeps the queue check and the start of the scan
            # inside the race-free bulk exclusion boundary.
            if (
                defer_if_vector_queue
                and await bm25_maintenance.vector_upsert_queue_nonempty(lock_conn)
            ):
                logger.info(
                    "BM25 recompute deferred: vector upsert queue is nonempty"
                )
                return {
                    "total_docs": None,
                    "avgdl": None,
                    "vocab_size": None,
                    "tokenizer": f"{tname}@{tver}",
                    "skipped": True,
                    "skip_reason": (
                        bm25_maintenance.BM25_RECOMPUTE_SKIP_REASON_VECTOR_QUEUE
                    ),
                }
            # The invalidation boundary belongs to the RUN, not to this call.
            # Chunk writes that commit while we are walking the corpus advance
            # the sequence past this value, so a later tick will conservatively
            # revisit them even if READ COMMITTED happened to expose some of
            # those rows — and a resumed run keeps the boundary it started
            # with, which is what makes that guarantee survive a restart.
            run = await _open_run(lock_conn, tname, tver)
            source_revision = int(run["source_revision"])
            source_chunk_count = int(run["source_chunk_count"])
            total_docs = int(run["total_docs"])
            total_length = int(run["total_length"])
            resumed = int(run["resumed"])

            last_id = run["cursor_chunk_id"]
            while True:
                if last_id is None:
                    rows = await lock_conn.fetch(
                        "SELECT id, content FROM chunks WHERE content IS NOT NULL "
                        "ORDER BY id LIMIT $1",
                        batch_size,
                    )
                else:
                    rows = await lock_conn.fetch(
                        "SELECT id, content FROM chunks WHERE content IS NOT NULL AND id > $1 "
                        "ORDER BY id LIMIT $2",
                        last_id, batch_size,
                    )
                if not rows:
                    break
                batch_df_counts: Counter[str] = Counter()
                for r in rows:
                    last_id = r["id"]
                    source_chunk_count += 1
                    toks = await _tokenize_uncached(r["content"] or "")
                    if not toks:
                        continue
                    total_docs += 1
                    total_length += len(toks)
                    for term in set(toks):
                        batch_df_counts[term] += 1

                # One transaction, both writes. Splitting them would let a
                # crash land the terms without the cursor (double-counting the
                # batch on resume) or the cursor without the terms (losing it).
                async with lock_conn.transaction():
                    if batch_df_counts:
                        terms, counts = zip(*batch_df_counts.items())
                        await lock_conn.execute(
                            """
                            INSERT INTO bm25_recompute_terms AS aggregate (term, df)
                            SELECT * FROM unnest($1::text[], $2::bigint[])
                            ON CONFLICT (term) DO UPDATE
                                SET df = aggregate.df + EXCLUDED.df
                            """,
                            list(terms),
                            list(counts),
                        )
                    await lock_conn.execute(
                        """
                        UPDATE bm25_recompute_run
                           SET cursor_chunk_id    = $1,
                               total_docs         = $2,
                               total_length       = $3,
                               source_chunk_count = $4,
                               updated_at         = NOW()
                         WHERE id = 1
                        """,
                        last_id,
                        total_docs,
                        total_length,
                        source_chunk_count,
                    )

            avgdl = (total_length / total_docs) if total_docs else 0.0
            vocab_count = int(
                await lock_conn.fetchval(
                    "SELECT COUNT(*) FROM bm25_recompute_terms"
                ) or 0
            )

            async with lock_conn.transaction():
                # Ensure all encountered terms have stable vocab ids.  Reading
                # directly from the temp aggregate avoids materialising the
                # corpus vocabulary in Python a second time.
                #
                # Only terms the vocabulary lacks reach `nextval()`.
                # `ON CONFLICT DO NOTHING` alone evaluates the select list, and
                # so draws an id, for every term it then discards: each pass
                # advanced `bm25_term_id_seq` by the whole vocabulary. The index
                # extension sizes its per-term arrays by the largest id, not by
                # the number of terms, and its VACUUM cleanup walks all of them;
                # one install had 729M ids for 955k terms (akb#687). A term an
                # encoder inserts concurrently still collides and costs one id.
                if vocab_count:
                    await lock_conn.execute(
                        """
                        INSERT INTO bm25_vocab (term, term_id)
                        SELECT r.term, nextval('bm25_term_id_seq')
                          FROM bm25_recompute_terms r
                         WHERE NOT EXISTS (
                                 SELECT 1 FROM bm25_vocab v WHERE v.term = r.term
                               )
                         ORDER BY r.term
                        ON CONFLICT (term) DO NOTHING
                        """
                    )

                # Two-step reset preserves append-only term ids while making
                # terms absent from the current corpus explicitly zero.
                await lock_conn.execute("UPDATE bm25_vocab SET df = 0, updated_at = NOW()")
                if vocab_count:
                    await lock_conn.execute(
                        """
                        UPDATE bm25_vocab v
                           SET df = c.df,
                               updated_at = NOW()
                          FROM bm25_recompute_terms c
                         WHERE v.term = c.term
                        """
                    )

                await lock_conn.execute(
                    """
                    UPDATE bm25_stats
                       SET total_docs = $1,
                           avgdl = $2,
                           tokenizer_name = $3,
                           tokenizer_version = $4,
                           source_revision = $5,
                           source_chunk_count = $6,
                           updated_at = NOW()
                     WHERE id = 1
                    """,
                    total_docs, avgdl, tname, tver,
                    source_revision, source_chunk_count,
                )

                # Clearing the run inside the publishing transaction is what
                # makes "published" and "no longer resumable" the same event.
                # Outside it, a failed publish would leave no progress to
                # resume from, which is the defect this whole path removes.
                await lock_conn.execute("DELETE FROM bm25_recompute_run WHERE id = 1")
                await lock_conn.execute("TRUNCATE bm25_recompute_terms")

            _invalidate_stats_cache()
            logger.info(
                "BM25 stats recomputed: source_chunks=%d total_docs=%d "
                "avgdl=%.2f vocab_size=%d source_revision=%d resumed=%d",
                source_chunk_count,
                total_docs,
                avgdl,
                vocab_count,
                source_revision,
                resumed,
            )
            return {
                "total_docs": total_docs,
                "avgdl": avgdl,
                "vocab_size": vocab_count,
                "tokenizer": f"{tname}@{tver}",
                "source_revision": source_revision,
                "source_chunk_count": source_chunk_count,
                "resumed": resumed,
            }
        finally:
            # Nothing is dropped here any more. Whatever the scan reached is
            # the next process's starting point.
            await lock_conn.execute(
                "SELECT pg_advisory_unlock($1)", _BM25_RECOMPUTE_LOCK_KEY
            )


async def vocab_size() -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval("SELECT COUNT(*) FROM bm25_vocab")
    return int(n or 0)


# ── Stats refresher background task ───────────────────────────────
#
# `recompute_stats()` rebuilds external N/avgdl/df for consumers named by
# settings.bm25_external_stats_consumers. VChord owns its own index statistics;
# its vocab term-ID registration remains in the encoding path even when the
# external refresher is disabled. Keep this refresher for posting rollback.

# Skip a tick until this many source-corpus mutations have accumulated since
# the last recompute.  The mutation sequence tracks inserts, deletes, and
# content changes without conflating raw source chunks with token-bearing BM25
# documents (the old count comparison did exactly that).
_BM25_RECOMPUTE_DELTA_THRESHOLD = 50


async def _current_corpus_revision(conn) -> int:
    """Return the sequence's logical revision (zero before first mutation)."""
    row = await conn.fetchrow(
        "SELECT last_value, is_called FROM bm25_corpus_revision_seq"
    )
    if not row or not row["is_called"]:
        return 0
    return int(row["last_value"])


def _state_requires_recompute(
    row,
    current_revision: int,
    *,
    live_chunk_count: int | None = None,
) -> bool:
    """Pure refresh decision shared by the periodic and startup gates.

    ``live_chunk_count`` is intentionally optional.  A steady-state tick needs
    only the O(1) sequence read; the full COUNT is a fallback for a small
    revision delta so a one-statement TRUNCATE is still detected.
    """
    if not row:
        return True
    if (
        row["tokenizer_name"] != "kiwi"
        or row["tokenizer_version"] != _kiwi_version
    ):
        return True

    stored_revision = int(row["source_revision"] or 0)
    if current_revision < stored_revision:
        # A restored/reset sequence must never make stale stats look current.
        return True
    delta = current_revision - stored_revision
    if delta == 0:
        return False
    if delta >= _BM25_RECOMPUTE_DELTA_THRESHOLD:
        return True

    source_chunk_count = int(row["source_chunk_count"] or 0)
    if source_chunk_count < _BM25_RECOMPUTE_DELTA_THRESHOLD:
        # Small corpora are cheap and need useful IDF immediately rather than
        # waiting until they happen to accumulate fifty mutations.
        return True
    if live_chunk_count is not None:
        return (
            abs(live_chunk_count - source_chunk_count)
            >= _BM25_RECOMPUTE_DELTA_THRESHOLD
        )
    return False


async def _should_recompute() -> bool:
    """True when tokenizer identity or source-corpus revision is stale."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT source_revision, source_chunk_count,
                   tokenizer_name, tokenizer_version
              FROM bm25_stats
             WHERE id = 1
            """
        )
        current_revision = await _current_corpus_revision(conn)
        if _state_requires_recompute(row, current_revision):
            return True

        # A sub-threshold revision delta normally waits for more changes.  The
        # only exception is a large set-based mutation represented by one
        # revision (notably TRUNCATE), detected by this conditional count.
        stored_revision = int(row["source_revision"] or 0) if row else 0
        if current_revision != stored_revision:
            live = int(await conn.fetchval("SELECT COUNT(*) FROM chunks") or 0)
            return _state_requires_recompute(
                row,
                current_revision,
                live_chunk_count=live,
            )
    return False


# A skip costs a whole `idle_secs` (six hours), and there is exactly one case
# where that is the wrong price: a rolling restart. The replaced pod keeps its
# PostgreSQL session -- and therefore the advisory lock -- until it finishes
# draining, so the replacement's first tick can find the lock held by a process
# that is already leaving. Retrying across that window costs a few seconds and
# saves an interval.
#
# It is deliberately bounded and short. A lock still held after this is held by
# a recompute that is genuinely running on another replica, and skipping THAT is
# correct -- it will publish the stats this tick wanted. We are outlasting a
# handover, not waiting out a peer's work.
_SKIPPED_RETRY_SECS = 20.0
_SKIPPED_RETRIES = 3


async def _refresh_tick(retry_secs: float = _SKIPPED_RETRY_SECS) -> int:
    """One refresher iteration. Returns 0 so `BackfillRunner` always
    treats us as idle and respects the configured `idle_secs` cadence
    rather than busy-looping.

    `BackfillRunner` has two outcomes: 0 sleeps for the configured interval,
    anything else drains immediately. Neither fits "could not run, try again
    shortly", and `configure_idle_secs` refuses while the runner is live, so
    the wait belongs here.
    """
    if not settings.bm25_external_stats_consumers:
        return 0
    if not await _should_recompute():
        return 0
    last_skip_reason = bm25_maintenance.BM25_RECOMPUTE_SKIP_REASON_LOCK_HELD
    for attempt in range(_SKIPPED_RETRIES + 1):
        outcome = await recompute_stats(defer_if_vector_queue=True)
        if not outcome.get("skipped"):
            return 0
        reason = outcome.get("skip_reason")
        if reason in bm25_maintenance.BM25_RECOMPUTE_SKIP_REASONS:
            last_skip_reason = reason
        if attempt < _SKIPPED_RETRIES:
            await asyncio.sleep(retry_secs)
    logger.info(
        "BM25 recompute deferred after retries: %s", last_skip_reason
    )
    return 0


from app.services._backfill import BackfillRunner  # noqa: E402

_refresher = BackfillRunner("bm25_stats_refresher", _refresh_tick, idle_secs=1)


def start_stats_refresher(interval_secs: int = 1800) -> None:
    """Launch the periodic stats refresher. Idempotent.

    ``BackfillRunner`` executes one tick immediately before its first sleep, so
    the startup path naturally uses the same tokenizer/revision gate as every
    periodic tick.  A fresh or tokenizer-changed database recomputes promptly,
    while restarting a stable 960k-chunk deployment performs no corpus scan.
    """
    if not settings.bm25_external_stats_consumers:
        logger.info("External BM25 stats refresher disabled: verified VChord-only deployment")
        return
    if _refresher.is_running():
        return
    _refresher.configure_idle_secs(interval_secs)
    _refresher.start()


async def stop_stats_refresher() -> None:
    """Signal stop and await the runner. Safe to call when not started."""
    if _refresher is not None:
        await _refresher.stop()


async def _run_progress(conn) -> dict | None:
    """The scan in flight, or None when no run is open.

    Without this, "is the recompute progressing or restarting" is only
    answerable from log lines, which live as long as the pod — and a pod
    restart is precisely the event in question (akb#616). `chunks_scanned`
    measures against the `source_chunk_count` already in this snapshot.
    """
    row = await conn.fetchrow(
        """
        SELECT total_docs, source_chunk_count, resumed, started_at, updated_at
          FROM bm25_recompute_run WHERE id = 1
        """
    )
    if row is None:
        return None
    return {
        "documents_counted": int(row["total_docs"] or 0),
        "chunks_scanned": int(row["source_chunk_count"] or 0),
        "resumed": int(row["resumed"] or 0),
        "started_at": row["started_at"].isoformat() if row["started_at"] else None,
        "advanced_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


def external_stats_policy_snapshot() -> dict:
    """Process policy, separate from shared DB success/progress observations."""
    consumers = settings.bm25_external_stats_consumers
    return {
        "mode": settings.bm25_external_stats_mode,
        "required": bool(consumers),
        "consumers": consumers,
        "refresher_running_in_this_process": _refresher.is_running(),
    }


async def stats_snapshot() -> dict:
    """Operator-facing snapshot of BM25 corpus stats. Surfaced by /health
    so a stuck refresher (total_docs=0 while chunks exist) is visible."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT total_docs, avgdl, tokenizer_name, tokenizer_version,
                   source_revision, source_chunk_count, updated_at
              FROM bm25_stats WHERE id = 1
            """
        )
        vocab = await conn.fetchval("SELECT COUNT(*) FROM bm25_vocab")
        current_revision = await _current_corpus_revision(conn)
        recompute = await _run_progress(conn)
        recompute_active = await bm25_maintenance.active_bm25_recompute(conn)
    if not row:
        return {
            "external_stats": external_stats_policy_snapshot(),
            "total_docs": 0, "avgdl": 0.0,
            "tokenizer": "kiwi@0",
            "vocab_size": int(vocab or 0),
            "source_chunk_count": 0,
            "source_revision": 0,
            "current_revision": current_revision,
            "pending_changes": current_revision,
            "last_recomputed_at": None,
            "recompute_in_flight": recompute,
            "recompute_active": recompute_active,
        }
    source_revision = int(row["source_revision"] or 0)
    return {
        "external_stats": external_stats_policy_snapshot(),
        "total_docs": int(row["total_docs"] or 0),
        "avgdl": float(row["avgdl"] or 0.0),
        "tokenizer": f"{row['tokenizer_name']}@{row['tokenizer_version']}",
        "vocab_size": int(vocab or 0),
        "source_chunk_count": int(row["source_chunk_count"] or 0),
        "source_revision": source_revision,
        "current_revision": current_revision,
        "pending_changes": max(0, current_revision - source_revision),
        "last_recomputed_at": (
            row["updated_at"].isoformat() if row["updated_at"] else None
        ),
        "recompute_in_flight": recompute,
        "recompute_active": recompute_active,
    }
