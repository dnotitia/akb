"""BM25 sparse vector encoder with Kiwi (Korean morphological) tokenizer.

- Tokenization: Kiwi (한국어 형태소 분석). Only content-bearing morphemes are
  kept (nouns, verbs, foreign words, Hanja, numbers); stop-like particles are
  dropped by tag filtering.
- Vocab: each unique term gets a stable integer id in `bm25_vocab`. Ids are
  NEVER reassigned — vector-store sparse vectors reference them, so a mutation
  would corrupt every already-indexed chunk.
- Corpus stats (`bm25_stats`): N, avgdl, tokenizer version. Can lag reality;
  quality degrades slightly until `recompute_stats()` runs.
- Query encoding goes through the same tokenizer + vocab; OOV terms are
  dropped silently.

Kiwi is a hard dependency: if import or initialization fails the module
raises on first use. Falling back to a different tokenizer would produce
terms that don't match the vocab (indexed with Kiwi) and silently tank
recall — the noisy failure is intentional.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import Counter, OrderedDict
from typing import Iterable

import kiwipiepy
from kiwipiepy import Kiwi

from app.config import settings
from app.db.postgres import get_pool

logger = logging.getLogger("akb.sparse_encoder")


# Kiwi tag prefixes we keep as content-bearing terms.
# N* = nouns, V* = verbs/adjectives (lemma form), SL = foreign, SH = hanja, SN = number.
_KEEP_TAG_PREFIXES = ("N", "V", "SL", "SH", "SN")

# Module-level singleton; initialized on first access. Failure raises —
# sparse BM25 without Kiwi produces tokens incompatible with the indexed
# vocab, so it's strictly worse than surfacing the error.
_kiwi: Kiwi | None = None
_kiwi_version: str = kiwipiepy.__version__ if hasattr(kiwipiepy, "__version__") else "unknown"


def _get_kiwi() -> Kiwi:
    global _kiwi
    if _kiwi is None:
        _kiwi = Kiwi()
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
        tokens.append(form.lower() if form.isascii() else form)
    return tokens


# Bounded LRU keyed by the source text. Kiwi tokenization is the dominant
# CPU cost in indexing; even a modest hit rate (retried upserts, repeat
# queries, duplicate chunk content) avoids re-running it. Using the text
# directly as the key sidesteps any hash-collision risk; chunks are short
# enough that 2048 entries fit comfortably in memory.
_TOKEN_CACHE_MAX = 2048
_token_cache: "OrderedDict[str, list[str]]" = OrderedDict()


async def tokenize(text: str) -> list[str]:
    """Tokenize text into content-bearing terms (lemma form for verbs).

    Async because Kiwi is sync C++ and would otherwise block the event loop;
    we offload to a worker thread (Kiwi releases the GIL during native work).
    Result is LRU-cached by source text.
    """
    if not text:
        return []
    cached = _token_cache.get(text)
    if cached is not None:
        _token_cache.move_to_end(text)
        return cached
    tokens = await asyncio.to_thread(_tokenize_sync, text)
    _token_cache[text] = tokens
    _token_cache.move_to_end(text)
    while len(_token_cache) > _TOKEN_CACHE_MAX:
        _token_cache.popitem(last=False)
    return tokens


# ── Vocab management (append-only) ────────────────────────────────


async def get_or_create_term_ids(terms: Iterable[str]) -> dict[str, int]:
    """Return {term: term_id} for given terms. New terms get fresh ids from
    the sequence. Existing terms are looked up. df is NOT incremented here —
    df/N/avgdl are rebuilt by `recompute_stats()`.
    """
    uniq = list({t for t in terms if t})
    if not uniq:
        return {}

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Upsert: existing rows stay untouched (including their term_id);
        # new rows get a fresh id from the sequence.
        rows = await conn.fetch(
            """
            INSERT INTO bm25_vocab (term, term_id)
            SELECT t, nextval('bm25_term_id_seq')
              FROM unnest($1::text[]) AS t
            ON CONFLICT (term) DO UPDATE
                SET updated_at = bm25_vocab.updated_at   -- no-op, returns existing row
            RETURNING term, term_id
            """,
            uniq,
        )
    return {r["term"]: int(r["term_id"]) for r in rows}


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


async def encode_document(text: str) -> tuple[list[int], list[float]]:
    """Encode a document chunk to a BM25 sparse vector (indices, values).
    Appends new terms to the vocab as a side effect. For query-time use
    `encode_query` which never touches the vocab.
    """
    tokens = await tokenize(text)
    if not tokens:
        return [], []

    term_counts = Counter(tokens)
    vocab = await get_or_create_term_ids(term_counts.keys())

    stats = await load_stats()
    total_docs = int(stats.get("total_docs") or 0)
    avgdl_raw = float(stats.get("avgdl") or 0)
    avgdl = avgdl_raw if avgdl_raw > 0 else 1.0
    k1 = float(stats.get("k1") or settings.bm25_k1)
    b = float(stats.get("b") or settings.bm25_b)

    # For documents we store TF pre-saturated with the BM25 saturation so
    # Dot product at query time yields BM25 score. Specifically for
    # each term t in doc d:
    #   doc_weight[t] = TF(t,d) * (k1 + 1) / (TF(t,d) + k1 * (1 - b + b*|d|/avgdl))
    # and query_weight[t] = IDF(t). Then dot = Σ IDF(t) * sat_TF(t,d) = BM25.
    dl = sum(term_counts.values())
    dl_norm = 1 - b + b * (dl / avgdl)

    # df for the doc's terms is looked up against CURRENT vocab df (fine for
    # indexing — not used at doc encoding time since we separate weights).
    indices: list[int] = []
    values: list[float] = []
    for term, tf in term_counts.items():
        tid = vocab.get(term)
        if tid is None:
            continue
        # tf>0 (Counter), k1>0, dl_norm>0 → denom>0, sat_tf>0; no zero guard needed.
        sat_tf = tf * (k1 + 1) / (tf + k1 * dl_norm)
        indices.append(tid)
        values.append(float(sat_tf))
    return indices, values


async def encode_query(text: str) -> tuple[list[int], list[float]]:
    """Encode a query to BM25 sparse vector. Uses IDF as term weight; OOV
    terms are dropped. No new terms are registered.

    Common terms are kept — BM25 IDF already down-weights them. Over-
    aggressive filtering caused false negatives on legitimate informative
    words. The sparse-prefetch gate in VectorStore.hybrid_search handles
    nonsense queries without hurting recall here.
    """
    tokens = await tokenize(text)
    if not tokens:
        return [], []
    uniq = list(set(tokens))
    vocab = await lookup_term_ids(uniq)
    if not vocab:
        return [], []

    df_map = await load_df_for_terms(vocab.values())
    stats = await load_stats()
    total_docs = int(stats.get("total_docs") or 0)
    if total_docs <= 0:
        return list(vocab.values()), [1.0] * len(vocab)

    indices: list[int] = []
    values: list[float] = []
    for term, tid in vocab.items():
        df = df_map.get(tid, 0)
        w = _idf(df, total_docs)
        if w <= 0:
            continue
        indices.append(tid)
        values.append(float(w))
    return indices, values


# ── Corpus stats recompute ────────────────────────────────────────


async def recompute_stats(batch_size: int = 500) -> dict:
    """Rebuild df (per term) and (total_docs, avgdl). Safe to run repeatedly.

    Streams chunks in keyset-paginated batches to keep memory bounded even
    on large corpora.
    """
    pool = await get_pool()
    tname, tver = tokenizer_info()

    total_docs = 0
    total_length = 0
    df_counts: Counter[str] = Counter()

    last_id = None
    while True:
        async with pool.acquire() as conn:
            if last_id is None:
                rows = await conn.fetch(
                    "SELECT id, content FROM chunks WHERE content IS NOT NULL "
                    "ORDER BY id LIMIT $1",
                    batch_size,
                )
            else:
                rows = await conn.fetch(
                    "SELECT id, content FROM chunks WHERE content IS NOT NULL AND id > $1 "
                    "ORDER BY id LIMIT $2",
                    last_id, batch_size,
                )
        if not rows:
            break
        for r in rows:
            last_id = r["id"]
            toks = await tokenize(r["content"] or "")
            if not toks:
                continue
            total_docs += 1
            total_length += len(toks)
            for term in set(toks):
                df_counts[term] += 1

    avgdl = (total_length / total_docs) if total_docs else 0.0

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Ensure all encountered terms have vocab ids. Assign in one batch.
            if df_counts:
                await conn.execute(
                    """
                    INSERT INTO bm25_vocab (term, term_id)
                    SELECT t, nextval('bm25_term_id_seq')
                      FROM unnest($1::text[]) AS t
                    ON CONFLICT (term) DO NOTHING
                    """,
                    list(df_counts.keys()),
                )

            # Two-step reset: (1) zero every row, (2) set counts for present
            # terms from a single unnest. Replaces a full-table UPDATE + N
            # executemany (one round-trip per term) with exactly two queries.
            await conn.execute("UPDATE bm25_vocab SET df = 0, updated_at = NOW()")
            if df_counts:
                terms, counts = zip(*df_counts.items())
                await conn.execute(
                    """
                    UPDATE bm25_vocab v
                       SET df = c.cnt,
                           updated_at = NOW()
                      FROM unnest($1::text[], $2::bigint[]) AS c(term, cnt)
                     WHERE v.term = c.term
                    """,
                    list(terms), list(counts),
                )

            await conn.execute(
                """
                UPDATE bm25_stats
                   SET total_docs = $1,
                       avgdl = $2,
                       tokenizer_name = $3,
                       tokenizer_version = $4,
                       updated_at = NOW()
                 WHERE id = 1
                """,
                total_docs, avgdl, tname, tver,
            )

    _invalidate_stats_cache()
    logger.info("BM25 stats recomputed: total_docs=%d avgdl=%.2f vocab_size=%d",
                total_docs, avgdl, len(df_counts))
    return {
        "total_docs": total_docs,
        "avgdl": avgdl,
        "vocab_size": len(df_counts),
        "tokenizer": f"{tname}@{tver}",
    }


async def vocab_size() -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval("SELECT COUNT(*) FROM bm25_vocab")
    return int(n or 0)
