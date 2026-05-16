"""Benchmark: pgvector sparse-storage shape — `arrays` vs `posting`.

Phase 3 of the vector-store driver abstraction calls for benchmarking
both shapes before locking one in. This script:

  1. Builds two scratch schemas in the configured DB:
       bench_arrays.chunks
       bench_posting.chunks + bench_posting.posting
  2. Samples N rows from the live `chunks` table (with embeddings) +
     synthesizes BM25 sparse vectors via `sparse_encoder.encode_document`.
  3. Times dense-only, sparse-only, and hybrid (RRF, prefetch=50) over
     M random queries with realistic source_id pre-filters.
  4. Reports p50 / p95 / p99 latency for each (shape × leg) cell.
  5. Drops the scratch schemas.

Usage:
    DATABASE_URL=postgres://akb:PW@host:5432/akb \\
    python backend/tests/bench/sparse_shape_bench.py --rows 50000 --queries 100

Both schemas are isolated under their own names; running this on a
shared cluster is safe as long as `bench_arrays` / `bench_posting`
schema names aren't already in use.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from statistics import median

# Allow `from app...` imports when run from repo root or in-container.
_HERE = Path(__file__).resolve()
for cand in (_HERE.parents[3] / "backend" if len(_HERE.parents) > 3 else None,
             Path("/app")):
    if cand and (cand / "app").exists():
        sys.path.insert(0, str(cand))
        break

import asyncpg  # noqa: E402

from app.services import sparse_encoder  # noqa: E402
from app.services.vector_store.pgvector import PgvectorStore  # noqa: E402


def _vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in vec) + "]"


def _percentile(samples: list[float], p: float) -> float:
    if not samples:
        return 0.0
    s = sorted(samples)
    k = int(round((p / 100.0) * (len(s) - 1)))
    return s[k]


def _summarize(label: str, samples: list[float]) -> str:
    if not samples:
        return f"{label}: no samples"
    return (
        f"{label:32s}  n={len(samples):4d}  "
        f"p50={_percentile(samples, 50)*1000:7.1f}ms  "
        f"p95={_percentile(samples, 95)*1000:7.1f}ms  "
        f"p99={_percentile(samples, 99)*1000:7.1f}ms  "
        f"median={median(samples)*1000:7.1f}ms"
    )


@asynccontextmanager
async def temp_pool(dsn: str):
    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=8, command_timeout=60)
    try:
        yield pool
    finally:
        await pool.close()


async def sample_corpus(
    pool: asyncpg.Pool, *, n: int, dense_dim: int
) -> list[dict]:
    """Pull N rows from the live chunks table. Truncates the live
    embedding (any source dim) to `dense_dim` — we're benchmarking
    SQL latency, not retrieval quality, so any consistent prefix
    works for the dense leg."""
    # TABLESAMPLE SYSTEM is page-level (fast); ORDER BY random() would
    # sort the entire 567k-row chunks table and hits the asyncpg
    # default timeout. Over-sample by 50% to absorb the WHERE filter.
    pct = max(0.5, min(50.0, (n / 500_000.0) * 100.0 * 1.5))
    rows = await pool.fetch(
        f"""
        SELECT id::text AS chunk_id,
               source_type,
               source_id::text AS source_id,
               COALESCE(section_path, '') AS section_path,
               content,
               COALESCE(chunk_index, 0) AS chunk_index,
               embedding::text AS embedding_text
        FROM chunks TABLESAMPLE SYSTEM ({pct})
        WHERE embedding IS NOT NULL
        LIMIT $1
        """,
        n,
    )
    out = []
    for r in rows:
        emb_text = r["embedding_text"]
        if not emb_text:
            continue
        # pgvector text form: "[v1,v2,...]"
        try:
            full = [float(x) for x in emb_text.strip("[]").split(",")]
        except ValueError:
            continue
        if len(full) < dense_dim:
            continue
        dense = full[:dense_dim]  # truncate to bench dim
        out.append(
            {
                "chunk_id": r["chunk_id"],
                "source_type": r["source_type"] or "document",
                "source_id": r["source_id"],
                "section_path": r["section_path"],
                "content": r["content"] or "",
                "chunk_index": int(r["chunk_index"]),
                "dense": dense,
            }
        )
    return out


async def encode_sparse_for_corpus(corpus: list[dict]) -> None:
    """Augment each row in-place with sparse_indices/values."""
    for row in corpus:
        idx, val = await sparse_encoder.encode_document(row["content"])
        row["sparse_indices"] = idx
        row["sparse_values"] = val


async def populate_store(store: PgvectorStore, corpus: list[dict]) -> None:
    print(f"  populating {store._sparse_shape} ({len(corpus)} rows)…", flush=True)
    t0 = time.perf_counter()
    for row in corpus:
        await store.upsert_one(
            chunk_id=row["chunk_id"],
            content=row["content"],
            section_path=row["section_path"],
            chunk_index=row["chunk_index"],
            dense=row["dense"],
            sparse_indices=row["sparse_indices"],
            sparse_values=row["sparse_values"],
            source_type=row["source_type"],
            source_id=row["source_id"],
        )
    dt = time.perf_counter() - t0
    print(
        f"  populated {store._sparse_shape} in {dt:.1f}s "
        f"({len(corpus)/dt:.0f} rows/s)",
        flush=True,
    )


async def time_query(coro_factory, n: int) -> list[float]:
    """Run coro_factory() n times, return per-call wall-clock seconds."""
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        await coro_factory()
        samples.append(time.perf_counter() - t0)
    return samples


async def run_bench(
    store: PgvectorStore,
    *,
    queries: list[dict],
    iters_per_query: int,
    source_id_pool: list[str],
    prefetch: int,
    limit: int,
):
    """For each (query × leg) yield (label, samples)."""
    dense_only_samples: list[float] = []
    sparse_only_samples: list[float] = []
    hybrid_samples: list[float] = []

    for q in queries:
        # Random selective filter (5% of corpus on average).
        sample_size = max(1, len(source_id_pool) // 20)
        src_filter = random.sample(source_id_pool, sample_size)

        # Dense-only
        for _ in range(iters_per_query):
            t0 = time.perf_counter()
            await store.hybrid_search(
                query_text=q["text"],
                query_dense=q["dense"],
                query_sparse_indices=[],
                query_sparse_values=[],
                source_ids=src_filter,
                limit=limit,
                prefetch_per_leg=prefetch,
            )
            dense_only_samples.append(time.perf_counter() - t0)

        # Sparse-only
        for _ in range(iters_per_query):
            t0 = time.perf_counter()
            await store.hybrid_search(
                query_text=q["text"],
                query_dense=None,
                query_sparse_indices=q["sparse_indices"],
                query_sparse_values=q["sparse_values"],
                source_ids=src_filter,
                limit=limit,
                prefetch_per_leg=prefetch,
            )
            sparse_only_samples.append(time.perf_counter() - t0)

        # Hybrid (RRF)
        for _ in range(iters_per_query):
            t0 = time.perf_counter()
            await store.hybrid_search(
                query_text=q["text"],
                query_dense=q["dense"],
                query_sparse_indices=q["sparse_indices"],
                query_sparse_values=q["sparse_values"],
                source_ids=src_filter,
                limit=limit,
                prefetch_per_leg=prefetch,
            )
            hybrid_samples.append(time.perf_counter() - t0)

    return {
        "dense_only": dense_only_samples,
        "sparse_only": sparse_only_samples,
        "hybrid_rrf":  hybrid_samples,
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=20_000,
                    help="Corpus size to seed each scratch schema with")
    ap.add_argument("--queries", type=int, default=20,
                    help="Distinct query strings to test")
    ap.add_argument("--iters", type=int, default=3,
                    help="Repetitions per query per leg (averages across)")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--prefetch", type=int, default=50)
    ap.add_argument("--dense-dim", type=int, default=4096)
    ap.add_argument("--keep", action="store_true",
                    help="Don't drop scratch schemas at end")
    args = ap.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        sys.exit(2)

    print(f"== sparse-shape bench: rows={args.rows} queries={args.queries} "
          f"iters/leg={args.iters} dim={args.dense_dim}")

    async with temp_pool(dsn) as pool:
        # 1. Sample corpus (with dense embeddings) from live chunks.
        print("→ sampling corpus…", flush=True)
        corpus = await sample_corpus(pool, n=args.rows, dense_dim=args.dense_dim)
        if len(corpus) < args.rows * 0.5:
            print(
                f"  WARN: only {len(corpus)} usable rows (asked for {args.rows}). "
                f"Embedding-API outage or dim mismatch?"
            )
        print(f"  got {len(corpus)} rows", flush=True)

        # 2. Encode BM25 sparse for all rows.
        print("→ encoding sparse vectors (BM25) for corpus…", flush=True)
        await encode_sparse_for_corpus(corpus)

        # 3. Build query set: pick diverse content snippets, tokenize.
        print("→ building query set…", flush=True)
        queries = []
        for row in random.sample(corpus, min(args.queries, len(corpus))):
            # Take the first ~80 chars as a "natural" query stand-in.
            text = (row["content"] or "")[:80].strip()
            if not text:
                continue
            sidx, sval = await sparse_encoder.encode_query(text)
            queries.append({
                "text": text,
                "dense": row["dense"],
                "sparse_indices": sidx,
                "sparse_values": sval,
            })
        print(f"  built {len(queries)} queries", flush=True)

        source_id_pool = list({r["source_id"] for r in corpus})

        async def get_main_pool():
            return pool

        # 4. Populate + bench each shape.
        results = {}
        for shape, schema in [("arrays", "bench_arrays"),
                              ("posting", "bench_posting")]:
            print(f"\n=== shape={shape} (schema={schema}) ===", flush=True)
            # Clean any leftover from prior runs.
            await pool.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')

            store = PgvectorStore(
                dsn=None,
                schema=schema,
                dense_dim=args.dense_dim,
                sparse_shape=shape,  # type: ignore[arg-type]
                get_main_pool=get_main_pool,
            )
            await store.ensure_collection()
            await populate_store(store, corpus)

            print("  warming up…", flush=True)
            await run_bench(
                store, queries=queries[:2], iters_per_query=1,
                source_id_pool=source_id_pool,
                prefetch=args.prefetch, limit=args.limit,
            )
            print("  measuring…", flush=True)
            results[shape] = await run_bench(
                store, queries=queries, iters_per_query=args.iters,
                source_id_pool=source_id_pool,
                prefetch=args.prefetch, limit=args.limit,
            )
            if not args.keep:
                await pool.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')

        # 5. Report.
        print("\n" + "=" * 78)
        print("RESULTS")
        print("=" * 78)
        for shape in ("arrays", "posting"):
            print(f"\n[{shape}]")
            for leg, samples in results[shape].items():
                print("  " + _summarize(leg, samples))

        print("\nWinner (by sparse_only p95):")
        a_p95 = _percentile(results["arrays"]["sparse_only"], 95) * 1000
        p_p95 = _percentile(results["posting"]["sparse_only"], 95) * 1000
        if p_p95 < a_p95:
            print(f"  posting  ({p_p95:.1f}ms vs arrays {a_p95:.1f}ms)")
        else:
            print(f"  arrays   ({a_p95:.1f}ms vs posting {p_p95:.1f}ms)")


if __name__ == "__main__":
    asyncio.run(main())
