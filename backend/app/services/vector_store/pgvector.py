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

Sparse storage shape is selected at construction time:

  posting  — chunks(...) + posting(term_id, chunk_id, weight),
             B-tree-indexed on term_id. Sparse search is a single
             indexed lookup with application-owned BM25 weights.
  vchord  — raw integer TF in bm25vector; the BM25 index owns scoring
             and corpus statistics. Exact fallback is size/time bounded.
  arrays   — chunks(sparse_terms BIGINT[], sparse_weights REAL[]).
             One row per chunk. Sparse search unnest+JOIN+GROUP BY.
             RETAINED for the bench harness only — don't pick this
             for production. May be removed in a future cleanup.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import assert_never

import asyncpg

from app.services.sparse_shapes import SparseShape

from .base import ChunkUpsert, VectorHit, VectorStoreUnavailable, has_dense


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
# Exact ranking is reserved for small corpora/scopes. Even the bounded
# cardinality probe can inspect a large heap, so ranking SQL also has a
# server deadline and the complete sparse search has a wall-clock deadline.
_VCHORD_MAX_EXACT_ROWS = 10_000
_VCHORD_SEARCH_SECONDS = 5.0


async def _set_vchord_candidate_budget(
    conn: asyncpg.Connection, budget: int,
) -> None:
    if budget != -1 and not 1 <= budget <= _VCHORD_MAX_CANDIDATES:
        raise ValueError(f"invalid vchord candidate budget: {budget}")
    # The bounded integer is safe to interpolate into the extension GUC.
    # SET LOCAL restores the pooled connection at transaction commit.
    await conn.execute(f"SET LOCAL bm25_catalog.bm25_limit = {budget}")

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
        self._get_main_pool = get_main_pool
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
            await conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_vi_chunks_bm25
                    ON "{self._schema}".chunks
                 USING bm25 (sparse_bm25 bm25_catalog.bm25_ops)
                """
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
                    # Replace posting rows for this chunk.
                    await c.execute(
                        f'DELETE FROM "{self._schema}".posting WHERE chunk_id = $1',
                        cid,
                    )
                    if sparse_indices:
                        await c.executemany(
                            f"""
                            INSERT INTO "{self._schema}".posting
                                (term_id, chunk_id, weight)
                            VALUES ($1, $2, $3)
                            """,
                            [
                                (int(t), cid, float(w))
                                for t, w in zip(sparse_indices, sparse_values)
                            ],
                        )
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
        pool = await self._pool()
        timings: dict[str, float] = {}

        async def _dense_leg() -> list[str]:
            assert query_dense is not None  # gated by has_dense in caller; for mypy
            begin = time.perf_counter()
            try:
                async with pool.acquire() as c:
                    timings["dense_wait"] = time.perf_counter() - begin
                    await self._ensure_codec(c)
                    return await self._search_dense(
                        c, query_dense=query_dense,
                        filter_uuids=filter_uuids, filter_col=filter_col,
                        source_type_values=source_type_values,
                        limit=prefetch_per_leg,
                    )
            finally:
                timings["dense"] = time.perf_counter() - begin

        async def _sparse_leg() -> list[str]:
            begin = time.perf_counter()
            try:
                async with pool.acquire() as c:
                    timings["sparse_wait"] = time.perf_counter() - begin
                    await self._ensure_codec(c)
                    return await self._search_sparse(
                        c, terms=list(query_sparse_indices),
                        weights=list(query_sparse_values),
                        filter_uuids=filter_uuids, filter_col=filter_col,
                        source_type_values=source_type_values,
                        limit=prefetch_per_leg,
                    )
            finally:
                timings["sparse"] = time.perf_counter() - begin

        succeeded = False
        try:
            # Two legs run in parallel — same PG, different conns. asyncpg
            # serialises queries on a single conn, so the two legs need
            # two conns. The pool max (default 8) accommodates this even
            # under burst.
            if has_dense and has_sparse:
                dense_ids, sparse_ids = await asyncio.gather(
                    _dense_leg(), _sparse_leg(),
                )
            elif has_dense:
                dense_ids = await _dense_leg()
                sparse_ids = []
            else:
                dense_ids = []
                sparse_ids = await _sparse_leg()

            # Single-leg paths skip RRF.
            if has_dense and not has_sparse:
                top_ids = dense_ids[:limit]
                scoring = [(cid, 1.0 / (RRF_K + i)) for i, cid in enumerate(top_ids, start=1)]
            elif has_sparse and not has_dense:
                top_ids = sparse_ids[:limit]
                scoring = [(cid, 1.0 / (RRF_K + i)) for i, cid in enumerate(top_ids, start=1)]
            else:
                fused = _rrf(dense_ids, sparse_ids)
                scoring = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:limit]
                top_ids = [cid for cid, _ in scoring]

            payload_started = time.perf_counter()
            async with pool.acquire() as c:
                await self._ensure_codec(c)
                rows = await self._fetch_payloads(c, top_ids)
            timings["payload"] = time.perf_counter() - payload_started
            by_id = {r["chunk_id"]: r for r in rows}
            hits = [
                _row_to_hit(by_id[cid], score=score)
                for cid, score in scoring
                if cid in by_id
            ]
            succeeded = True
            return hits
        except (asyncpg.PostgresError, TimeoutError) as e:
            raise VectorStoreUnavailable(f"search failed: {e}") from e
        finally:
            # No query text, source IDs, content, or credentials in diagnostics.
            # Legs overlap; their durations include pool wait and are not additive.
            logger.info(
                "hybrid_timing ok=%s filter=%s filter_count=%d terms=%d "
                "dense_ms=%.2f dense_wait_ms=%.2f sparse_ms=%.2f sparse_wait_ms=%.2f "
                "payload_ms=%.2f total_ms=%.2f",
                succeeded, filter_col if filter_uuids is not None else "none",
                len(filter_uuids) if filter_uuids is not None else 0,
                len(query_sparse_indices),
                *(timings.get(key, 0.0) * 1000 for key in (
                    "dense", "dense_wait", "sparse", "sparse_wait", "payload",
                )),
                (time.perf_counter() - started) * 1000,
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
        # Binary codec → list[float] passes through directly.
        # `WHERE dense IS NOT NULL` mirrors the partial HNSW index above —
        # sparse-only points (embed API was down when they were indexed)
        # contribute only to the sparse leg, never to the dense KNN.
        # `filter_col` is "source_id" or "vault_id" (literal — see hybrid_search).
        # `source_type_values`, when present, ANDs an additional
        # `source_type = ANY(...)` predicate (workbench #1069). The bind slot
        # is always $4 in the filtered branch and $3 in the unfiltered branch
        # (params: dense, [uuids,] limit, types) so planner shapes stay stable.
        type_suffix_filtered = (
            " AND source_type = ANY($4::text[])" if source_type_values else ""
        )
        type_suffix_unfiltered = (
            " AND source_type = ANY($3::text[])" if source_type_values else ""
        )
        if filter_uuids:
            # HNSW post-filters: it walks the graph for ~`ef_search` GLOBAL
            # nearest, THEN drops the ones failing the WHERE. With a selective
            # filter (one user's vaults/docs out of the whole corpus) most of
            # the global top-ef live in OTHER vaults, so a plain query returns
            # only the handful that survive — severe under-retrieval (a query
            # whose global-nearest sit in other vaults came back with ~1 hit
            # while the corpus held dozens). `hnsw.iterative_scan` (pgvector
            # >= 0.8) makes the index keep scanning until `limit` filtered rows
            # are found, bounded by `hnsw.max_scan_tuples`. relaxed_order is
            # fine — the dense leg is re-ranked by RRF + cross-encoder anyway.
            # SET LOCAL scopes it to this transaction so the pooled conn resets.
            async with conn.transaction():
                await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
                # ef_search bumped from the default 40 so iterative scan has a
                # wider beam before it starts re-scanning (fewer scan rounds).
                await conn.execute("SET LOCAL hnsw.ef_search = 200")
                rows = await conn.fetch(
                    f"""
                    SELECT chunk_id::text AS chunk_id
                    FROM "{self._schema}".chunks
                    WHERE {filter_col} = ANY($2::uuid[]) AND dense IS NOT NULL{type_suffix_filtered}
                    ORDER BY dense <=> $1
                    LIMIT $3
                    """,
                    list(query_dense), filter_uuids, int(limit),
                    *([source_type_values] if source_type_values else []),
                )
        else:
            rows = await conn.fetch(
                f"""
                SELECT chunk_id::text AS chunk_id
                FROM "{self._schema}".chunks
                WHERE dense IS NOT NULL{type_suffix_unfiltered}
                ORDER BY dense <=> $1
                LIMIT $2
                """,
                list(query_dense), int(limit),
                *([source_type_values] if source_type_values else []),
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
            if filter_uuids:
                if filter_col == "source_id":
                    # Source lists can contain thousands of IDs while a common
                    # term can match almost the whole corpus. Bound the join by
                    # materializing the authorized chunk IDs first. This only
                    # changes join order; terms, weights and scores are intact.
                    sql = f"""
                        WITH q AS (
                          SELECT unnest($1::bigint[]) AS tid,
                                 unnest($2::real[])   AS w
                        ),
                        candidate_chunks AS MATERIALIZED (
                          SELECT chunk_id
                          FROM "{self._schema}".chunks
                          WHERE source_id = ANY($3::uuid[])
                            {"AND source_type = ANY($5::text[])" if source_type_values else ""}
                        )
                        SELECT p.chunk_id::text AS chunk_id,
                               SUM(q.w * p.weight) AS score
                        FROM candidate_chunks c
                        JOIN "{self._schema}".posting p ON p.chunk_id = c.chunk_id
                        JOIN q ON q.tid = p.term_id
                        GROUP BY p.chunk_id
                        ORDER BY score DESC
                        LIMIT $4
                    """
                else:
                    # A vault filter can cover most of the corpus. Forcing that
                    # large set into a materialized CTE would increase memory and
                    # temporary-I/O pressure, so leave its direct join intact.
                    sql = f"""
                        WITH q AS (
                          SELECT unnest($1::bigint[]) AS tid,
                                 unnest($2::real[])   AS w
                        )
                        SELECT p.chunk_id::text AS chunk_id,
                               SUM(q.w * p.weight) AS score
                        FROM "{self._schema}".posting p
                        JOIN q ON q.tid = p.term_id
                        JOIN "{self._schema}".chunks c ON c.chunk_id = p.chunk_id
                        WHERE c.{filter_col} = ANY($3::uuid[])
                          {"AND c.source_type = ANY($5::text[])" if source_type_values else ""}
                        GROUP BY p.chunk_id
                        ORDER BY score DESC
                        LIMIT $4
                    """
                rows = await conn.fetch(
                    sql, list(terms), [float(w) for w in weights],
                    filter_uuids, int(limit),
                    *([source_type_values] if source_type_values else []),
                )
            elif source_type_values:
                sql = f"""
                    WITH q AS (
                      SELECT unnest($1::bigint[]) AS tid,
                             unnest($2::real[])   AS w
                    )
                    SELECT p.chunk_id::text AS chunk_id,
                           SUM(q.w * p.weight) AS score
                    FROM "{self._schema}".posting p
                    JOIN q ON q.tid = p.term_id
                    JOIN "{self._schema}".chunks c ON c.chunk_id = p.chunk_id
                    WHERE c.source_type = ANY($4::text[])
                    GROUP BY p.chunk_id
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
                    SELECT p.chunk_id::text AS chunk_id,
                           SUM(q.w * p.weight) AS score
                    FROM "{self._schema}".posting p
                    JOIN q ON q.tid = p.term_id
                    GROUP BY p.chunk_id
                    ORDER BY score DESC
                    LIMIT $3
                """
                rows = await conn.fetch(
                    sql, list(terms), [float(w) for w in weights], int(limit),
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
            # `plan_cache_mode` is set because asyncpg always prepares, and a
            # generic plan is built without the filter's values: the same
            # statement measured 0.8ms for ten executions and then 1500ms once
            # PostgreSQL switched. SET LOCAL scopes it to this transaction so
            # the pooled connection is not left altered.
            query_terms = [int(t) for t in terms]
            # Read the statistics BEFORE the transaction opens. Inside it, a
            # server-side error aborts the transaction, and catching the
            # exception in Python does not un-abort it — the next statement
            # fails with InFailedSQLTransactionError, which is a PostgresError,
            # which `hybrid_search` turns into VectorStoreUnavailable and takes
            # the dense leg down with it. "A statistics read that throws must
            # not take a search with it" is only true out here.
            deadline = asyncio.get_running_loop().time() + _VCHORD_SEARCH_SECONDS
            async with asyncio.timeout_at(deadline):
                selective = (
                    await self._filter_is_selective(conn, filter_col, filter_uuids)
                    if filter_uuids
                    else False
                )
            async with asyncio.timeout_at(deadline), conn.transaction():
                current_timeout = int(await conn.fetchval(
                    "SELECT setting FROM pg_settings WHERE name = 'statement_timeout'"
                ))
                timeout_ms = int(_VCHORD_SEARCH_SECONDS * 1000)
                if current_timeout > 0:
                    timeout_ms = min(timeout_ms, current_timeout)
                await conn.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
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

                if filter_uuids and selective:
                    await self._check_vchord_exact_scope(
                        conn, filter_col=filter_col, filter_uuids=filter_uuids,
                        source_type_values=source_type_values,
                    )
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
                                    $1::int[]::bm25_catalog.bm25vector) AS score
                          FROM candidate_chunks
                          ORDER BY score
                          LIMIT $3
                        ) ranked WHERE score < 0
                        ORDER BY score
                    """
                    rows = await conn.fetch(
                        sql, query_terms, filter_uuids, requested_limit,
                        *([source_type_values] if source_type_values else []),
                    )
                elif filter_uuids or source_type_values:
                    # Sealed segments can apply executor predicates during the
                    # extension scan; growing segments score before that hook.
                    # Preserve the direct fast path, and only use unqualified
                    # global candidates to choose widening or exact fallback.
                    configured_budget = int(
                        await conn.fetchval(
                            "SELECT current_setting('bm25_catalog.bm25_limit')::integer"
                        )
                    )
                    return await self._search_vchord_index_led_filtered(
                        conn,
                        query_terms=query_terms,
                        filter_col=filter_col,
                        filter_uuids=filter_uuids,
                        source_type_values=source_type_values,
                        limit=requested_limit,
                        configured_budget=configured_budget,
                    )
                else:
                    configured_budget = int(
                        await conn.fetchval(
                            "SELECT current_setting('bm25_catalog.bm25_limit')::integer"
                        )
                    )
                    sql = f"""
                        SELECT chunk_id FROM (
                          SELECT chunk_id::text AS chunk_id,
                                 sparse_bm25 <&> bm25_catalog.to_bm25query(
                                    '"{self._schema}".idx_vi_chunks_bm25'::regclass,
                                    $1::int[]::bm25_catalog.bm25vector) AS score
                          FROM "{self._schema}".chunks
                          WHERE sparse_bm25 IS NOT NULL
                          ORDER BY score
                          LIMIT $2
                        ) ranked WHERE score < 0
                        ORDER BY score
                    """
                    if configured_budget == -1 or requested_limit > _VCHORD_MAX_CANDIDATES:
                        await self._check_vchord_exact_scope(conn)
                    candidate_budget = configured_budget
                    if configured_budget != -1:
                        candidate_budget = (
                            -1
                            if requested_limit > _VCHORD_MAX_CANDIDATES
                            else max(requested_limit, configured_budget, 1)
                        )
                        await _set_vchord_candidate_budget(conn, candidate_budget)
                    rows = await conn.fetch(sql, query_terms, requested_limit)
                    if (
                        candidate_budget != -1
                        and len(rows) < requested_limit
                    ):
                        await self._check_vchord_exact_scope(conn)
                        await _set_vchord_candidate_budget(conn, -1)
                        rows = await conn.fetch(
                            sql, query_terms, requested_limit,
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
    # both shapes return the same rows.
    _SELECTIVE_FRACTION = 0.01

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

    async def _check_vchord_exact_scope(
        self,
        conn: asyncpg.Connection,
        *,
        filter_col: str = "vault_id",
        filter_uuids: list[uuid.UUID] | None = None,
        source_type_values: list[str] | None = None,
    ) -> None:
        """Bound actual rows, not planner estimates, before exact scoring.

        An index scan with bm25_limit=-1 may examine the global corpus before
        applying filters, so its callers check the *global* size. Only the
        materialized path can safely check the filtered scope instead.
        """
        predicates = ["sparse_bm25 IS NOT NULL"]
        args: list[object] = []
        if filter_uuids:
            args.append(filter_uuids)
            predicates.append(f"{filter_col} = ANY(${len(args)}::uuid[])")
        if source_type_values:
            args.append(source_type_values)
            predicates.append(f"source_type = ANY(${len(args)}::text[])")
        count = await conn.fetchval(
            f"""SELECT count(*) FROM (
                SELECT 1 FROM "{self._schema}".chunks
                WHERE {" AND ".join(predicates)}
                LIMIT {_VCHORD_MAX_EXACT_ROWS + 1}
            ) bounded_exact_scope""", *args,
        )
        if count > _VCHORD_MAX_EXACT_ROWS:
            raise VectorStoreUnavailable(
                "VChord exact search exceeds the bounded row budget"
            )

    async def _search_vchord_index_led_filtered(
        self,
        conn: asyncpg.Connection,
        *,
        query_terms: list[int],
        filter_col: str,
        filter_uuids: list[uuid.UUID] | None,
        source_type_values: list[str] | None,
        limit: int,
        configured_budget: int,
    ) -> list[str]:
        """Search an index-led vchord scope without silently losing top-k rows.

        The first query preserves VectorChord's sealed-segment prefilter. If it
        underfills, a separate global-candidate query counts an unqualified
        candidate page before choosing bounded widening or exact fallback.
        Growing segments do not use the extension prefilter, and nonvisible
        rows can consume an index page, so only the exact fallback proves
        exhaustion.
        """

        def scope(
            first_parameter: int,
        ) -> tuple[list[str], list[object], int]:
            predicates: list[str] = []
            params: list[object] = []
            parameter = first_parameter
            if filter_uuids:
                predicates.append(f"c.{filter_col} = ANY(${parameter}::uuid[])")
                params.append(filter_uuids)
                parameter += 1
            if source_type_values:
                predicates.append(f"c.source_type = ANY(${parameter}::text[])")
                params.append(source_type_values)
                parameter += 1
            return predicates, params, parameter

        filtered_predicates, filtered_scope_args, filtered_limit_parameter = scope(2)
        filtered_where = " AND ".join(
            ["c.sparse_bm25 IS NOT NULL", *filtered_predicates]
        )
        filtered_sql = f"""
            SELECT chunk_id FROM (
              SELECT c.chunk_id::text AS chunk_id,
                     c.sparse_bm25 <&> bm25_catalog.to_bm25query(
                        '"{self._schema}".idx_vi_chunks_bm25'::regclass,
                        $1::int[]::bm25_catalog.bm25vector) AS score
              FROM "{self._schema}".chunks c
              WHERE {filtered_where}
              ORDER BY score
              LIMIT ${filtered_limit_parameter}
            ) ranked WHERE score < 0
            ORDER BY score
        """
        filtered_args: list[object] = [query_terms, *filtered_scope_args, limit]

        async def filtered_hits() -> list[str]:
            rows = await conn.fetch(filtered_sql, *filtered_args)
            return [row["chunk_id"] for row in rows]

        # -1 is the extension's exact brute-force mode. A requested SQL page
        # larger than its finite maximum also needs that mode to remain exact.
        if configured_budget == -1:
            await self._check_vchord_exact_scope(conn)
            return await filtered_hits()
        if limit > _VCHORD_MAX_CANDIDATES:
            await self._check_vchord_exact_scope(conn)
            await _set_vchord_candidate_budget(conn, -1)
            return await filtered_hits()

        candidate_budget = max(
            limit,
            configured_budget if configured_budget > 0 else 1,
            1,
        )
        await _set_vchord_candidate_budget(conn, candidate_budget)

        # Keep the extension's direct filtered path as the fast path. On sealed
        # segments ENABLE_PREFILTER can apply this scope while scanning.
        hits = await filtered_hits()
        if len(hits) >= limit:
            return hits

        global_predicates, global_scope_args, global_limit_parameter = scope(3)
        global_where = " AND ".join(global_predicates)
        global_sql = f"""
            WITH global_candidates AS MATERIALIZED (
              SELECT c.chunk_id,
                     c.sparse_bm25 <&> bm25_catalog.to_bm25query(
                        '"{self._schema}".idx_vi_chunks_bm25'::regclass,
                        $1::int[]::bm25_catalog.bm25vector) AS score
              FROM "{self._schema}".chunks c
              WHERE c.sparse_bm25 IS NOT NULL
              ORDER BY score
              LIMIT $2
            ),
            ranked AS (
              SELECT g.chunk_id::text AS chunk_id, g.score
              FROM global_candidates g
              JOIN "{self._schema}".chunks c ON c.chunk_id = g.chunk_id
              WHERE {global_where} AND g.score < 0
              ORDER BY g.score
              LIMIT ${global_limit_parameter}
            )
            SELECT
              (SELECT COUNT(*) FROM global_candidates) AS candidate_count,
              ARRAY(SELECT chunk_id FROM ranked ORDER BY score) AS chunk_ids
        """

        while True:
            probe_args: list[object] = [
                query_terms,
                candidate_budget,
                *global_scope_args,
                limit,
            ]
            probe = await conn.fetchrow(global_sql, *probe_args)
            assert probe is not None
            candidate_count = int(probe["candidate_count"])
            candidate_ids = list(probe["chunk_ids"])

            if len(candidate_ids) >= limit:
                return candidate_ids
            if (
                candidate_count < candidate_budget
                or candidate_budget == _VCHORD_MAX_CANDIDATES
            ):
                await self._check_vchord_exact_scope(conn)
                await _set_vchord_candidate_budget(conn, -1)
                return await filtered_hits()

            candidate_budget = min(
                _VCHORD_MAX_CANDIDATES,
                candidate_budget * 4,
            )
            await _set_vchord_candidate_budget(conn, candidate_budget)

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
