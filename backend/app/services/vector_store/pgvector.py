"""Pgvector driver for VectorStore.

Stores dense + corpus-side BM25 sparse vectors in a Postgres schema
(`vector_index` by default). RRF fusion happens application-side over
two SQL queries (dense KNN + BM25 sum) executed in parallel.

Operator deployment modes (no code change between them):

- Same-instance: `vector_store_dsn` blank → driver uses the main PG
  pool. Main PG must have the `vector` extension installed; the driver
  creates its own schema, so the main `chunks` table is untouched.
- Separate-instance: `vector_store_dsn` set to a different Postgres
  URL → driver opens a dedicated pool. The main PG never gains a
  vector dependency.

Sparse storage shape is selected at construction time. With the default
`vector_store_sparse_shape: auto`, startup decides it per database before the
store is built (`sparse_shape_state.py`), and `_do_ensure` records the shape it
set up so the decision is stable:

  vchord   — raw integer TF in bm25vector; the BM25 index owns scoring
             and corpus statistics. What a new database gets where the
             server provides `vchord_bm25`.
  posting  — chunks(...) + posting(term_id, chunk_id, weight),
             B-tree-indexed on term_id. Sparse search is a single
             indexed lookup with application-owned BM25 weights. What a
             server without the extension gets, and what every existing
             installation keeps until it runs the backfill runbook.
  arrays   — chunks(sparse_terms BIGINT[], sparse_weights REAL[]).
             One row per chunk. Sparse search unnest+JOIN+GROUP BY.
             RETAINED for the bench harness only — don't pick this
             for production. May be removed in a future cleanup.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import assert_never

import asyncpg

from app.services import sparse_encoder
from app.services.sparse_shapes import SparseShape

from .base import ChunkUpsert, VectorHit, VectorSearchDegraded, VectorStoreUnavailable, has_dense
from .posting_search import MAX_POINT_CHUNKS, MAX_POINT_PROBES, estimate_scope_strategy
from .sparse_shape_state import record_sparse_shape


def _advisory_lock_key(schema: str) -> int:
    """Stable PG ``bigint`` key for the per-schema ensure_collection
    advisory lock. Cross-process: any worker computing the same
    schema string gets the same key. Signed so it fits the PG
    ``bigint`` parameter that ``pg_advisory_xact_lock`` expects."""
    digest = hashlib.blake2b(
        f"akb:vector_store:ensure_collection:{schema}".encode("utf-8"),
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, "big", signed=True)

logger = logging.getLogger("akb.vector_store.pgvector")


# RRF constant (Qdrant's default). Same value across drivers so the
# `score` field has consistent semantics — the absolute number still
# isn't comparable across drivers (per the VectorHit contract), but
# at least the formula is identical.
RRF_K = 60

_VCHORD_MAX_CANDIDATES = 65_535
# Materialising a selective scope scores every row in it, so it is kept to
# scopes of at most this many rows; a larger one is searched index-led
# (akb#626). All SQL retains the existing caller/pool budgets.
_VCHORD_MAX_MATERIALISED_ROWS = 10_000

# Search gets its own budget; writes and maintenance keep the pool's timeout.
# Reserves scale down with short budgets used by isolated serving experiments.
_SEARCH_CLEANUP_RESERVE_SECS = 0.1
_SEARCH_PAYLOAD_RESERVE_SECS = 0.2
_SEARCH_TERMINATE_GRACE_SECS = 0.01


def _terminate_search_connection(conn) -> None:
    try:
        conn.terminate()
    except asyncpg.InterfaceError:
        # A simultaneous release may already have detached the pool proxy.
        pass


async def _stop_search_tasks(tasks, connections, *, cleanup_deadline: float, terminated: set[str]) -> None:
    """Wait for cancellation/reset, then discard connections that cannot settle.

    asyncpg transactions may await ROLLBACK while unwinding cancellation. The
    supervisor, rather than that same cancelled task, owns the hard cleanup
    bound and can terminate its connection even if the network has stalled.
    """
    pending = {task for task in tasks.values() if not task.done()}
    for task in pending:
        task.cancel()
    if pending:
        _, pending = await asyncio.wait(
            pending, timeout=max(0.0, cleanup_deadline - asyncio.get_running_loop().time()),
        )
    if pending:
        for name, task in tasks.items():
            if task in pending:
                if (conn := connections.get(name)) is not None:
                    terminated.add(name)
                    _terminate_search_connection(conn)
                task.cancel()
        # Termination wakes asyncpg's protocol waiters; give their finally
        # blocks a bounded turn. This is the only grace beyond the budget.
        _, pending = await asyncio.wait(pending, timeout=_SEARCH_TERMINATE_GRACE_SECS)
    for task in tasks.values():
        if task in pending:
            # No live connection is retained. A custom/corrupt coroutine that
            # ignores cancellation must not make the request wait forever.
            logger.error("search cleanup task resisted cancellation")
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        elif not task.cancelled():
            task.exception()  # Retrieve exceptions from cancelled peers too.


async def _wait_search_tasks(
    tasks, connections, *, deadline: float, cleanup_deadline: float, cleanup_timeout: float,
    cleanup_deadlines: dict[str, float], terminated: set[str],
) -> set[str]:
    """Return timed-out names; unexpected errors/caller cancellation propagate."""
    failed = False
    try:
        done, pending = await asyncio.wait(
            tasks.values(), timeout=max(0.0, deadline - asyncio.get_running_loop().time()),
            return_when=asyncio.FIRST_EXCEPTION,
        )
        for task in done:
            if task.cancelled():
                raise asyncio.CancelledError
            if (error := task.exception()) is not None:
                raise error
        return {name for name, task in tasks.items() if task in pending}
    except BaseException:
        failed = True
        raise
    finally:
        effective_deadline = min(cleanup_deadline, asyncio.get_running_loop().time() + cleanup_timeout)
        for name in tasks:
            cleanup_deadlines[name] = effective_deadline
        cleanup = asyncio.create_task(_stop_search_tasks(
            tasks, connections, cleanup_deadline=effective_deadline, terminated=terminated,
        ))
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # A disconnect may race the leg timeout or another cancellation.
                # The supervisor keeps its bounded cleanup ownership in both cases.
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError
        if not failed:
            # A deadline can race a completed task, or expose a bug while
            # unwinding a query. Neither becomes a successful empty search.
            for task in tasks.values():
                if task.done() and not task.cancelled() and (error := task.exception()) is not None:
                    raise error


def _leg_unavailable(error: BaseException) -> bool:
    """Only availability failures permit a healthy-leg fallback.

    Permission, schema, vocabulary-fence and programming errors must refuse
    the whole request; rescuing hits would hide a broken security boundary.
    PostgreSQL statement cancellation includes the server's query timeout.
    Caller cancellation is a BaseException and is never classified as unavailable.
    """
    return isinstance(error, (TimeoutError, OSError, asyncpg.PostgresConnectionError, VectorStoreUnavailable)) or (
        isinstance(error, asyncpg.PostgresError)
        and error.sqlstate in {"57014", "57P01", "57P02", "57P03", "53300", "53400", "53200"}
    )

# The largest term id the vchord shape can index. Term ids are minted as
# `bigint`, and a `bm25vector`'s text input parses u32, answering anything
# larger, or negative, with "Bad parsing at position N" (akb#665). The index is
# narrower than both: it addresses its per-term arrays with 32-bit byte offsets,
# 4 bytes per id, and the release build does not check the multiplication. An
# id at or above 2^30 therefore lands on the id 2^30 below it, and nothing
# fails. In an index built over existing rows, a search for the high id reads
# the low id's entries, and the document holding the high id cannot be found
# through it. Inserted and then sealed, the high id's posting joins the low
# id's list and its document the low id's count, so a search for the low term
# ranks a document that does not hold it, and both terms' IDF is wrong.
_BM25VECTOR_MAX_TERM_ID = 2**30 - 1


class TermIdOutOfRange(ValueError):
    """A term id the vchord BM25 index cannot hold (akb#687).

    A `ValueError` subclass, so existing `except ValueError` callers keep
    refusing it. Unlike a generic `ValueError`, it is deterministic: no retry
    will ever index this chunk while the vocabulary numbers ids this way.
    Worker paths terminate it on the first failure instead of spending the
    retry budget; the remedy is `scripts/compact_bm25_term_ids.py`.
    """

    def __init__(self, term_id: int) -> None:
        self.term_id = int(term_id)
        super().__init__(
            f"term id {self.term_id} is outside the range the vchord BM25 index "
            f"holds (0 to {_BM25VECTOR_MAX_TERM_ID:,}): an id at or above "
            f"2^30 would land on another term's entries in that index and "
            f"corrupt its postings, statistics and search results. "
            f"The BM25 vocabulary's term ids have to be renumbered densely "
            f"(scripts/compact_bm25_term_ids.py) before a document holding it "
            f"can be indexed."
        )


async def _set_vchord_candidate_budget(
    conn: asyncpg.Connection, budget: int,
) -> None:
    if budget != -1 and not 1 <= budget <= _VCHORD_MAX_CANDIDATES:
        raise ValueError(f"invalid vchord candidate budget: {budget}")
    # The bounded integer is safe to interpolate into the extension GUC.
    # SET LOCAL restores the pooled connection at transaction commit.
    await conn.execute(f"SET LOCAL bm25_catalog.bm25_limit = {budget}")


async def _vchord_configured_budget(conn: asyncpg.Connection) -> int:
    """`bm25_catalog.bm25_limit`, on a session that may not have loaded vchord_bm25.

    The extension defines its settings when its library loads, and the image
    `deploy/postgres/Dockerfile` builds does not preload it. On a session that
    has not called into the extension yet the setting does not exist, and a
    plain `current_setting` raised `unrecognized configuration parameter` —
    which reaches `hybrid_search` as a store failure, so the first unfiltered
    or index-led search on every new pooled connection lost both legs
    (akb#615). A CI server that preloaded the library never saw it.

    Any call into the extension loads it; a literal of its type is the
    cheapest, and a connection pays for it once. A value an operator set in
    the server configuration already exists before the load and is read as is.
    """
    value = await conn.fetchval(
        "SELECT current_setting('bm25_catalog.bm25_limit', true)"
    )
    if value is None:
        await conn.fetchval("SELECT '{}'::bm25_catalog.bm25vector IS NOT NULL")
        value = await conn.fetchval(
            "SELECT current_setting('bm25_catalog.bm25_limit')"
        )
    return int(value)

# Schema name lands in identifier position in DDL; validate to keep
# operator typos and config-injection-style attacks from blowing up
# the cluster. Plain ASCII identifier is enough — pgvector's own
# schema only ever sees lowercase names.
_SCHEMA_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _rrf(*ranked_lists: list[str]) -> dict[str, float]:
    """Reciprocal Rank Fusion. Each list is chunk_ids in score order
    (best first). Returns {chunk_id: fused_score}."""
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, chunk_id in enumerate(ranked, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)
    return scores



def _bm25vector_literal(
    indices: list[int], values: list[float]
) -> str:
    """`{term_id:tf, …}` — the extension's text input; `'{}'` for no terms.

    Term ids must be strictly increasing, not merely sorted: the parser rejects
    `{1:2, 5:1, 5:1}` with "Indexes are not increasing", so a repeated id has to
    be folded into one entry rather than emitted twice. `encode_document`
    returns distinct terms today, which is exactly why this is worth doing here
    — a caller that does not is a silent corruption, and the error only appears
    once a real database sees the literal.

    Frequencies are integers: the vector counts occurrences, and
    `encode_document` produced raw counts for this shape.

    The floor is the second of two defences, not a redundant one. `upsert_one`
    refuses non-integral weights before reaching here, so a pre-baked value
    arriving through the store is already gone — but that guard passes anything
    that happens to be whole, and a single-term document of exactly average
    length gives tf*(k1+1)/(tf + k1*1) = 1.0 exactly. It also does not apply to
    a caller holding this function directly. So `max(1, round(w))` still has
    work: rounding rather than truncating, and flooring at one, because
    `round(0.4)` is 0 and a term the document contains must not vanish.
    """
    if len(indices) != len(values):
        # `zip` would truncate to the shorter one and drop the rest without a
        # sound. The result would still be a valid vector, so nothing downstream
        # would notice a document indexed under half its terms.
        raise ValueError(
            f"sparse indices and values disagree: {len(indices)} vs {len(values)}"
        )
    for term in indices:
        if not 0 <= int(term) <= _BM25VECTOR_MAX_TERM_ID:
            # Up to u32 the text input takes the id, and the index corrupts
            # another term's entries without a word; past u32, or below zero,
            # the input answers "Bad parsing at position N", naming neither the
            # term nor the limit. Dropping the term instead would index the
            # document under a subset of what it says, which nothing downstream
            # could notice.
            raise TermIdOutOfRange(term)
    counts: dict[int, int] = {}
    for term, weight in zip(indices, values):
        counts[int(term)] = counts.get(int(term), 0) + max(1, round(float(weight)))
    # An empty vector is written, not skipped, and the two are not the same
    # thing: `NULL` means nothing has encoded this row yet, `'{}'` means
    # something did and the document had no content-bearing terms. Only that
    # distinction makes `sparse_bm25 IS NULL` an exact statement of work
    # remaining, which is what a backfill over an existing corpus needs.
    #
    # It costs nothing at search time. `'{}'` passes the `IS NOT NULL` guard
    # and scores `-0`, but `-0 < 0` is false in IEEE 754, so the ranked
    # subquery's filter drops it exactly as it drops a document holding no
    # query term. Measured rather than argued: with five real matches and ten
    # thousand `'{}'` rows under `LIMIT 10`, all five came back — the `-0` rows
    # sort after every negative score, so they can only occupy slots that no
    # match wanted. (An earlier version of this comment claimed the opposite
    # and returned None for it. It was wrong about the filter.)
    return "{" + ", ".join(f"{t}:{counts[t]}" for t in sorted(counts)) + "}"


def _bm25query_literal(terms: list[int]) -> str | None:
    """The query as the same `{id:tf}` text input documents use, or None.

    Queries were bound as `int[]`, the extension's only array cast, and asyncpg
    refuses any id past 2,147,483,647 before the query is sent, while term ids
    are minted as `bigint` (akb#665). Built as text, a query goes through the
    same input and the same bound as a document, and the two stay equal: the
    array cast counts a repeated id the way this literal folds it.

    That bound is `_BM25VECTOR_MAX_TERM_ID`, 1,073,741,823: the index cannot
    address an id at or above 2^30. A term past it is in no document, because
    writing one is refused, so it is dropped here rather than failing the whole
    search. None means nothing is left to ask.
    """
    held = [int(t) for t in terms if 0 <= int(t) <= _BM25VECTOR_MAX_TERM_ID]
    if not held:
        return None
    return _bm25vector_literal(held, [1.0] * len(held))


class PgvectorStore:
    """VectorStore impl over PostgreSQL + pgvector + posting table.

    Construction takes a `get_main_pool` callable so the driver can
    transparently share the main PG pool when DSN is blank or open
    its own pool when DSN is set, with one code path either way.

    Write methods (ensure_collection / upsert_one / delete_point) all
    accept an optional `conn`. When the caller is already inside a PG
    transaction, passing that conn lets this driver join — making the
    chunks-table mark and the vector_index INSERT atomic. Without
    that, an outer rollback after an inner-conn commit would leak
    rows into vector_index that the SoT chunks table still treats as
    pending (recoverable, but visible as a counter mismatch).
    """

    # Stores vault_id, filters on it in hybrid_search, exposes
    # vault_backfill_pending() — the reference vault-filter driver (issue #189).
    vault_filter_supported = True

    def __init__(
        self,
        *,
        dsn: str | None,
        schema: str,
        dense_dim: int,
        sparse_shape: SparseShape,
        get_main_pool=None,  # callable returning the main PG pool, used when dsn is None
        retrieval_timeout_secs: float = 30.0,
        # Raw term frequencies -> the weights `posting` stores. Handed over
        # only while the way back to `posting` is retained (akb#615); see
        # `_keeps_posting` below.
        posting_weights: Callable[[list[float]], Awaitable[list[float]]] | None = None,
    ):
        if not _SCHEMA_NAME_RE.match(schema):
            raise ValueError(
                f"vector_store_schema must be a plain SQL identifier "
                f"([A-Za-z_][A-Za-z0-9_]*); got {schema!r}"
            )
        self._dsn = dsn or None
        self._schema = schema
        self._dense_dim = dense_dim
        self._sparse_shape = sparse_shape
        if not math.isfinite(retrieval_timeout_secs) or not 0 < retrieval_timeout_secs <= 30:
            raise ValueError("retrieval_timeout_secs must be finite and in (0, 30]")
        self._retrieval_timeout_secs = retrieval_timeout_secs
        self._get_main_pool = get_main_pool
        self._posting_weights = posting_weights
        # Decided in `_do_ensure`: the vchord shape keeps `posting` current
        # only when it has weights for it and the table is already there.
        self._keeps_posting = False
        self._own_pool: asyncpg.Pool | None = None
        self._ensured_collection = False
        # Serialize ensure_collection across concurrent callers. PG's
        # CREATE SCHEMA IF NOT EXISTS / CREATE TABLE IF NOT EXISTS are
        # not race-safe at the catalog level — concurrent sessions can
        # still trip "duplicate key value violates pg_namespace_nspname_index".
        # The lock makes only the first caller hit the DB; the rest see
        # _ensured_collection=True and short-circuit.
        self._ensure_lock = asyncio.Lock()

    async def _pool(self) -> asyncpg.Pool:
        """Return the pool we read/write through."""
        if self._dsn is None:
            if self._get_main_pool is None:
                raise RuntimeError(
                    "PgvectorStore: dsn is blank and no main pool factory was provided"
                )
            return await self._get_main_pool()
        if self._own_pool is None:
            # Bootstrap the extension BEFORE building a pool whose `init`
            # callback registers the pgvector codec. register_vector ->
            # asyncpg.set_type_codec('vector', ...) can't build a codec
            # for a type that doesn't exist yet and raises
            # `ValueError: unknown type: public.vector` — which would
            # abort pool creation on any DB where `CREATE EXTENSION
            # vector` has never run (e.g. a fresh `pgvector/pgvector`
            # DB: the extension is *available* but not *created*). See #117.
            await self._bootstrap_extension(self._dsn)

            async def _init(conn):
                # Register pgvector binary codec on every conn the
                # pool hands out — list[float] in, list[float] out,
                # no text-literal round-trip.
                from pgvector.asyncpg import register_vector
                await register_vector(conn)
                try:
                    conn._akb_pgvector_codec = True
                except (AttributeError, TypeError):
                    pass
            self._own_pool = await asyncpg.create_pool(
                self._dsn, min_size=1, max_size=8, command_timeout=30,
                init=_init,
            )
        return self._own_pool

    @staticmethod
    async def _bootstrap_extension(dsn: str) -> None:
        """`CREATE EXTENSION IF NOT EXISTS vector` over a one-off conn.

        Used to guarantee the `vector` type exists before any code path
        registers the pgvector codec (pool `init`). Idempotent; cheap.
        """
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        finally:
            await conn.close()

    async def _ensure_codec(self, conn) -> None:
        """Register the pgvector binary codec on `conn` once. Conns
        from our own pool already have it (init callback); main-pool
        and caller-supplied conns get registered on first use."""
        if getattr(conn, "_akb_pgvector_codec", False):
            return
        from pgvector.asyncpg import register_vector
        await register_vector(conn)
        try:
            conn._akb_pgvector_codec = True
        except (AttributeError, TypeError):
            pass  # fallback: re-register every time, low cost

    @asynccontextmanager
    async def _conn(self, outer):
        """Yield a usable, codec-registered conn.

        - `outer` not None → reuse it; caller owns the transaction.
        - `outer` is None  → acquire a fresh conn from the pool, run
          inside a transaction so the writes commit on context exit.
        """
        if outer is not None:
            await self._ensure_codec(outer)
            yield outer
            return
        pool = await self._pool()
        async with pool.acquire() as c:
            await self._ensure_codec(c)
            async with c.transaction():
                yield c

    async def ensure_collection(self, *, conn=None) -> None:
        """Idempotent schema creation, race-free across processes.

        Three layers of guards, in order:

        1. **Instance flag** — ``_ensured_collection`` short-circuits
           every call after the first successful one within this
           process. The hot path is one bool read.
        2. **asyncio.Lock** — serializes concurrent callers inside the
           same event loop (e.g. lifespan startup + a request handler
           racing on a cold start). The second caller waits, sees the
           flag, returns.
        3. **PG advisory transaction lock** — serializes across worker
           processes / pods sharing the same database. A peer that's
           mid-rebuild holds the lock; we block until it commits (or
           rolls back), then re-check inside the lock and skip the
           build if the peer already produced the artifact.

        Layer (3) was missing pre-0.6.4. The 0.6.2 rebuild of
        `idx_vi_chunks_dense` (partial HNSW) could race against
        itself: a search request that timed out (504) cancelled the
        in-flight CREATE INDEX before it committed; the next request
        saw ``_ensured_collection=False`` and re-issued, ad infinitum.
        Even at a single uvicorn worker the asyncio cancel made it
        look multi-process. Cross-process advisory lock + atomic
        index swap (build under temp name → DROP legacy → RENAME)
        below makes the rebuild forward-progress-safe.

        `conn` is accepted for driver-interface parity (base.py) and
        ignored — we always acquire our own pool conn so the schema
        commit is independent of any caller transaction state.
        """
        if self._ensured_collection:
            return
        async with self._ensure_lock:
            if self._ensured_collection:
                return
            try:
                pool = await self._pool()
                async with pool.acquire() as c:
                    # CREATE EXTENSION must be COMMITTED before the codec is
                    # registered. register_vector -> set_type_codec('vector')
                    # introspects pg_catalog for the `vector` type; an
                    # extension created inside the *same uncommitted*
                    # transaction is NOT resolvable and asyncpg raises
                    # `ValueError: unknown type: public.vector` on a fresh DB.
                    #
                    # The separate-DSN path bootstraps the extension on its
                    # own committed connection in `_pool()` (see
                    # `_bootstrap_extension`). The shared-main-pool path
                    # (`vector_url=""`, dsn is None) skips that, so it must
                    # commit the extension here — outside the transaction
                    # block below — before _ensure_codec runs. #117 fixed the
                    # separate-DSN case but assumed the same-tx create was
                    # visible to the codec; it is not, so shared-PG self-host
                    # deployments still broke on a fresh DB (e.g. after a
                    # demo PVC-wipe reset). This autocommit statement is
                    # idempotent and a no-op once the extension exists.
                    await c.execute("CREATE EXTENSION IF NOT EXISTS vector")
                    async with c.transaction():
                        # advisory_xact_lock auto-releases on tx end
                        # (commit OR rollback OR conn close), so a
                        # cancelled CREATE INDEX can't strand the lock.
                        await c.execute(
                            "SELECT pg_advisory_xact_lock($1)",
                            _advisory_lock_key(self._schema),
                        )
                        # _do_ensure re-runs CREATE EXTENSION IF NOT EXISTS
                        # (no-op now) then builds the schema/tables; the
                        # codec registers cleanly because the type is
                        # already committed above.
                        await self._do_ensure(c)
                        await self._ensure_codec(c)
            except asyncpg.PostgresError as e:
                raise VectorStoreUnavailable(f"schema setup failed: {e}") from e
            self._ensured_collection = True

    @property
    def sparse_shape(self) -> SparseShape:
        """How this store wants its sparse terms — read by the encoder.

        The convention the encoder must produce depends on it, and the store is
        the only thing that knows which shape it was actually built with. The
        setting is not: `tests/bench/sparse_shape_bench.py` constructs one store
        per shape in a loop while `settings` never moves, and encodes the corpus
        once for all of them."""
        return self._sparse_shape

    async def _do_ensure(self, conn) -> None:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"')

        # `dense` is nullable: the embed worker upserts sparse-only points
        # when the embedding API is unavailable, and the dense leg of
        # hybrid_search filters them out via the partial HNSW index below.
        if self._sparse_shape == "arrays":
            await conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS "{self._schema}".chunks (
                    chunk_id        UUID PRIMARY KEY,
                    source_type     TEXT NOT NULL,
                    source_id       UUID NOT NULL,
                    vault_id        UUID,
                    section_path    TEXT,
                    content         TEXT NOT NULL,
                    chunk_index     INTEGER NOT NULL,
                    dense           vector({self._dense_dim}),
                    sparse_terms    BIGINT[] NOT NULL DEFAULT '{{}}',
                    sparse_weights  REAL[]   NOT NULL DEFAULT '{{}}',
                    indexed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
        elif self._sparse_shape == "posting":
            # Fresh databases can score postings without heap reads. Existing
            # tables are intentionally not rebuilt during startup; see the
            # explicit concurrent-index maintenance SQL for upgrades.
            await conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS "{self._schema}".chunks (
                    chunk_id        UUID PRIMARY KEY,
                    source_type     TEXT NOT NULL,
                    source_id       UUID NOT NULL,
                    vault_id        UUID,
                    section_path    TEXT,
                    content         TEXT NOT NULL,
                    chunk_index     INTEGER NOT NULL,
                    dense           vector({self._dense_dim}),
                    indexed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            await conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS "{self._schema}".posting (
                    term_id   BIGINT NOT NULL,
                    chunk_id  UUID NOT NULL REFERENCES "{self._schema}".chunks(chunk_id) ON DELETE CASCADE,
                    weight    REAL NOT NULL,
                    PRIMARY KEY (term_id, chunk_id) INCLUDE (weight)
                )
                """
            )
            await conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_posting_term
                    ON "{self._schema}".posting (term_id)
                """
            )
            await conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_posting_chunk
                    ON "{self._schema}".posting (chunk_id)
                """
            )

        elif self._sparse_shape == "vchord":
            # The terms live in one column and the index owns the scoring, so
            # there is no side table and no stored weights — `bm25vector` holds
            # {term_id: term_frequency} and k1/b are the index's, not ours.
            #
            # `CREATE EXTENSION` here matches what this method already does for
            # `vector` two statements up: the driver provisions what it needs
            # and fails visibly if the database cannot supply it. An operator
            # who has not installed the extension picks a different shape.
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vchord_bm25")
            await conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS "{self._schema}".chunks (
                    chunk_id        UUID PRIMARY KEY,
                    source_type     TEXT NOT NULL,
                    source_id       UUID NOT NULL,
                    vault_id        UUID,
                    section_path    TEXT,
                    content         TEXT NOT NULL,
                    chunk_index     INTEGER NOT NULL,
                    dense           vector({self._dense_dim}),
                    sparse_bm25     bm25_catalog.bm25vector,
                    indexed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            # Schema-qualified: the extension installs into `bm25_catalog` and
            # nothing here puts that on `search_path`.
            await conn.execute(
                f"""
                ALTER TABLE "{self._schema}".chunks
                    ADD COLUMN IF NOT EXISTS sparse_bm25 bm25_catalog.bm25vector
                """
            )
            # The index is built here only for an empty table — a fresh
            # install, where it costs nothing. Over existing chunks it is the
            # job of `scripts/backfill_bm25_vector.py --index`, which builds it
            # CONCURRENTLY and refuses while any row still has no vector.
            # Building it here held this transaction's ShareLock against every
            # write for the whole build, over whatever part of the column the
            # backfill had reached, and made each later batch of that backfill
            # pay index maintenance per row (akb#615). A populated table with no
            # index means the shape was selected before the runbook finished:
            # say so, rather than serve a partial column.
            if await conn.fetchval(
                "SELECT to_regclass($1) IS NULL",
                f'"{self._schema}".idx_vi_chunks_bm25',
            ):
                if await conn.fetchval(
                    f'SELECT EXISTS (SELECT 1 FROM "{self._schema}".chunks)'
                ):
                    raise VectorStoreUnavailable(
                        'vector_store_sparse_shape is "vchord" but '
                        f'"{self._schema}".idx_vi_chunks_bm25 does not exist and '
                        "the table already holds chunks. Fill sparse_bm25 and build "
                        "the index with scripts/backfill_bm25_vector.py (--index, "
                        "once the sweep has converged) before selecting this shape."
                    )
                await conn.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS idx_vi_chunks_bm25
                        ON "{self._schema}".chunks
                     USING bm25 (sparse_bm25 bm25_catalog.bm25_ops)
                    """
                )
            # The way back to `posting` (akb#615). `bm25_external_stats_mode =
            # required` keeps posting's statistics fresh for a rollback, and
            # while it does the factory hands over `posting_weights`: an
            # installation that came from `posting` then keeps that table
            # current on every write, so switching back serves the rows as
            # they are now. A fresh vchord install has nothing to go back to,
            # and nothing is created for it.
            self._keeps_posting = (
                self._posting_weights is not None
                and await self._side_table_exists(conn)
            )

        else:
            assert_never(self._sparse_shape)
        # Existing deployments may have `dense` from the pre-0.6.2
        # NOT NULL era. Drop the constraint idempotently so the
        # sparse-only fallback can actually store a NULL.
        await conn.execute(
            f'ALTER TABLE "{self._schema}".chunks ALTER COLUMN dense DROP NOT NULL'
        )

        # vault_id (issue #189 Phase 2): denormalized owning-vault on each point
        # so the ACL filter can be by accessible vault (small set) instead of an
        # enumerated source_id list. Idempotent ADD COLUMN for tables created
        # before this column existed; nullable because the column is backfilled
        # out-of-band (UPDATE from source) and the vault filter stays gated off
        # (`vault_filter_enabled`) until the backfill completes.
        await conn.execute(
            f'ALTER TABLE "{self._schema}".chunks ADD COLUMN IF NOT EXISTS vault_id UUID'
        )

        # Common indexes (both shapes).
        await conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_vi_chunks_source_id
                ON "{self._schema}".chunks (source_id) INCLUDE (chunk_id)
            """
        )
        await conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_vi_chunks_vault_id
                ON "{self._schema}".chunks (vault_id) INCLUDE (chunk_id)
            """
        )
        # HNSW for dense KNN, partial on `WHERE dense IS NOT NULL` so
        # sparse-only points (BM25 fallback) don't pollute the dense leg.
        # Inspect `pg_index.indpred` rather than the textual
        # `pg_indexes.indexdef`: indpred is non-NULL iff the index has a
        # WHERE clause, format-agnostic across PG versions.
        idx_state = await conn.fetchrow(
            """
            SELECT i.indpred IS NULL AS is_legacy_full
              FROM pg_index i
              JOIN pg_class c     ON c.oid = i.indexrelid
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = $1
               AND c.relname = 'idx_vi_chunks_dense'
            """,
            self._schema,
        )

        if idx_state is None:
            # Fresh schema — no index yet. Build the partial form
            # directly under the canonical name.
            await self._build_partial_hnsw(conn, target_name="idx_vi_chunks_dense")
        elif idx_state["is_legacy_full"]:
            # Pre-0.6.2 legacy full HNSW. Atomic swap so the index is
            # never absent: build the new partial under a temp name,
            # then DROP legacy + RENAME inside the same transaction
            # holding the advisory lock. If the build fails (OOM, /dev/shm
            # too small, cancelled), the legacy index stays in place
            # and search keeps working — operator just sees the swap
            # didn't happen yet.
            await self._build_partial_hnsw(conn, target_name="idx_vi_chunks_dense_new")
            await conn.execute(
                f'DROP INDEX "{self._schema}".idx_vi_chunks_dense'
            )
            await conn.execute(
                f'ALTER INDEX "{self._schema}".idx_vi_chunks_dense_new '
                f'RENAME TO idx_vi_chunks_dense'
            )
        # else: partial index already in place — no-op.

        # Last, so only a setup that succeeded is recorded: the shape this
        # database now serves, which is what a later `auto` reads first.
        await record_sparse_shape(conn, schema=self._schema, shape=self._sparse_shape)

    async def _build_partial_hnsw(self, conn, *, target_name: str) -> None:
        """Build the partial HNSW dense index under ``target_name``.

        Bumps ``maintenance_work_mem`` for this session: at our scale
        (a few hundred K chunks at 1024-dim) HNSW's graph-construction
        memory peaks well above the PG default 64MB. With the default
        we have observed `could not resize shared memory segment ...
        No space left on device` errors that abort the CREATE INDEX
        — and a half-built index leaves the schema with `dense_idx`
        absent, sending the dense leg of every search to a seq scan.

        2GB chosen empirically as the largest value that comfortably
        fits in the 4GB `/dev/shm` allocated to the postgres pod by
        ``deploy/k8s/postgres.yaml`` while leaving headroom for
        concurrent normal workload. Operators on much larger corpora
        (multi-M chunks) can either raise the pod's `/dev/shm` and
        this constant in lockstep, or wait for a future driver flag.
        """
        await conn.execute("SET LOCAL maintenance_work_mem = '2GB'")
        await conn.execute(
            f"""
            CREATE INDEX "{target_name}"
                ON "{self._schema}".chunks
                USING hnsw (dense vector_cosine_ops)
                WITH (m = 16, ef_construction = 64)
                WHERE dense IS NOT NULL
            """
        )

    async def health(self) -> bool:
        try:
            pool = await self._pool()
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return True
        except Exception:  # noqa: BLE001
            return False

    async def vault_backfill_pending(self) -> int:
        """How many points still have NULL `vault_id` (issue #189 Phase 2). The
        vault filter (`vault_filter_enabled`) is only safe to enable once this is
        0 — surfaced in /health so operators can tell when the backfill is done.
        Cheap (indexed `vault_id` btree). Driver-specific (pgvector only)."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            return int(await conn.fetchval(
                f'SELECT count(*) FROM "{self._schema}".chunks WHERE vault_id IS NULL'
            ))

    # ── Upsert ────────────────────────────────────────────────────

    async def _side_table_exists(self, conn) -> bool:
        return bool(await conn.fetchval(
            "SELECT to_regclass($1) IS NOT NULL", f'"{self._schema}".posting',
        ))

    async def _replace_postings(
        self, c, cid: uuid.UUID, terms: list[int], weights: list[float],
    ) -> None:
        """Make this chunk's rows in `posting` exactly `terms` and `weights`."""
        await c.execute(
            f'DELETE FROM "{self._schema}".posting WHERE chunk_id = $1', cid,
        )
        if terms:
            await c.executemany(
                f"""
                INSERT INTO "{self._schema}".posting
                    (term_id, chunk_id, weight)
                VALUES ($1, $2, $3)
                """,
                [(int(t), cid, float(w)) for t, w in zip(terms, weights)],
            )

    async def upsert_one(
        self,
        *,
        conn=None,
        chunk_id: str,
        content: str,
        section_path: str | None,
        chunk_index: int,
        dense: list[float] | None,
        sparse_indices: list[int],
        sparse_values: list[float],
        source_type: str,
        source_id: str,
        vault_id: str,
    ) -> None:
        await self.ensure_collection()
        cid = uuid.UUID(str(chunk_id))
        sid = uuid.UUID(str(source_id))
        vid = uuid.UUID(str(vault_id))
        # `dense` goes through pgvector's binary codec — list[float]
        # straight into asyncpg's bind. No text literal, no `::vector`
        # cast needed. `None` (sparse-only fallback when the embed API
        # was unavailable) becomes a NULL row and is excluded from the
        # partial HNSW index by the WHERE clause above.
        # `list(dense)` is a defensive copy: the caller may reuse the
        # list across batches and asyncpg binds by reference.
        dense_param: list[float] | None = list(dense) if has_dense(dense) else None
        try:
            async with self._conn(conn) as c:
                if self._sparse_shape == "arrays":
                    await c.execute(
                        f"""
                        INSERT INTO "{self._schema}".chunks
                            (chunk_id, source_type, source_id, vault_id, section_path,
                             content, chunk_index, dense,
                             sparse_terms, sparse_weights, indexed_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW())
                        ON CONFLICT (chunk_id) DO UPDATE SET
                            source_type    = EXCLUDED.source_type,
                            source_id      = EXCLUDED.source_id,
                            vault_id       = EXCLUDED.vault_id,
                            section_path   = EXCLUDED.section_path,
                            content        = EXCLUDED.content,
                            chunk_index    = EXCLUDED.chunk_index,
                            dense          = EXCLUDED.dense,
                            sparse_terms   = EXCLUDED.sparse_terms,
                            sparse_weights = EXCLUDED.sparse_weights,
                            indexed_at     = NOW()
                        """,
                        cid, source_type, sid, vid, section_path or "",
                        content, int(chunk_index), dense_param,
                        list(sparse_indices), [float(v) for v in sparse_values],
                    )
                elif self._sparse_shape == "posting":
                    await c.execute(
                        f"""
                        INSERT INTO "{self._schema}".chunks
                            (chunk_id, source_type, source_id, vault_id, section_path,
                             content, chunk_index, dense, indexed_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
                        ON CONFLICT (chunk_id) DO UPDATE SET
                            source_type  = EXCLUDED.source_type,
                            source_id    = EXCLUDED.source_id,
                            vault_id     = EXCLUDED.vault_id,
                            section_path = EXCLUDED.section_path,
                            content      = EXCLUDED.content,
                            chunk_index  = EXCLUDED.chunk_index,
                            dense        = EXCLUDED.dense,
                            indexed_at   = NOW()
                        """,
                        cid, source_type, sid, vid, section_path or "",
                        content, int(chunk_index), dense_param,
                    )
                    await self._replace_postings(c, cid, sparse_indices, sparse_values)
                elif self._sparse_shape == "vchord":
                    # One statement, not two: the terms are a column of the row
                    # being written, so there is no side table to delete from
                    # and no window in which a chunk exists without its terms.
                    #
                    # `sparse_values` are raw term frequencies here — the shape
                    # is in `_RAW_WEIGHT_SHAPES`, so `encode_document` did not
                    # bake k1/b into them. The index applies BM25 itself; a
                    # pre-baked weight arriving at this branch would be
                    # saturated twice and would only show up as worse ranking.
                    # Defence in depth, not the barrier. What actually keeps
                    # pre-baked weights out is that `encode_document` is told
                    # the store's `sparse_shape`; this only catches a caller
                    # that went around it, and it catches most rather than all:
                    # a saturated weight lands in (0, k1+1) and is rarely whole,
                    # but a single-term document of exactly average length gives
                    # tf*(k1+1)/(tf + k1*1) = 1.0 exactly. Multi-term documents
                    # are caught because `any` only needs one fractional weight.
                    #
                    # ValueError, not VectorStoreUnavailable: this is a
                    # deterministic caller error that no retry fixes, and
                    # `embed_worker` reads VectorStoreUnavailable as a transient
                    # outage — it would fail the row, hand the rest of the batch
                    # back and return, halting indexing while reporting that the
                    # vector store is down. The per-row path is the right one,
                    # and it is the one `_bm25vector_literal` already uses for
                    # the same class of mistake.
                    if any(float(w) != int(float(w)) for w in sparse_values):
                        raise ValueError(
                            "vchord shape received non-integral sparse weights: "
                            "these look pre-baked, and this shape needs raw term "
                            "frequencies. The encoder decides from the store's "
                            "`sparse_shape`; a caller that encodes once and then "
                            "switches shapes has to re-encode."
                        )
                    await c.execute(
                        f"""
                        INSERT INTO "{self._schema}".chunks
                            (chunk_id, source_type, source_id, vault_id, section_path,
                             content, chunk_index, dense, sparse_bm25, indexed_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8,
                                $9::text::bm25_catalog.bm25vector, NOW())
                        ON CONFLICT (chunk_id) DO UPDATE SET
                            source_type  = EXCLUDED.source_type,
                            source_id    = EXCLUDED.source_id,
                            vault_id     = EXCLUDED.vault_id,
                            section_path = EXCLUDED.section_path,
                            content      = EXCLUDED.content,
                            chunk_index  = EXCLUDED.chunk_index,
                            dense        = EXCLUDED.dense,
                            sparse_bm25  = EXCLUDED.sparse_bm25,
                            indexed_at   = NOW()
                        """,
                        cid, source_type, sid, vid, section_path or "",
                        content, int(chunk_index), dense_param,
                        _bm25vector_literal(sparse_indices, sparse_values),
                    )
                    if self._keeps_posting:
                        assert self._posting_weights is not None
                        await self._replace_postings(
                            c, cid, sparse_indices,
                            await self._posting_weights(
                                [float(v) for v in sparse_values]
                            ),
                        )
                else:
                    assert_never(self._sparse_shape)
        except asyncpg.PostgresError as e:
            raise VectorStoreUnavailable(f"upsert failed: {e}") from e

    # ── Delete ────────────────────────────────────────────────────

    async def upsert_batch(
        self,
        chunks: list[ChunkUpsert],
        *,
        conn=None,
    ) -> None:
        """Fallback batch path — N calls of ``upsert_one``. No native
        batch shape on this driver yet; the loop preserves the
        Protocol contract while keeping per-call atomicity unchanged.

        Note for whoever gives this path a caller:

        * With `conn=None` it is not atomic. `_conn(None)` opens a transaction
          per call, so a raise partway through leaves the earlier chunks
          committed and the rest not.
        * The raise carries no cursor. The caller cannot tell how far it got.
        * The error policy belongs to the caller, and the two that exist chose
          differently: `embed_worker` marks the one row failed and continues,
          the migration script catches the batch and retries it row by row.
          Swallowing per chunk here would delete that second policy rather than
          add anything.
        * None of the above is observed. `loop_upsert_batch` has no caller
          through any driver today — the only `upsert_batch` call in the tree
          targets a store with a native implementation — so this is read off
          the code, not measured. That is also why it is a note and not a fix:
          a change here has nothing to verify it."""
        from .base import loop_upsert_batch
        await loop_upsert_batch(self, chunks, conn=conn)

    async def delete_point(self, chunk_id: str, *, conn=None) -> None:
        await self.ensure_collection()
        cid = uuid.UUID(str(chunk_id))
        try:
            async with self._conn(conn) as c:
                # ON DELETE CASCADE on posting takes care of the side table.
                await c.execute(
                    f'DELETE FROM "{self._schema}".chunks WHERE chunk_id = $1', cid,
                )
        except asyncpg.PostgresError as e:
            raise VectorStoreUnavailable(f"delete failed: {e}") from e

    # ── Search ────────────────────────────────────────────────────

    @property
    def query_epoch_supported(self) -> bool:
        """Only the layout the atomic renumbering can change needs fencing."""
        return not self._dsn and self._sparse_shape in {"posting", "vchord"}

    async def hybrid_search(
        self,
        *,
        query_text: str,
        query_dense: list[float] | None,
        query_sparse_indices: list[int],
        query_sparse_values: list[float],
        source_ids: list[str] | None,
        limit: int,
        prefetch_per_leg: int,
        vault_ids: list[str] | None = None,
        source_types: list[str] | None = None,
        query_sparse_epoch: int | None = None,
    ) -> list[VectorHit]:
        del query_text  # debug-only on this driver; keep signature parity
        started = time.perf_counter()

        # None means no filter; an explicitly empty authorized set means no
        # results, never an expensive unscoped scan.
        if source_ids == [] or vault_ids == []:
            return []

        await self.ensure_collection()

        has_dense = query_dense is not None and len(query_dense) > 0
        has_sparse = len(query_sparse_indices) > 0
        if not has_dense and not has_sparse:
            return []

        # vault_ids (issue #189 Phase 2) vs source_ids: the caller sends EITHER
        # the vault-granularity ACL filter OR the per-resource filter, never
        # both. Both reduce to `<col> = ANY($N::uuid[])`, so we resolve a single
        # (uuids, column) pair and pass the column name down — keeping each leg
        # query single-branch (filter vs none) instead of duplicating it per
        # column. `filter_col` is a fixed literal, never user input, so the
        # f-string interpolation is as safe as the existing `self._schema` one.
        # Defensive: the two filters are mutually exclusive by contract; if both
        # ever arrive, vault_ids wins below — assert so a future caller bug is
        # caught loudly instead of silently dropping the source filter.
        assert not (vault_ids and source_ids), \
            "hybrid_search got both vault_ids and source_ids; expected exactly one"
        if vault_ids:
            filter_uuids: list[uuid.UUID] | None = [uuid.UUID(str(s)) for s in vault_ids]
            filter_col = "vault_id"
        elif source_ids:
            filter_uuids = [uuid.UUID(str(s)) for s in source_ids]
            filter_col = "source_id"
        else:
            filter_uuids = None
            filter_col = "source_id"  # unused when filter_uuids is None
        # source_types (workbench #1069) is orthogonal to the ACL filter: it
        # ANDs with whichever of vault_ids / source_ids is present (or with no
        # filter at all). Values are driver-owned discriminators, never user
        # input — validate against the known set so a caller typo fails loud
        # instead of silently matching nothing.
        source_type_values: list[str] | None = None
        if source_types is not None:
            from app.services.index_service import SOURCE_TYPES
            unknown = [t for t in source_types if t not in SOURCE_TYPES]
            if unknown:
                raise ValueError(f"unknown source_type filter: {unknown!r}")
            source_type_values = list(source_types)
        loop = asyncio.get_running_loop()
        budget = self._retrieval_timeout_secs
        deadline = loop.time() + budget
        cleanup_reserve = min(_SEARCH_CLEANUP_RESERVE_SECS, budget * 0.1)
        payload_reserve = min(_SEARCH_PAYLOAD_RESERVE_SECS, budget * 0.2)
        leg_deadline = deadline - cleanup_reserve - payload_reserve
        leg_cleanup_deadline = deadline - payload_reserve
        timings: dict[str, float] = {}
        statuses = {"dense": "skipped", "sparse": "skipped"}
        connections: dict[str, asyncpg.Connection] = {}
        cleanup_deadlines: dict[str, float] = {}
        terminated: set[str] = set()

        def closed_transaction_error(error: BaseException) -> bool:
            return (isinstance(error, asyncpg.InterfaceError)
                    and str(error) == "cannot call Transaction.__aexit__(): the underlying connection is closed")

        def cancelled_rollback(error: BaseException) -> bool:
            if not isinstance(error, asyncio.CancelledError) or error.__context__ is None:
                return False
            # Cancellation during a nested ROLLBACK can replace the query error
            # before search_connection sees it. Recover only the exception that
            # asyncpg was unwinding, not unrelated handled exception context on
            # a cancelled query or successful COMMIT/RELEASE SAVEPOINT.
            traceback = error.__traceback__
            while traceback is not None:
                frame = traceback.tb_frame
                if (frame.f_code is asyncpg.transaction.Transaction.__aexit__.__code__
                        and frame.f_locals.get("ex") is error.__context__):
                    return True
                traceback = traceback.tb_next
            return False

        @asynccontextmanager
        async def search_connection(name: str, *, query_deadline: float, release_deadline: float):
            """Own this connection through bounded cancellation and pool reset."""
            begin = time.perf_counter()
            conn = None
            body_error: BaseException | None = None
            try:
                remaining = query_deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError
                conn = await pool.acquire(timeout=remaining)
                connections[name] = conn
                timings[f"{name}_wait"] = time.perf_counter() - begin
                await self._ensure_codec(conn)
                async with conn.transaction():
                    try:
                        # Capture setup failures too: a lost connection here can
                        # also be masked by asyncpg's transaction-exit wrapper.
                        # Server cancellation precedes the client deadline. Each
                        # statement is bounded; the supervisor also bounds their
                        # aggregate, codec registration, pool wait and rollback.
                        millis = int((query_deadline - loop.time()) * 1000) - 1
                        if millis <= 0:
                            raise TimeoutError
                        # pg_settings reports this setting in milliseconds. Respect
                        # an operator's stricter nonzero server/session timeout.
                        await conn.execute(
                            "SELECT set_config('statement_timeout', "
                            "LEAST(NULLIF(setting::bigint, 0), $1::bigint)::text, true) "
                            "FROM pg_settings WHERE name='statement_timeout'", millis,
                        )
                        if loop.time() >= query_deadline:
                            raise TimeoutError
                        yield conn
                    except BaseException as exc:
                        body_error = exc
                        # Inner savepoint cleanup can hide a lost connection or
                        # the query's original error before the outer tx exits.
                        # Peel only validated asyncpg cleanup exceptions.
                        seen = {id(body_error)}
                        while closed_transaction_error(body_error) or cancelled_rollback(body_error):
                            previous = body_error.__context__
                            if previous is None or id(previous) in seen:
                                break
                            seen.add(id(previous))
                            body_error = previous
                        raise
            except BaseException as exc:
                forced_close = name in terminated and closed_transaction_error(exc)
                if (body_error is not None and body_error is not exc
                        and (_leg_unavailable(exc) or closed_transaction_error(exc)
                             or isinstance(exc, asyncio.CancelledError))
                        and not (name in terminated and closed_transaction_error(body_error))):
                    # A failed ROLLBACK must not relabel the query's auth or
                    # programming error as an operational degradation.
                    raise body_error from exc
                if forced_close:
                    body_error = asyncio.CancelledError()
                    raise body_error from exc
                body_error = exc
                raise
            finally:
                if conn is not None:
                    # Transfer ownership before asyncpg starts reset. Terminating
                    # a pool-owned connection concurrently corrupts its holder.
                    connections.pop(name, None)
                    try:
                        effective_deadline = min(release_deadline, cleanup_deadlines.get(name, release_deadline))
                        remaining = min(cleanup_reserve, effective_deadline - loop.time())
                        if remaining <= 0:
                            _terminate_search_connection(conn)
                        else:
                            # asyncpg waits its protocol cancellation and reset
                            # here, terminating the connection if either fails.
                            release = asyncio.create_task(pool.release(conn, timeout=remaining))
                            cancelled = False
                            while not release.done():
                                try:
                                    await asyncio.shield(release)
                                except asyncio.CancelledError:
                                    # Repeated cancellation must not abandon the
                                    # pool's own shielded cancellation/reset task.
                                    cancelled = True
                            release.result()
                            if cancelled and body_error is None:
                                raise asyncio.CancelledError
                    except Exception as exc:
                        if body_error is None or not _leg_unavailable(exc):
                            raise
                timings.setdefault(f"{name}_wait", time.perf_counter() - begin)
                timings[name] = time.perf_counter() - begin

        async def run_leg(name: str) -> list[str]:
            statuses[name] = "running"
            try:
                async with search_connection(
                    name, query_deadline=leg_deadline, release_deadline=leg_cleanup_deadline,
                ) as conn:
                    if name == "dense":
                        assert query_dense is not None
                        ids = await self._search_dense(
                            conn, query_dense=query_dense,
                            filter_uuids=filter_uuids, filter_col=filter_col,
                            source_type_values=source_type_values, limit=prefetch_per_leg,
                        )
                    else:
                        if self.query_epoch_supported:
                            # Fence before vector-table access, on this same
                            # bounded connection and transaction. Refusals
                            # propagate and cancel the concurrent dense leg.
                            await sparse_encoder.hold_vocabulary_epoch(
                                conn, query_sparse_epoch if query_sparse_epoch is not None else 0,
                            )
                        ids = await self._search_sparse(
                            conn, terms=list(query_sparse_indices), weights=list(query_sparse_values),
                            filter_uuids=filter_uuids, filter_col=filter_col,
                            source_type_values=source_type_values, limit=prefetch_per_leg,
                        )
                statuses[name] = "complete"
                return ids
            except (TimeoutError, asyncpg.QueryCanceledError):
                statuses[name] = "timeout"
                return []
            except Exception as exc:
                if not _leg_unavailable(exc):
                    raise
                statuses[name] = "unavailable"
                logger.warning("hybrid leg unavailable: leg=%s type=%s", name, type(exc).__name__)
                return []

        def degradation_reason() -> str | None:
            unavailable = [name for name, status in statuses.items() if status == "unavailable"]
            if unavailable:
                if not any(status == "complete" for status in statuses.values()):
                    return "retrieval_unavailable"
                return f"{unavailable[0]}_leg_failed"
            timed_out = [name for name, status in statuses.items() if status == "timeout"]
            if len(timed_out) == 2:
                return "retrieval_timeout"
            if timed_out:
                return f"{timed_out[0]}_leg_timeout"
            return None

        succeeded = False
        try:
            async with asyncio.timeout_at(leg_deadline):
                pool = await self._pool()
            # The same absolute deadline includes pool acquisition on both
            # legs. Expected operational failures become request-local
            # outcomes; an unexpected error cancels and drains its peer.
            tasks = {
                name: asyncio.create_task(run_leg(name), name=f"pgvector-search-{name}")
                for name, enabled in (("dense", has_dense), ("sparse", has_sparse)) if enabled
            }
            timed_out = await _wait_search_tasks(
                tasks, connections, deadline=leg_deadline, cleanup_deadline=leg_cleanup_deadline,
                cleanup_timeout=cleanup_reserve, cleanup_deadlines=cleanup_deadlines, terminated=terminated,
            )
            for name in timed_out:
                statuses[name] = "timeout"
            dense_ids = tasks["dense"].result() if "dense" in tasks and "dense" not in timed_out else []
            sparse_ids = tasks["sparse"].result() if "sparse" in tasks and "sparse" not in timed_out else []

            dense_available = has_dense and statuses["dense"] == "complete"
            sparse_available = has_sparse and statuses["sparse"] == "complete"
            # Single-leg paths skip RRF.
            if dense_available and not sparse_available:
                top_ids = dense_ids[:limit]
                scoring = [(cid, 1.0 / (RRF_K + i)) for i, cid in enumerate(top_ids, start=1)]
            elif sparse_available and not dense_available:
                top_ids = sparse_ids[:limit]
                scoring = [(cid, 1.0 / (RRF_K + i)) for i, cid in enumerate(top_ids, start=1)]
            else:
                fused = _rrf(dense_ids, sparse_ids)
                scoring = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:limit]
                top_ids = [cid for cid, _ in scoring]

            rows = []
            if top_ids:
                async def fetch_payloads():
                    async with search_connection(
                        "payload", query_deadline=deadline - cleanup_reserve, release_deadline=deadline,
                    ) as conn:
                        return await self._fetch_payloads(conn, top_ids)

                payload_task = asyncio.create_task(fetch_payloads(), name="pgvector-search-payload")
                if await _wait_search_tasks(
                    {"payload": payload_task}, connections,
                    deadline=deadline - cleanup_reserve, cleanup_deadline=deadline,
                    cleanup_timeout=cleanup_reserve, cleanup_deadlines=cleanup_deadlines, terminated=terminated,
                ):
                    raise VectorSearchDegraded(hits=[], reason="retrieval_timeout")
                rows = payload_task.result()
            by_id = {r["chunk_id"]: r for r in rows}
            hits = [
                _row_to_hit(by_id[cid], score=score)
                for cid, score in scoring
                if cid in by_id
            ]
            if reason := degradation_reason():
                raise VectorSearchDegraded(hits=hits, reason=reason)
            succeeded = True
            return hits
        except VectorSearchDegraded:
            raise
        except (TimeoutError, asyncpg.QueryCanceledError) as exc:
            raise VectorSearchDegraded(hits=[], reason="retrieval_timeout") from exc
        except Exception as exc:
            if not _leg_unavailable(exc):
                raise
            raise VectorSearchDegraded(hits=[], reason="retrieval_unavailable") from exc
        finally:
            # No query text, source IDs, content, or credentials in diagnostics.
            # Legs overlap; their durations include pool wait and are not additive.
            logger.info(
                "hybrid_timing ok=%s filter=%s filter_count=%d terms=%d "
                "dense_ms=%.2f dense_wait_ms=%.2f sparse_ms=%.2f sparse_wait_ms=%.2f "
                "payload_ms=%.2f total_ms=%.2f dense_status=%s sparse_status=%s",
                succeeded, filter_col if filter_uuids is not None else "none",
                len(filter_uuids) if filter_uuids is not None else 0,
                len(query_sparse_indices),
                *(timings.get(key, 0.0) * 1000 for key in (
                    "dense", "dense_wait", "sparse", "sparse_wait", "payload",
                )),
                (time.perf_counter() - started) * 1000,
                statuses["dense"], statuses["sparse"],
            )

    async def _search_dense(
        self,
        conn: asyncpg.Connection,
        *,
        query_dense: list[float],
        filter_uuids: list[uuid.UUID] | None,
        filter_col: str,
        source_type_values: list[str] | None = None,
        limit: int,
    ) -> list[str]:
        # RRF consumes rank, so relaxed HNSW output must be sorted by actual
        # distance first. A materialized candidate set preserves the ANN index
        # ORDER BY; the outer +0 also keeps the explicit sort on PostgreSQL 17+.
        params: list[object] = [list(query_dense)]
        predicates = ["dense IS NOT NULL"]
        if filter_uuids:
            params.append(filter_uuids)
            predicates.append(f"{filter_col} = ANY(${len(params)}::uuid[])")
        if source_type_values:
            params.append(source_type_values)
            predicates.append(f"source_type = ANY(${len(params)}::text[])")
        params.append(int(limit))
        async with conn.transaction():
            await conn.execute("SET LOCAL plan_cache_mode = force_custom_plan")
            await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
            await conn.execute("SET LOCAL hnsw.ef_search = 200")
            rows = await conn.fetch(
                f"""
                WITH nearest AS MATERIALIZED (
                  SELECT chunk_id, dense <=> $1 AS distance
                  FROM "{self._schema}".chunks
                  WHERE {" AND ".join(predicates)}
                  ORDER BY dense <=> $1
                  LIMIT ${len(params)}
                )
                SELECT chunk_id::text AS chunk_id FROM nearest
                ORDER BY distance + 0, chunk_id
                """,
                *params,
            )
        return [r["chunk_id"] for r in rows]

    async def _search_sparse(
        self,
        conn: asyncpg.Connection,
        *,
        terms: list[int],
        weights: list[float],
        filter_uuids: list[uuid.UUID] | None,
        filter_col: str,
        source_type_values: list[str] | None = None,
        limit: int,
    ) -> list[str]:
        if not terms:
            return []

        # source_type predicate (workbench #1069), orthogonal to the ACL
        # filter. In filtered branches it ANDs onto the existing WHERE; in
        # unfiltered branches it becomes the WHERE. Each branch below spells
        # its own bind numbering. Values are driver-owned discriminators
        # (validated in hybrid_search), never user input.
        if self._sparse_shape == "arrays":
            # Two query branches (with/without filter) keep the planner honest —
            # a single SQL with `WHERE $3 IS NULL OR <col> = ANY($3)` confuses
            # ANY-cardinality estimation. `filter_col` is "source_id"/"vault_id"
            # (literal — see hybrid_search), so the interpolation is safe.
            if filter_uuids:
                if source_type_values:
                    sql = f"""
                        WITH q AS (
                          SELECT unnest($1::bigint[]) AS tid,
                                 unnest($2::real[])   AS w
                        ),
                        cand AS (
                          SELECT chunk_id, sparse_terms, sparse_weights
                          FROM "{self._schema}".chunks
                          WHERE {filter_col} = ANY($3::uuid[])
                            AND source_type = ANY($5::text[])
                        )
                        SELECT c.chunk_id::text AS chunk_id,
                               SUM(q.w * t.weight) AS score
                        FROM cand c
                        CROSS JOIN LATERAL unnest(c.sparse_terms, c.sparse_weights)
                            AS t(tid, weight)
                        JOIN q ON q.tid = t.tid
                        GROUP BY c.chunk_id
                        ORDER BY score DESC
                        LIMIT $4
                    """
                    rows = await conn.fetch(
                        sql, list(terms), [float(w) for w in weights],
                        filter_uuids, int(limit), source_type_values,
                    )
                else:
                    sql = f"""
                        WITH q AS (
                          SELECT unnest($1::bigint[]) AS tid,
                                 unnest($2::real[])   AS w
                        ),
                        cand AS (
                          SELECT chunk_id, sparse_terms, sparse_weights
                          FROM "{self._schema}".chunks
                          WHERE {filter_col} = ANY($3::uuid[])
                        )
                        SELECT c.chunk_id::text AS chunk_id,
                               SUM(q.w * t.weight) AS score
                        FROM cand c
                        CROSS JOIN LATERAL unnest(c.sparse_terms, c.sparse_weights)
                            AS t(tid, weight)
                        JOIN q ON q.tid = t.tid
                        GROUP BY c.chunk_id
                        ORDER BY score DESC
                        LIMIT $4
                    """
                    rows = await conn.fetch(
                        sql, list(terms), [float(w) for w in weights],
                        filter_uuids, int(limit),
                    )
            elif source_type_values:
                sql = f"""
                    WITH q AS (
                      SELECT unnest($1::bigint[]) AS tid,
                             unnest($2::real[])   AS w
                    )
                    SELECT c.chunk_id::text AS chunk_id,
                           SUM(q.w * t.weight) AS score
                    FROM "{self._schema}".chunks c
                    CROSS JOIN LATERAL unnest(c.sparse_terms, c.sparse_weights)
                        AS t(tid, weight)
                    JOIN q ON q.tid = t.tid
                    WHERE c.source_type = ANY($4::text[])
                    GROUP BY c.chunk_id
                    ORDER BY score DESC
                    LIMIT $3
                """
                rows = await conn.fetch(
                    sql, list(terms), [float(w) for w in weights], int(limit),
                    source_type_values,
                )
            else:
                sql = f"""
                    WITH q AS (
                      SELECT unnest($1::bigint[]) AS tid,
                             unnest($2::real[])   AS w
                    )
                    SELECT c.chunk_id::text AS chunk_id,
                           SUM(q.w * t.weight) AS score
                    FROM "{self._schema}".chunks c
                    CROSS JOIN LATERAL unnest(c.sparse_terms, c.sparse_weights)
                        AS t(tid, weight)
                    JOIN q ON q.tid = t.tid
                    GROUP BY c.chunk_id
                    ORDER BY score DESC
                    LIMIT $3
                """
                rows = await conn.fetch(
                    sql, list(terms), [float(w) for w in weights], int(limit),
                )
        elif self._sparse_shape == "posting":
            return await self._search_posting(
                conn, terms=terms, weights=weights, filter_uuids=filter_uuids,
                filter_col=filter_col, source_type_values=source_type_values, limit=limit,
            )

        elif self._sparse_shape == "vchord":
            # Every shape below wraps its ORDER BY ... LIMIT and drops rows whose
            # score is not negative. `<&>` returns a NEGATIVE BM25 score for a
            # document holding any query term and exactly `-0` for one holding
            # none — it is a total order over the whole table, not a filter. So
            # a plain `ORDER BY ... LIMIT k` pads the result with irrelevant
            # chunks whenever fewer than k documents match, tied at -0 and in
            # whatever order the heap gives. With `prefetch_per_leg` at
            # max(limit*3, 50) that is up to fifty of them entering RRF.
            #
            # `posting` never had this: it JOINs on the term, so a chunk without
            # it is simply absent. The wrap restores the same meaning here, and
            # returning fewer than k when fewer than k match is the same answer
            # `posting` gives.
            #
            # Wrapping rather than a WHERE on the expression keeps the index
            # available for the ordering — verified: the plan is still an Index
            # Scan on the bm25 index with the filter applied above it.
            #
            # `< 0` is safe because this extension's IDF is a log1p variant,
            # which stays positive for every df — so a document holding a query
            # term always scores strictly negative, even at df = N (measured:
            # 50 of 50 matching documents scored -0.00985, none cut). Classic
            # BM25 IDF, log((N-df+0.5)/(df+0.5)), goes negative past df > N/2
            # and this filter would then discard matches. That is a property of
            # the pinned extension, not of this code, so it belongs on the
            # checklist for moving the pin (deploy/postgres/README.md).
            #
            # The index scores and orders; the query only says what to filter.
            # Two shapes, chosen by selectivity, because measurement says no
            # single one wins (akb#626):
            #
            #   filter keeps >= 1% of the corpus  →  let the index lead
            #   filter keeps <  1%                →  materialise first
            #
            # At 0.19% selectivity the second is 21x faster (16ms against
            # 342ms); at 18% the first is 69x faster (27ms against 1863ms).
            # Below about 0.05% they converge — the planner reaches the same
            # place on its own — so the branch is harmless at the small end.
            #
            # Materialising scores every row in scope, so it is kept to scopes
            # under a row cap. A selective scope over the cap is not refused:
            # the index leads, as it would for any wider filter. Refusing it lost
            # the whole sparse leg for every scope between the cap and 1% of the
            # corpus, while the index-led shape answered the same scope
            # (akb#626). The choice decides latency; it must never decide the
            # rows.
            #
            # `plan_cache_mode` is set because asyncpg always prepares, and a
            # generic plan is built without the filter's values: the same
            # statement measured 0.8ms for ten executions and then 1500ms once
            # PostgreSQL switched. SET LOCAL scopes it to this transaction so
            # the pooled connection is not left altered.
            query_vector = _bm25query_literal(list(terms))
            if query_vector is None:
                return []
            # The statistics helper uses a savepoint: catalog failures can
            # fall back without aborting the enclosing retrieval transaction.
            # Server deadline cancellation propagates to the leg collector.
            selective = (
                await self._filter_is_selective(conn, filter_col, filter_uuids)
                if filter_uuids else False
            )
            async with conn.transaction():
                await conn.execute("SET LOCAL plan_cache_mode = force_custom_plan")
                # Every name below is schema-qualified, yet the extension's
                # own `to_bm25query` resolves `bm25vector` unqualified inside
                # itself and fails with `type "bm25vector" does not exist` —
                # an error that names the type and is caused by the path. The
                # This REPLACES the path with a fixed list rather than
                # appending to whatever was configured — an earlier version of
                # this comment claimed otherwise. It is safe here and only
                # here: every name inside the branch is schema-qualified, the
                # branch is the only thing running in this transaction, and
                # SET LOCAL puts the pooled connection back at commit.
                await conn.execute(
                    "SET LOCAL search_path TO \"$user\", public, bm25_catalog"
                )
                requested_limit = int(limit)

                materialise = False
                if filter_uuids and selective:
                    materialise = not await self._scope_exceeds_materialise_cap(
                        conn, filter_col=filter_col, filter_uuids=filter_uuids,
                        source_type_values=source_type_values,
                    )
                if materialise:
                    type_pred = (
                        " AND source_type = ANY($4::text[])" if source_type_values else ""
                    )
                    sql = f"""
                        SELECT chunk_id FROM (
                          WITH candidate_chunks AS MATERIALIZED (
                            SELECT chunk_id, sparse_bm25
                            FROM "{self._schema}".chunks
                            WHERE {filter_col} = ANY($2::uuid[])
                              AND sparse_bm25 IS NOT NULL{type_pred}
                          )
                          SELECT chunk_id::text AS chunk_id,
                                 sparse_bm25 <&> bm25_catalog.to_bm25query(
                                    '"{self._schema}".idx_vi_chunks_bm25'::regclass,
                                    $1::text::bm25_catalog.bm25vector) AS score
                          FROM candidate_chunks
                          ORDER BY score
                          LIMIT $3
                        ) ranked WHERE score < 0
                        ORDER BY score
                    """
                    rows = await conn.fetch(
                        sql, query_vector, filter_uuids, requested_limit,
                        *([source_type_values] if source_type_values else []),
                    )
                else:
                    # Index-led, with or without a filter: the extension ranks,
                    # and the WHERE clause (when there is one) is applied to what
                    # it returns.
                    predicates = ["c.sparse_bm25 IS NOT NULL"]
                    args: list[object] = [query_vector]
                    if filter_uuids:
                        args.append(filter_uuids)
                        predicates.append(f"c.{filter_col} = ANY(${len(args)}::uuid[])")
                    if source_type_values:
                        args.append(source_type_values)
                        predicates.append(f"c.source_type = ANY(${len(args)}::text[])")
                    args.append(requested_limit)
                    sql = f"""
                        SELECT chunk_id FROM (
                          SELECT c.chunk_id::text AS chunk_id,
                                 c.sparse_bm25 <&> bm25_catalog.to_bm25query(
                                    '"{self._schema}".idx_vi_chunks_bm25'::regclass,
                                    $1::text::bm25_catalog.bm25vector) AS score
                          FROM "{self._schema}".chunks c
                          WHERE {" AND ".join(predicates)}
                          ORDER BY score
                          LIMIT ${len(args)}
                        ) ranked WHERE score < 0
                        ORDER BY score
                    """

                    async def ranked() -> list[str]:
                        return [row["chunk_id"] for row in await conn.fetch(sql, *args)]

                    return await self._vchord_page_then_exact(
                        conn, ranked, limit=requested_limit,
                        configured_budget=await _vchord_configured_budget(conn),
                    )

        else:
            assert_never(self._sparse_shape)
        return [r["chunk_id"] for r in rows]

    # Below this share of the corpus, filtering first beats letting the BM25
    # index lead. Measured rather than guessed: 0.19% selectivity is 21x faster
    # materialised, 2.0% is 1.9x faster index-led, and the crossing sits just
    # under 1% (akb#626). It is one number and it will age — what keeps it
    # honest is that both sides of it were measured on a corpus shaped like a
    # real deployment, and that being wrong costs latency, never correctness:
    # both shapes return the same rows. That includes a selective scope too big
    # to materialise under the row cap — it is index-led, not refused.
    _SELECTIVE_FRACTION = 0.01

    async def _search_posting(
        self, conn: asyncpg.Connection, *, terms: list[int], weights: list[float],
        filter_uuids: list[uuid.UUID] | None, filter_col: str,
        source_type_values: list[str] | None, limit: int,
    ) -> list[str]:
        strategy = "term"
        params: list[object] = [list(terms), [float(w) for w in weights]]
        predicates = []
        if filter_uuids:
            params.append(filter_uuids)
            predicates.append(f"c.{filter_col} = ANY(${len(params)}::uuid[])")
            strategy = await estimate_scope_strategy(
                conn, schema=self._schema, filter_col=filter_col,
                filter_uuids=filter_uuids, terms=terms,
            )
        if source_type_values:
            params.append(source_type_values)
            predicates.append(f"c.source_type = ANY(${len(params)}::text[])")
        where = " WHERE " + " AND ".join(predicates) if predicates else ""
        async with conn.transaction():
            # Repeated prepared calls must retain the current term/scope costs.
            await conn.execute("SET LOCAL plan_cache_mode = force_custom_plan")
            if strategy == "point":
                # Statistics can be stale. Bound the actual number of point
                # probes before selecting that plan; no result rows are capped.
                # Scope SQL uses the same parameter numbering as the search.
                scope_params = params[2:]
                scope_where = where
                for index in range(3, len(params) + 1):
                    scope_where = scope_where.replace(f"${index}::", f"${index - 2}::")
                actual = await conn.fetchval(
                    f'WITH scoped AS (SELECT 1 FROM "{self._schema}".chunks c'
                    f'{scope_where} LIMIT {MAX_POINT_CHUNKS + 1}) SELECT count(*) FROM scoped',
                    *scope_params,
                )
                if actual > MAX_POINT_CHUNKS or actual * len(terms) > MAX_POINT_PROBES:
                    strategy = "term"
            params.append(int(limit))
            query_terms = "SELECT unnest($1::bigint[]) AS tid, unnest($2::real[]) AS w"
            if strategy in {"point", "scope"}:
                cte = f"""WITH q AS ({query_terms}), selected AS MATERIALIZED (
                    SELECT c.chunk_id FROM "{self._schema}".chunks c{where})"""
                if strategy == "point":
                    joined = f"""FROM selected c CROSS JOIN q CROSS JOIN LATERAL (
                        SELECT p.weight FROM "{self._schema}".posting p
                        WHERE p.term_id=q.tid AND p.chunk_id=c.chunk_id OFFSET 0
                    ) p"""
                else:
                    joined = f"""FROM selected c
                        JOIN "{self._schema}".posting p ON p.chunk_id=c.chunk_id
                        JOIN q ON q.tid=p.term_id"""
                select_id = "c.chunk_id"
            else:
                cte = f"WITH q AS ({query_terms})"
                joined = f'FROM "{self._schema}".posting p JOIN q ON q.tid=p.term_id'
                if predicates:
                    joined += f' JOIN "{self._schema}".chunks c ON c.chunk_id=p.chunk_id{where}'
                select_id = "p.chunk_id"
            rows = await conn.fetch(
                f"""{cte} SELECT {select_id}::text AS chunk_id, SUM(q.w*p.weight) AS score
                    {joined} GROUP BY {select_id}
                    ORDER BY score DESC, {select_id} LIMIT ${len(params)}""", *params,
            )
        logger.debug("posting_scope_strategy=%s terms=%d", strategy, len(terms))
        return [row["chunk_id"] for row in rows]

    async def _filter_is_selective(
        self,
        conn: asyncpg.Connection,
        filter_col: str,
        filter_uuids: list[uuid.UUID],
    ) -> bool:
        """Does this filter keep a small enough slice to be worth materialising?

        Read from the statistics PostgreSQL already keeps rather than counted:
        a `COUNT(*)` here would be a second round trip on every search, and the
        answer only has to be right about which side of one threshold it falls.
        Checked against real vault sizes spanning 0.0005% to 27%, the estimate
        agreed with the true selectivity on that question 10 times out of 10.

        Any failure answers "not selective", which is the index-led shape — the
        one that is correct everywhere and merely slower in the small-filter
        band. A statistics read that throws must not take a search with it.
        """
        try:
            # hybrid_search owns an outer transaction for statement_timeout.
            # Isolate this best-effort catalog read so a failed statement does
            # not leave that transaction aborted before the actual search.
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT c.reltuples::float8                     AS total,
                           s.n_distinct                            AS n_distinct,
                           s.most_common_vals::text::uuid[]        AS mcv,
                           s.most_common_freqs                     AS freqs
                      FROM pg_class c
                      JOIN pg_namespace n ON n.oid = c.relnamespace
                      LEFT JOIN pg_stats s
                             ON s.schemaname = n.nspname
                            AND s.tablename  = c.relname
                            AND s.attname    = $2
                     WHERE n.nspname = $1 AND c.relname = 'chunks'
                    """,
                    self._schema, filter_col,
                )
        except asyncpg.QueryCanceledError:
            raise  # The retrieval deadline must remain a timeout outcome.
        except asyncpg.PostgresError:
            return False
        if not row or not row["total"] or row["total"] <= 0:
            return False

        total = float(row["total"])
        mcv = list(row["mcv"] or [])
        freqs = list(row["freqs"] or [])
        common = dict(zip(mcv, freqs))
        n_distinct = float(row["n_distinct"] or 0)
        # Negative n_distinct is a ratio of the row count, which is how
        # PostgreSQL records a column whose cardinality grows with the table.
        distinct = abs(n_distinct) * total if n_distinct < 0 else n_distinct
        remainder = max(0.0, 1.0 - sum(freqs))
        others = max(1.0, distinct - len(common))
        share = sum(common.get(v, remainder / others) for v in filter_uuids)
        return share < self._SELECTIVE_FRACTION

    async def _scope_exceeds_materialise_cap(
        self,
        conn: asyncpg.Connection,
        *,
        filter_col: str,
        filter_uuids: list[uuid.UUID],
        source_type_values: list[str] | None = None,
    ) -> bool:
        """Does this scope hold more rows than materialising may score? Actual rows, bounded.

        Counts rows, never planner estimates, and stops at cap + 1, so asking
        costs less than the work it guards.
        """
        predicates = ["sparse_bm25 IS NOT NULL", f"{filter_col} = ANY($1::uuid[])"]
        args: list[object] = [filter_uuids]
        if source_type_values:
            args.append(source_type_values)
            predicates.append(f"source_type = ANY(${len(args)}::text[])")
        count = await conn.fetchval(
            f"""SELECT count(*) FROM (
                SELECT 1 FROM "{self._schema}".chunks
                WHERE {" AND ".join(predicates)}
                LIMIT {_VCHORD_MAX_MATERIALISED_ROWS + 1}
            ) bounded_materialise_scope""", *args,
        )
        return bool(count > _VCHORD_MAX_MATERIALISED_ROWS)

    async def _vchord_page_then_exact(
        self,
        conn: asyncpg.Connection,
        ranked: Callable[[], Awaitable[list[str]]],
        *,
        limit: int,
        configured_budget: int,
    ) -> list[str]:
        """One bounded page, and the exact scan when that page comes back short.

        A finite `bm25_limit` is the size of the extension's internal top-k, and
        a page shorter than `limit` does not prove there is nothing more to find:
        growing-segment rows are scored without the query's filter and take
        top-k slots, and so can rows this snapshot cannot see.

        Only `bm25_limit = -1` reads every posting of the query terms and leaves
        visibility and the filter to the executor, so only it is complete.

        What it costs is the query's own postings plus the growing segment, and
        one heap check per candidate the executor reads before the page fills.
        The corpus size is not the measure, and a short page is no reason to
        refuse. That page already read every posting it could: pruning only
        starts once the internal top-k holds more than twice its size. Measured
        on a 2.1M-chunk corpus, completing ranged from 60 ms to 2.5 s. The
        widening probes and the refusal this replaces took 0.1-8 s, and marked
        the search degraded (akb#673).
        """
        if configured_budget == -1:
            return await ranked()  # the operator already asked for exact
        if limit > _VCHORD_MAX_CANDIDATES:
            await _set_vchord_candidate_budget(conn, -1)
            return await ranked()
        await _set_vchord_candidate_budget(conn, max(limit, configured_budget, 1))
        page = await ranked()
        if len(page) >= limit:
            return page
        await _set_vchord_candidate_budget(conn, -1)
        return await ranked()

    async def _fetch_payloads(
        self,
        conn: asyncpg.Connection,
        chunk_ids: list[str],
    ) -> list[dict]:
        if not chunk_ids:
            return []
        rows = await conn.fetch(
            f"""
            SELECT chunk_id::text AS chunk_id,
                   source_type, source_id::text AS source_id,
                   section_path, content
            FROM "{self._schema}".chunks
            WHERE chunk_id = ANY($1::uuid[])
            """,
            [uuid.UUID(c) for c in chunk_ids],
        )
        return [dict(r) for r in rows]


def _row_to_hit(row: dict, *, score: float) -> VectorHit:
    return VectorHit(
        chunk_id=row["chunk_id"],
        source_type=row.get("source_type") or "document",
        source_id=row.get("source_id") or "",
        section_path=row.get("section_path") or "",
        content=row.get("content") or "",
        score=float(score),
    )
