# Vector store driver abstraction + pgvector path to OSS

**Status**: Approved 2026-05-07 — implementation in progress
**Date**: 2026-05-07
**Owner**: 한병전
**Target**: Pre-OSS release. Replace the hard dependency on Qdrant with a
driver-pluggable `VectorStore` interface. Ship with **pgvector** as the
default OSS driver and **Qdrant** as an optional driver. Same codebase.

---

## 1. Why

Three motivations stack:

1. **OSS friction.** A 4-container stack (PG + Qdrant + backend + frontend)
   plus a 4096-dim embedding model that doesn't fit pgvector's HNSW limits
   is heavy for a "kick the tires" user. The headline becomes "I need
   Qdrant and a 16GB GPU just to try it" instead of "PostgreSQL only".

2. **Vendor neutrality.** Hard-coded `qdrant_client` calls leak through
   `vector_store.py`, `vector_indexer.py`, and the PG schema (`vector_url`
   was already renamed driver-neutral, but the implementation isn't). For
   OSS, operators want choice — Qdrant for prod scale, pgvector for
   simple deploys, and the door open to Weaviate / Milvus / Chroma.

3. **Source-of-truth clarity.** The current chunks table holds
   `embedding vector(N)` *and* `vector_indexed_at` flags. The vector
   column is half SoT, half derived index. Moving the dense vector to
   the driver's own storage cleans up: main PG holds chunk *text*; the
   vector store (whichever driver) is the derived index. Re-indexing
   from text is always possible.

---

## 2. Goals

- **Driver protocol**: `VectorStore` Protocol that QdrantStore and
  PgvectorStore both implement. No qdrant-specific types in the search
  or indexing pipelines.
- **Same-instance pgvector mode**: operator can point the pgvector
  driver at the main PG DSN; the driver creates its own tables under a
  schema/prefix. Deploy stays at 3 containers.
- **Separate-instance mode**: same driver, different DSN. No code change.
- **Embedding swap to 1024-dim** (bge-m3 default). Activates pgvector
  HNSW. Re-indexing is automatic via existing `embed_worker`.
- **Hybrid search semantics preserved**: dense + BM25 sparse, RRF-fused.
  Pgvector driver implements RRF in application code; Qdrant driver
  keeps using native `FusionQuery`.
- **No regression** on the 30 existing `test_hybrid_*.sh` E2E suites.
  Search ranking can shift by ±5% on `eval/run_eval.py` (unavoidable
  with a different RRF impl); we'll widen the threshold.

## 3. Non-goals

- Qdrant deprecation. Qdrant driver stays first-class for operators who
  prefer running an external vector service.
- Schema migration tools beyond a one-shot dual-write or cutover.
- Sparse-only or dense-only standalone modes (already supported as
  fallback inside `hybrid_search`; no API surface change needed).
- Reranker driver abstraction. Cross-encoder rerank is orthogonal.

---

## 4. Architecture

```
┌─ Main PG (Source of Truth) ─────────────────────────┐
│  documents, vaults, chunks (text+metadata only),    │
│  bm25_vocab, bm25_stats, vector_delete_outbox, …    │
│  └─ pgvector extension NOT required                 │
└─────────────────────────────────────────────────────┘
                       │
       embed_worker    │ reads chunks.content
       sparse_encoder  │ (also writes corpus sparse → main PG)
                       ▼
┌─ VectorStore (Protocol) ────────────────────────────┐
│  ensure_collection / health                         │
│  upsert(chunk_id, dense, sparse, payload)           │
│  delete(chunk_ids)                                  │
│  search_hybrid(query_text, query_vec, sparse,       │
│                source_ids, limit) → list[VectorHit] │
└─────────────────────────────────────────────────────┘
       │                            │
       ▼                            ▼
┌─ QdrantStore ──────────┐    ┌─ PgvectorStore ────────────────┐
│  AsyncQdrantClient     │    │  asyncpg pool (separate or     │
│  collection: chunks    │    │  shared with main PG)          │
│  RRF: native FusionQ   │    │  table: vector_index.chunks    │
│  filter: payload index │    │  RRF: app-side reciprocal-rank │
└────────────────────────┘    │  filter: WHERE source_id IN …  │
                              │  HNSW: vector_cosine_ops       │
                              └────────────────────────────────┘
```

### 4.1 What moves where

| Concern | Current location | Future location |
|---|---|---|
| chunk text + metadata | `chunks` (main PG) | unchanged |
| dense embedding | `chunks.embedding` (pgvector type in main PG) | **vector_index.chunks.embedding** in driver-specific store |
| corpus sparse vector | computed at upsert, only in Qdrant | **vector_index.chunks.sparse_terms / .sparse_weights** (or Qdrant sparse slot) |
| query sparse vector | computed at query time in `sparse_encoder` | unchanged |
| BM25 vocab + stats | `bm25_vocab`, `bm25_stats` (main PG) | unchanged — driver-neutral |
| index status (retry, indexed_at) | `chunks.vector_*` (main PG) | unchanged — application-level state |
| delete outbox | `vector_delete_outbox` (main PG) | unchanged |
| RRF fusion | Qdrant native `FusionQuery` | driver-specific: Qdrant native OR app-side |
| `chunks.embedding` column itself | PG vector(N) | **dropped** — main PG no longer needs pgvector ext |

### 4.2 Why corpus sparse moves to PG

Currently the BM25 sparse vector for a corpus chunk is computed inside
`vector_store.upsert_one()` and immediately handed to Qdrant — never
stored in PG. With a driver abstraction:

- We need to *upsert dense + sparse atomically* into whichever store.
- We don't want every driver to re-implement `sparse_encoder.encode_document()`.
- We want re-indexing to be cheap: change driver, re-read PG, reupsert.

Solution: extend `chunks` (or a sibling `chunk_sparse` table) with
`sparse_terms BIGINT[]` + `sparse_weights REAL[]`. `embed_worker` (or
a new `sparse_worker`) populates these alongside dense embeddings. The
driver's `upsert()` simply receives both and writes to its own backend.

This means BM25 corpus encoding **becomes part of the SoT in main PG** —
which is correct: BM25 is a deterministic function of (text, vocab,
stats), and the vocab+stats are already in main PG.

---

## 5. The `VectorStore` Protocol

```python
# backend/app/services/vector_store/base.py

from typing import Protocol, runtime_checkable
from dataclasses import dataclass

@dataclass(frozen=True)
class VectorHit:
    chunk_id: str
    source_type: str          # 'document' | 'table' | 'file'
    source_id: str
    section_path: str
    content: str
    score: float              # RRF score (driver-internal scale, monotonic)

class VectorStoreUnavailable(Exception):
    """Driver-side transient failure. Backfill workers retry."""

@runtime_checkable
class VectorStore(Protocol):
    async def ensure_collection(self) -> None: ...
    async def health(self) -> bool: ...

    async def upsert_one(
        self,
        *,
        chunk_id: str,
        content: str,
        section_path: str | None,
        chunk_index: int,
        dense: list[float],
        sparse_indices: list[int],     # BM25 term ids
        sparse_values: list[float],    # BM25 saturated TF
        source_type: str,
        source_id: str,
    ) -> None: ...

    async def delete_point(self, chunk_id: str) -> None: ...

    async def hybrid_search(
        self,
        *,
        query_text: str,                       # for driver-side debug/logging
        query_dense: list[float] | None,
        query_sparse_indices: list[int],
        query_sparse_values: list[float],
        source_ids: list[str] | None,          # post-filter (vault/collection/etc.)
        limit: int,
        prefetch_per_leg: int,                 # max(limit*3, 50) by default
    ) -> list[VectorHit]: ...
```

**Key contract decisions** (these are the tricky bits):

1. **Sparse encoding happens *outside* the driver.** Caller invokes
   `sparse_encoder.encode_document` / `encode_query` and passes
   `(indices, values)` to the driver. This keeps BM25 logic in one
   place (main PG vocab + Kiwi tokenizer) and makes adding a non-BM25
   driver later straightforward (e.g. an SPLADE-based driver could
   ignore these and re-encode internally).

2. **Driver returns RRF score in its own scale**, but `VectorHit.score`
   has documented contract: monotonic (higher = better), unbounded.
   Callers (search_service rerank, eval) must not compare scores
   across drivers. Test assertions use *titles* and *ordering*, not
   absolute scores — already true in the existing tests.

3. **`source_ids` is post-filter** — the caller pre-resolves which
   document/table/file ids match metadata filters (vault, collection,
   doc_type, tags, ACL) and hands the union to the driver. Drivers
   translate to whatever filter their backend supports (Qdrant payload
   index `MatchAny`, pgvector `WHERE source_id = ANY($n)`).

4. **`prefetch_per_leg` is explicit** — currently hard-coded to
   `max(limit*3, 50)` inside `hybrid_search`. Lifting it out lets
   drivers tune (Qdrant native fusion is cheap; pgvector app-side
   fusion benefits from generous prefetch).

5. **`upsert_one` not `upsert_many`** — current path is one chunk at a
   time. We'll keep that for parity, add `upsert_many` later if
   benchmarks warrant.

---

## 6. Driver implementations

### 6.1 `QdrantStore` (extracted from current code, behavior-preserving)

Drop `vector_store.py` (current) into `vector_store/qdrant.py`, change
the methods to match the Protocol signatures (the only real diff: it
now *receives* `sparse_indices`/`sparse_values` instead of computing
them itself — `upsert_one` loses the `sparse_encoder.encode_document`
call). Native `FusionQuery(RRF)` stays.

### 6.2 `PgvectorStore` (new)

**Schema** (driver-managed; created on first `ensure_collection()`):

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA IF NOT EXISTS vector_index;

CREATE TABLE IF NOT EXISTS vector_index.chunks (
    chunk_id        UUID PRIMARY KEY,
    source_type     TEXT NOT NULL,
    source_id       UUID NOT NULL,
    section_path    TEXT,
    content         TEXT NOT NULL,
    chunk_index     INTEGER NOT NULL,
    dense           vector(1024) NOT NULL,
    sparse_terms    BIGINT[] NOT NULL,    -- term_id list
    sparse_weights  REAL[]   NOT NULL,    -- saturated TF list
    indexed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_vi_chunks_source_id
    ON vector_index.chunks (source_id);

-- HNSW now activates because dim ≤ 2000.
CREATE INDEX idx_vi_chunks_dense
    ON vector_index.chunks
    USING hnsw (dense vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
```

**Schema name `vector_index`** keeps it visible-but-separate when
sharing the main PG instance, and keeps `chunks` (main) vs
`vector_index.chunks` (derived) unambiguous.

**Dense search**:
```sql
SELECT chunk_id, source_type, source_id, section_path, content, chunk_index,
       1 - (dense <=> $1) AS sim
FROM vector_index.chunks
WHERE source_id = ANY($2)
ORDER BY dense <=> $1
LIMIT $3;
```

**Sparse (BM25) search** — without paradedb, we do this in app SQL:
```sql
-- $1 = query term_ids, $2 = query weights (IDF), $3 = source_ids, $4 = limit
WITH q AS (SELECT unnest($1::bigint[]) AS tid, unnest($2::real[]) AS w),
     hits AS (
       SELECT c.chunk_id,
              SUM(q.w * c.sparse_weights[i]) AS score
       FROM vector_index.chunks c
       CROSS JOIN LATERAL unnest(c.sparse_terms) WITH ORDINALITY AS t(tid, i)
       JOIN q ON q.tid = t.tid
       WHERE c.source_id = ANY($3)
       GROUP BY c.chunk_id
     )
SELECT chunk_id, score FROM hits ORDER BY score DESC LIMIT $4;
```

This is the costliest piece — BM25 over arrays without an inverted
index. Two mitigations evaluated, choose at impl time:
- **Option A**: keep arrays, lean on `idx_vi_chunks_source_id`
  pre-filter. Acceptable for <500k chunks per query scope.
- **Option B**: a side `vector_index.posting (term_id, chunk_id, weight)`
  GIN-indexed table. Faster but doubles write cost. Defer until
  benchmarks force it.

Start with A. If `eval/run_eval.py` p95 latency > 500ms on prod data
(567k chunks total but vault-scoped queries hit far fewer), switch to B.

**RRF fusion in app**:
```python
def rrf(results_per_leg: list[list[str]], k: int = 60) -> dict[str, float]:
    scores: dict[str, float] = {}
    for leg in results_per_leg:
        for rank, chunk_id in enumerate(leg, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return scores
```
Then re-fetch payloads for the top `limit` chunk_ids in one round-trip.

**Connection management**: takes a `dsn: str | None`. If None, reuses
the main PG pool (same-instance mode). If set, opens a separate pool.

### 6.3 Driver selection

`config/app.yaml`:
```yaml
vector_store_driver: pgvector   # or "qdrant"
vector_store_dsn: ""             # pgvector only; blank = main PG
vector_url: ""                   # qdrant only
vector_api_key: ""               # qdrant only
vector_collection: chunks
```

Factory:
```python
def get_vector_store() -> VectorStore:
    if settings.vector_store_driver == "qdrant":
        return QdrantStore(url=settings.vector_url, ...)
    elif settings.vector_store_driver == "pgvector":
        return PgvectorStore(dsn=settings.vector_store_dsn or None, ...)
    raise ValueError(...)
```

---

## 7. Embedding model — fully pluggable, no default lock-in

**Stance**: the embedding model is config, not code. AKB ships with
sensible *defaults* but has no opinion on which open-weight model to
run. Operators (and OSS users) swap by editing `app.yaml`:

```yaml
embed_base_url: <any OpenAI-compatible /v1 endpoint>
embed_model:    <provider's model name>
embed_dimensions: <must match the model>
```

**Pre-OSS sample defaults** (in `config/app.yaml.example`): point at
OpenAI's `text-embedding-3-small` (1536-dim). The Korean / multilingual
note in README mentions a few proven open-weight options
(bge-m3 1024-dim, multilingual-e5-large 1024-dim, mxbai-embed-large
1024-dim) without prescribing one.

**Why this matters for the migration**: the internal cluster currently
runs Qwen3-embed-8b (4096-dim). For the cutover we must pick *some*
≤2000-dim model so pgvector HNSW activates. Phase 6 benchmarks the
candidate models against `eval/run_eval.py` on a Korean-heavy sample
and locks in the post-cutover model. **Code does not encode any model
choice** — just `embed_dimensions` flows through to the
`vector_index.chunks.dense vector(N)` column type.

**Migration sequence**:
1. Drop `chunks.embedding` column from main PG (the column moves to
   `vector_index.chunks.dense` under driver ownership).
2. `embed_worker` re-reads `chunks` where `vector_indexed_at IS NULL`
   (we force-NULL all rows during migration), generates embeddings via
   whatever `embed_base_url` / `embed_model` is configured, calls
   `vector_store.upsert_one()`.
3. `sparse_worker` (new, or fold into embed_worker) recomputes
   `sparse_terms` / `sparse_weights` and includes them in the upsert.
4. Internal cluster: re-embed 567k chunks. Wall clock depends on the
   chosen model + endpoint throughput; estimate ~60-120 minutes. Run
   off-hours. Confirmed acceptable.

**Dimension change is explicit**: `embed_dimensions` controls the
pgvector column type. Changing it forces a re-create of
`vector_index.chunks` — gated by a `_verify_dimension` check that
errors loud rather than corrupting data silently. Same model? Same
dim? Hot reload. Different dim? Recreate + re-index.

---

## 8. Migration strategy (internal cluster)

**Cut-over, not dual-write.** Dual-write doubles complexity for a
one-time migration; cutover with a short re-index window is simpler.

Sequence:

1. **Freeze**: deploy a build with the new `VectorStore` abstraction
   but `vector_store_driver: qdrant` still. Existing Qdrant cluster
   continues serving. (Behavior-preserving deploy — green light to
   land Phase 2.)
2. **Schema land**: deploy the migration that creates
   `vector_index.chunks` and drops `chunks.embedding`. Ingest still
   writes to chunks (text only); search still reads from Qdrant.
   Workers stay paused for a window.
3. **Re-index**: flip `vector_store_driver: pgvector`. embed_worker
   sees `vector_indexed_at IS NULL` for everything, fans out, populates
   `vector_index.chunks`. Search stays degraded (returns empty for
   pgvector-not-yet-populated) for ~95 minutes. We accept this for the
   one-time cutover and pre-announce it.
4. **Verify**: `/health` shows `pending: 0`, `eval/run_eval.py`
   recall ≥ baseline − 5%. Drop the Qdrant StatefulSet from the cluster.

**Rollback**: keep Qdrant data for 7 days. If anything regresses,
flip the driver back and re-attach.

---

## 9. OSS UX

`docker-compose.yaml` (default — pgvector same-instance):

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg16   # already used; extension built-in
    # ...
  backend:
    environment:
      AKB_VECTOR_STORE_DRIVER: pgvector
      # vector_store_dsn blank → reuses main PG pool
  frontend:
```

Result: **3 containers**, single DB to back up, single thing to scale.

Operators wanting Qdrant override via `docker-compose.qdrant.yaml`:
```yaml
services:
  qdrant:
    image: qdrant/qdrant:v1.12.4
  backend:
    environment:
      AKB_VECTOR_STORE_DRIVER: qdrant
      AKB_VECTOR_URL: http://qdrant:6333
```

`docker compose -f docker-compose.yaml -f docker-compose.qdrant.yaml up -d`

---

## 10. Test strategy

- **Existing 30 `test_hybrid_*.sh`**: must pass on both drivers. Test
  harness param: `AKB_VECTOR_STORE_DRIVER=pgvector|qdrant`. CI matrix
  not strictly needed (no CI right now), but we run both locally.
- **`eval/run_eval.py`**: Recall@K and MRR baselines captured before
  migration on Qdrant. After cutover, allow ±5% drift on Recall and
  ±0.05 on MRR. If drift exceeds, investigate before declaring done.
- **`test_hybrid_invariants_e2e.sh`**: drives the assertion that
  ranking is *consistent* across calls (idempotent). Not affected by
  driver swap.
- **New driver-specific tests**: a small `test_vector_store_drivers_e2e.sh`
  that exercises upsert/search/delete/filter against whichever driver is
  configured.

---

## 11. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Pgvector BM25 array scan too slow | Medium | High (search latency regress) | Bench during Phase 6; switch to posting-table (Option B) if p95 > 500ms |
| App-side RRF ranks differ enough to break user expectations | Medium | Medium | Use canonical k=60 (matches Qdrant default); accept up to ±5% Recall drift |
| Re-index window during cutover too long | Low | Medium | Off-hours window, pre-announced; rollback plan keeps Qdrant for 7 days |
| Operator schema collision (`vector_index.chunks` clashes with their custom schema) | Low | Low | Schema name configurable via `vector_store_schema` setting; default `vector_index` |
| HNSW build cost on 567k re-indexed rows | Low | Low | HNSW is incremental; build time included in re-index window |
| Embedding model swap breaks ranking on Korean queries | Medium | Medium | Run `eval/run_eval.py` against Korean test set before cutover; if bge-m3 underperforms, fall back to multilingual-mxbai or keep Qwen3-embed-8b with halfvec(4000) |

---

## 12. Phasing (mapped to the task list)

- **Phase 2** (1-2d): `VectorStore` Protocol in `backend/app/services/vector_store/base.py`. Move existing `vector_store.py` → `vector_store/qdrant.py`, conform to Protocol. `sparse_encoder.encode_document` call moves out of QdrantStore into `vector_indexer` (caller side). Tests still green on Qdrant.
- **Phase 3** (3-4d): `vector_store/pgvector.py`. Includes
  `vector_index.chunks` migration script, dense + sparse SQL, app-side
  RRF. **Sparse storage shape (arrays vs posting table) is benchmark-
  gated within this phase** — synthetic + sampled-prod data, p50/p95
  latency at 100k / 500k corpus sizes, choose the winner before
  declaring Phase 3 done. No "ship A, maybe upgrade later" — pick
  once, ship once.
- **Phase 4** (1-2d): main PG migration to drop `chunks.embedding`, add `chunks.sparse_terms` / `chunks.sparse_weights` (or sibling table), update `embed_worker` to populate both and call `vector_store.upsert_one`. Embedding model swap to bge-m3 1024-dim.
- **Phase 5** (1d): docker-compose / k8s / config defaults.
- **Phase 6** (3-5d): test regression matrix, `eval/run_eval.py` baselines, p95 latency bench (Qdrant vs pgvector at 567k chunks).
- **Phase 7** (1-2d): internal cluster cutover, verify, decommission Qdrant.
- **Phase 8** (1d): README / CLAUDE.md / deploy/k8s/README.md updates.

**Total**: ~10-15 working days. Two-and-a-half to three weeks calendar.

---

## 13. Resolved decisions (2026-05-07)

1. **Embedding model is fully pluggable** — no default lock-in. Code
   reads dim from config; OSS docs list a few proven options as
   examples. Phase 6 benchmark picks the post-cutover model for the
   internal cluster.
2. **Re-index downtime ~60-120 min** — confirmed acceptable, off-hours.
3. **Sparse storage shape** — benchmark within Phase 3 (arrays vs
   posting table). Pick once based on data, ship that. No "ship A,
   upgrade B later" path.
4. **paradedb**: skipped for v1 (extension installation friction for
   OSS). Revisit if pgvector BM25 latency proves insufficient post-launch.
5. **`chunks.embedding` deprecation**: N/A pre-OSS. Destructive
   one-shot migration documented in Phase 4.
