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
             indexed lookup. Default and recommended at any scale
             where you actually care about latency.
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
import uuid
from contextlib import asynccontextmanager
from typing import Literal

import asyncpg

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


SparseShape = Literal["arrays", "posting"]


# RRF constant (Qdrant's default). Same value across drivers so the
# `score` field has consistent semantics — the absolute number still
# isn't comparable across drivers (per the VectorHit contract), but
# at least the formula is identical.
RRF_K = 60

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
        else:  # posting
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
                    PRIMARY KEY (term_id, chunk_id)
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
                ON "{self._schema}".chunks (source_id)
            """
        )
        await conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_vi_chunks_vault_id
                ON "{self._schema}".chunks (vault_id)
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
                else:  # posting
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
        Protocol contract while keeping per-call atomicity unchanged."""
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
    ) -> list[VectorHit]:
        del query_text  # debug-only on this driver; keep signature parity
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
        pool = await self._pool()

        async def _dense_leg() -> list[str]:
            assert query_dense is not None  # gated by has_dense in caller; for mypy
            async with pool.acquire() as c:
                await self._ensure_codec(c)
                return await self._search_dense(
                    c, query_dense=query_dense,
                    filter_uuids=filter_uuids, filter_col=filter_col,
                    limit=prefetch_per_leg,
                )

        async def _sparse_leg() -> list[str]:
            async with pool.acquire() as c:
                await self._ensure_codec(c)
                return await self._search_sparse(
                    c, terms=list(query_sparse_indices),
                    weights=list(query_sparse_values),
                    filter_uuids=filter_uuids, filter_col=filter_col,
                    limit=prefetch_per_leg,
                )

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

            async with pool.acquire() as c:
                await self._ensure_codec(c)
                rows = await self._fetch_payloads(c, top_ids)
            by_id = {r["chunk_id"]: r for r in rows}
            return [
                _row_to_hit(by_id[cid], score=score)
                for cid, score in scoring
                if cid in by_id
            ]
        except asyncpg.PostgresError as e:
            raise VectorStoreUnavailable(f"search failed: {e}") from e

    async def _search_dense(
        self,
        conn: asyncpg.Connection,
        *,
        query_dense: list[float],
        filter_uuids: list[uuid.UUID] | None,
        filter_col: str,
        limit: int,
    ) -> list[str]:
        # Binary codec → list[float] passes through directly.
        # `WHERE dense IS NOT NULL` mirrors the partial HNSW index above —
        # sparse-only points (embed API was down when they were indexed)
        # contribute only to the sparse leg, never to the dense KNN.
        # `filter_col` is "source_id" or "vault_id" (literal — see hybrid_search).
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
                    WHERE {filter_col} = ANY($2::uuid[]) AND dense IS NOT NULL
                    ORDER BY dense <=> $1
                    LIMIT $3
                    """,
                    list(query_dense), filter_uuids, int(limit),
                )
        else:
            rows = await conn.fetch(
                f"""
                SELECT chunk_id::text AS chunk_id
                FROM "{self._schema}".chunks
                WHERE dense IS NOT NULL
                ORDER BY dense <=> $1
                LIMIT $2
                """,
                list(query_dense), int(limit),
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
        limit: int,
    ) -> list[str]:
        if not terms:
            return []

        if self._sparse_shape == "arrays":
            # Two query branches (with/without filter) keep the planner honest —
            # a single SQL with `WHERE $3 IS NULL OR <col> = ANY($3)` confuses
            # ANY-cardinality estimation. `filter_col` is "source_id"/"vault_id"
            # (literal — see hybrid_search), so the interpolation is safe.
            if filter_uuids:
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
        else:  # posting
            if filter_uuids:
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
                    GROUP BY p.chunk_id
                    ORDER BY score DESC
                    LIMIT $4
                """
                rows = await conn.fetch(
                    sql, list(terms), [float(w) for w in weights],
                    filter_uuids, int(limit),
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

        return [r["chunk_id"] for r in rows]

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
