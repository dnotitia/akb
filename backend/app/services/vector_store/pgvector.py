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
import logging
import re
import uuid
from contextlib import asynccontextmanager
from typing import Literal

import asyncpg

from .base import VectorHit, VectorStoreUnavailable

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

    async def _pool(self) -> asyncpg.Pool:
        """Return the pool we read/write through."""
        if self._dsn is None:
            if self._get_main_pool is None:
                raise RuntimeError(
                    "PgvectorStore: dsn is blank and no main pool factory was provided"
                )
            return await self._get_main_pool()
        if self._own_pool is None:
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
        """Idempotent schema creation. Only the first call hits the DB.
        Reuses `conn` so first-write boots the schema in the caller's
        transaction (one round-trip, atomic with the first chunk)."""
        if self._ensured_collection:
            return
        try:
            async with self._conn(conn) as c:
                await self._do_ensure(c)
        except asyncpg.PostgresError as e:
            raise VectorStoreUnavailable(f"schema setup failed: {e}") from e
        self._ensured_collection = True

    async def _do_ensure(self, conn) -> None:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"')

        if self._sparse_shape == "arrays":
            await conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS "{self._schema}".chunks (
                    chunk_id        UUID PRIMARY KEY,
                    source_type     TEXT NOT NULL,
                    source_id       UUID NOT NULL,
                    section_path    TEXT,
                    content         TEXT NOT NULL,
                    chunk_index     INTEGER NOT NULL,
                    dense           vector({self._dense_dim}) NOT NULL,
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
                    section_path    TEXT,
                    content         TEXT NOT NULL,
                    chunk_index     INTEGER NOT NULL,
                    dense           vector({self._dense_dim}) NOT NULL,
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

        # Common indexes (both shapes).
        await conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_vi_chunks_source_id
                ON "{self._schema}".chunks (source_id)
            """
        )
        # HNSW for dense KNN. m=16, ef_construction=64 are pgvector's
        # defaults and serve us well at <1M points.
        await conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_vi_chunks_dense
                ON "{self._schema}".chunks
                USING hnsw (dense vector_cosine_ops)
                WITH (m = 16, ef_construction = 64)
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

    # ── Upsert ────────────────────────────────────────────────────

    async def upsert_one(
        self,
        *,
        conn=None,
        chunk_id: str,
        content: str,
        section_path: str | None,
        chunk_index: int,
        dense: list[float],
        sparse_indices: list[int],
        sparse_values: list[float],
        source_type: str,
        source_id: str,
    ) -> None:
        await self.ensure_collection(conn=conn)
        cid = uuid.UUID(str(chunk_id))
        sid = uuid.UUID(str(source_id))
        # `dense` goes through pgvector's binary codec — list[float]
        # straight into asyncpg's bind. No text literal, no `::vector`
        # cast needed.
        try:
            async with self._conn(conn) as c:
                if self._sparse_shape == "arrays":
                    await c.execute(
                        f"""
                        INSERT INTO "{self._schema}".chunks
                            (chunk_id, source_type, source_id, section_path,
                             content, chunk_index, dense,
                             sparse_terms, sparse_weights, indexed_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW())
                        ON CONFLICT (chunk_id) DO UPDATE SET
                            source_type    = EXCLUDED.source_type,
                            source_id      = EXCLUDED.source_id,
                            section_path   = EXCLUDED.section_path,
                            content        = EXCLUDED.content,
                            chunk_index    = EXCLUDED.chunk_index,
                            dense          = EXCLUDED.dense,
                            sparse_terms   = EXCLUDED.sparse_terms,
                            sparse_weights = EXCLUDED.sparse_weights,
                            indexed_at     = NOW()
                        """,
                        cid, source_type, sid, section_path or "",
                        content, int(chunk_index), list(dense),
                        list(sparse_indices), [float(v) for v in sparse_values],
                    )
                else:  # posting
                    await c.execute(
                        f"""
                        INSERT INTO "{self._schema}".chunks
                            (chunk_id, source_type, source_id, section_path,
                             content, chunk_index, dense, indexed_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                        ON CONFLICT (chunk_id) DO UPDATE SET
                            source_type  = EXCLUDED.source_type,
                            source_id    = EXCLUDED.source_id,
                            section_path = EXCLUDED.section_path,
                            content      = EXCLUDED.content,
                            chunk_index  = EXCLUDED.chunk_index,
                            dense        = EXCLUDED.dense,
                            indexed_at   = NOW()
                        """,
                        cid, source_type, sid, section_path or "",
                        content, int(chunk_index), list(dense),
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

    async def delete_point(self, chunk_id: str, *, conn=None) -> None:
        await self.ensure_collection(conn=conn)
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
    ) -> list[VectorHit]:
        del query_text  # debug-only on this driver; keep signature parity
        await self.ensure_collection()

        has_dense = query_dense is not None and len(query_dense) > 0
        has_sparse = len(query_sparse_indices) > 0
        if not has_dense and not has_sparse:
            return []

        src_uuids = [uuid.UUID(str(s)) for s in (source_ids or [])]
        src_filter = src_uuids if src_uuids else None
        pool = await self._pool()

        async def _dense_leg() -> list[str]:
            async with pool.acquire() as c:
                await self._ensure_codec(c)
                return await self._search_dense(
                    c, query_dense=query_dense,
                    src_uuids=src_filter, limit=prefetch_per_leg,
                )

        async def _sparse_leg() -> list[str]:
            async with pool.acquire() as c:
                await self._ensure_codec(c)
                return await self._search_sparse(
                    c, terms=list(query_sparse_indices),
                    weights=list(query_sparse_values),
                    src_uuids=src_filter, limit=prefetch_per_leg,
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
        src_uuids: list[uuid.UUID] | None,
        limit: int,
    ) -> list[str]:
        # Binary codec → list[float] passes through directly.
        if src_uuids:
            rows = await conn.fetch(
                f"""
                SELECT chunk_id::text AS chunk_id
                FROM "{self._schema}".chunks
                WHERE source_id = ANY($2::uuid[])
                ORDER BY dense <=> $1
                LIMIT $3
                """,
                list(query_dense), src_uuids, int(limit),
            )
        else:
            rows = await conn.fetch(
                f"""
                SELECT chunk_id::text AS chunk_id
                FROM "{self._schema}".chunks
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
        src_uuids: list[uuid.UUID] | None,
        limit: int,
    ) -> list[str]:
        if not terms:
            return []

        if self._sparse_shape == "arrays":
            # Two query branches (with/without source filter) keep the
            # planner honest — a single SQL with `WHERE $3 IS NULL OR
            # source_id = ANY($3)` confuses ANY-cardinality estimation.
            if src_uuids:
                sql = f"""
                    WITH q AS (
                      SELECT unnest($1::bigint[]) AS tid,
                             unnest($2::real[])   AS w
                    ),
                    cand AS (
                      SELECT chunk_id, sparse_terms, sparse_weights
                      FROM "{self._schema}".chunks
                      WHERE source_id = ANY($3::uuid[])
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
                    src_uuids, int(limit),
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
            if src_uuids:
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
                    WHERE c.source_id = ANY($3::uuid[])
                    GROUP BY p.chunk_id
                    ORDER BY score DESC
                    LIMIT $4
                """
                rows = await conn.fetch(
                    sql, list(terms), [float(w) for w in weights],
                    src_uuids, int(limit),
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
