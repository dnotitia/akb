# Search performance: posting retrieval and stage diagnostics

## Scope and evidence

This change improves PostgreSQL/pgvector retrieval while preserving the default
BM25 weights, dense embeddings, RRF, reranking, candidate limits, and application
authorization. An optional large-corpus profile can omit very common BM25 query
terms when a dense leg is available; that explicit latency/relevance tradeoff is
disabled by default. It does not replace the search engine or deploy a new model.

A read-only investigation of an older deployed backend found:

- The active driver was **pgvector, posting shape**, not Qdrant. Query embedding
  and reranking used external services; reranking was already enabled.
- Catalog estimates were approximately 998,000 vector chunks and **71.5 million
  posting rows**. Posting heap storage was about 4.5 GB, total storage about 11 GB.
- Two frequently executed sparse-query fingerprints had historical mean
  execution times of **6,276 ms / 23,879 calls** and **4,825 ms / 6,450 calls**;
  maxima were close to 30 seconds. Dense fingerprints also had substantial
  latency, including a 1,359 ms mean over 27,351 calls.
- These are cumulative `pg_stat_statements` observations since July 8, 2026,
  not a controlled current-request sample or an end-to-end latency percentile.
  Query variants, parameters, concurrency and cache states differ.
- Existing posting indexes covered `term_id` and `chunk_id`, but not `weight`.
  A representative **EXPLAIN without ANALYZE** showed a posting index scan,
  a scoped chunk lookup, a join, and score aggregation. Scoring requires heap
  access for the missing weight. This is a supported bottleneck hypothesis,
  not a measurement of that individual production execution plan.

Only settings allowlisted for inspection, catalog metadata, existing query
statistics, and non-executing plans were inspected. No production benchmark,
index creation, configuration change, reindexing, migration or deployment was
performed. Production content and credentials were not copied into fixtures.

## Where time goes

```text
search request
  -> query embedding (external API)
  -> authorized candidate selection (including metadata/archive constraints)
  -> query sparse encoding
  -> pgvector retrieval
       + dense nearest-neighbor lookup --+
       + sparse posting aggregation ----+ run concurrently
       -> rank fusion and payload lookup
  -> source deduplication
  -> optional external reranking
  -> metadata hydration
  -> response
```

`search_timing` logs total, embedding, candidates, retrieval, rerank and hydration
milliseconds plus returned count on successful and empty-result paths.
`retrieval_ms` includes sparse encoding and the driver call. Total includes
overhead outside the listed phases; these are diagnostics, not tracing spans.

`hybrid_timing` logs dense/sparse leg durations, each leg's connection-pool wait,
payload duration, filter kind/count and term count. Legs overlap, and their
durations include their pool waits: **do not add them to estimate request time**.
Failed calls may have incomplete leg measurements. Early empty driver inputs
do not run retrieval or produce a driver timing entry. No raw queries, UUIDs,
result content or credentials are included in these new timing messages.

## Changes

### Cover score and scope lookups

For **fresh** stores:

- Posting primary key: `(term_id, chunk_id) INCLUDE (weight)`.
- Chunk vault index: `(vault_id) INCLUDE (chunk_id)`.
- Chunk source index: `(source_id) INCLUDE (chunk_id)`.

The scoring expression and filtering SQL are unchanged. A covering index makes
an index-only scan possible, avoiding extra heap reads where the visibility map
permits it. Recently modified pages can still require heap visits. Vacuum
health, selectivity and the planner remain important. See PostgreSQL's
[index-only scan documentation](https://www.postgresql.org/docs/current/indexes-index-only-scans.html).

Existing tables/indexes are **not rebuilt by application startup**.
`CREATE ... IF NOT EXISTS` preserves their definitions. Updating backend code
alone therefore does not retrofit the larger indexes into an old installation.
This deliberately avoids an unplanned large-table operation during rollout.

### Explicit maintenance for existing posting stores

The optional script is
`backend/scripts/sql/pgvector_search_covering_indexes.sql`:

```bash
psql "$VECTOR_DSN" -v schema=vector_index \
  -f backend/scripts/sql/pgvector_search_covering_indexes.sql
```

Use the **vector-store database**, which may differ from the main database.
This is not an automatic deployment step and is not for arrays-shaped stores.
Do not run it inside a transaction or with `--single-transaction`.

It creates three additional covering indexes concurrently and reports validity
and readiness. It preserves existing indexes and primary keys. Check free disk,
temporary build space, WAL/replica capacity and write load before scheduling it.
Concurrent builds allow writes but still consume resources and may wait for
transactions. A cancelled build can leave an invalid index; `IF NOT EXISTS`
does not repair it. Inspect the final validity output and resolve invalid
indexes explicitly before retrying. Review redundant indexes separately.

Test first on a representative isolated copy, with realistic write churn and
concurrency. Compare plans, buffer accesses, p50/p95/p99, indexing throughput,
disk and end-to-end latency. The local read-mostly result below is not enough
to approve a production maintenance window.

### Empty scopes are not unscoped

The pgvector driver now treats `source_ids=[]` or `vault_ids=[]` as no matches.
`None` continues to mean no restriction. This prevents an unnecessary unscoped
scan if a caller passes an empty authorized set. It supplements, rather than
replaces, the service's existing authorization checks.

## Local benchmark

The reproducible script is `backend/scripts/benchmark_search_posting.py`.
It is intentionally restricted to PostgreSQL on `127.0.0.1:15434`, creates a
random database and drops only that database on exit. It makes no model calls.

```bash
# Use a free port/name. This is a disposable, loopback-only test database.
docker run --rm -d --name akb-search-perf-pg --shm-size=512m \
  -p 127.0.0.1:15434:5432 -e POSTGRES_HOST_AUTH_METHOD=trust \
  pgvector/pgvector:pg16
cd backend
uv sync --locked --extra dev
uv run python scripts/benchmark_search_posting.py --rows 100000 --repeats 5
AKB_SEARCH_PERF_DSN=postgresql://postgres@127.0.0.1:15434/postgres \
  uv run pytest tests/test_search_covering_indexes_e2e.py -q
# Stop only the disposable container created above after finishing.
docker stop akb-search-perf-pg
```

Measured on local Docker PostgreSQL 16, September 7, 2026:
100,000 synthetic chunks, 6.4 million posting rows, 100 vaults. Three common
terms appear in every chunk; mixed and rare queries exercise different
selectivity. The same database and exact SQL run before and after indexes are
added. Each case has one warmup and five measured repetitions. Both forced
custom and generic prepared-plan modes are tested. VACUUM/ANALYZE runs before
measurement, with no concurrent writer: this favors index-only access.

Selected median database-query times (milliseconds):

| Plan / terms | Vault scope | Before | Covering | Interpretation |
| --- | ---: | ---: | ---: | --- |
| Custom / common | 1% | 257.97 | 18.93 | 13.6x faster |
| Custom / common | 10% | 265.19 | 38.96 | 6.8x faster |
| Custom / common | 100% | 306.70 | 288.18 | Little improvement |
| Generic / common | 100% | 329.66 | 91.99 | 3.6x faster |
| Custom / mixed | 1% | 90.18 | 6.43 | 14.0x faster |
| Generic / rare | 100% | 2.40 | 4.95 | Regression in this case |

All **18 before/after cases** preserved ordered chunk IDs and exact scores.
The fixture uses exactly representable weights and a chunk-ID tie breaker to
make equality meaningful. This verifies unchanged score semantics on synthetic
data, not semantic relevance or production tie ordering.

Posting total size grew from **853,729,280 to 1,171,603,456 bytes** (about 37%)
when retaining old indexes and adding the covering posting index; the separate
chunk index is additional. This is a real storage/write tradeoff, not free
acceleration. No reliable concurrent-write or cold-cache result is claimed.

The benchmark isolates sparse SQL; it is **not full HTTP search latency** and
does not model the production corpus's distribution. Native document-head
verification, long source-ID lists and external embedding/reranking require
separate measurements.

## Functional verification

`test_search_covering_indexes_e2e.py` uses real PostgreSQL and pgvector with
32-dimensional synthetic vectors to cover:

- New covering definitions and preservation of old indexes on startup.
- Dense+sparse retrieval, dense-only and sparse-only fallback.
- Vault/source scoping and explicitly empty authorized sets.
- Timing output without fixture query text, source IDs or result content.

The maintenance SQL was also executed twice against a disposable local database:
all three indexes were valid/ready, and the second execution was idempotent.
Search filter, source URI, parent-context, native selection, reranking and
multi-vault regression suites remain part of the verification gate.

Validation on this branch: **66 search regression tests, four real-pgvector
tests, and one live HTTP scenario passed**. The HTTP scenario ran against a
separate local repository-owned runtime with deterministic embedding responses,
not the existing development stack. It covered registration/login, document
creation and asynchronous indexing, tags/types/archive/collection boundaries,
result-limit ordering, unauthorized access, and file/table search. Ruff, mypy
for the two changed service modules, and whitespace checks passed. No real
external embedding/reranker latency or browser SSO behavior was tested here.

PR review also added DB-free coverage for empty scopes and stage timing units,
moved the empty-scope return ahead of database initialization, and delayed the
successful driver timing flag until payload conversion succeeds. The new
real-pgvector suite runs in the existing CI database job rather than silently
skipping in both jobs. These adjustments do not change ranking or measured SQL.

## Alternatives and next decisions

- Do not add a reranker to address a sparse database bottleneck: this deployment
  already uses one, and reranking adds another network/model stage.
- Do not lower candidate counts blindly: that changes recall, especially after
  source deduplication and filtering.
- Do not replace source filters with vault-only filtering when archive/type
  constraints require source selection. That would undo correctness fixes.
- A materialized aggregate-before-scope experiment was slower locally; a
  redundant term predicate did not show a consistent gain. Neither is shipped.
- Dense HNSW filtering can still dominate for some workloads. Measure its
  iterative scans and selectivity before tuning recall/latency parameters;
  see [pgvector filtering guidance](https://github.com/pgvector/pgvector#filtering).
- Collect current per-stage timings before adding caches, changing engines or
  provisioning models. An engine replacement needs a relevance dataset and
  operational cost comparison, not just this SQL benchmark.

**Status:** a bounded first improvement, not a claim that every slow production
search is resolved. Production remains unchanged. Existing installations need
an explicitly approved index-maintenance step to receive the index benefit.

## Production-scale cache follow-up

A further read-only settings check found production `shared_buffers=2 GiB`,
`effective_cache_size=6 GiB`, `work_mem=32 MiB`, `random_page_cost=1.1`, and
`track_io_timing=off`. The HNSW index alone occupied **8,384,167,936 bytes**
(7.81 GiB); the posting primary key occupied 4,882,440,192 bytes. This makes
cache pressure plausible, but does not prove cache misses are the sole cause.
An HNSW query need not read the entire index. `effective_cache_size` is a planner
estimate, not allocated cache memory.

The earlier dominant sparse fingerprint accumulated 2,143,933,393 shared hits
and 1,405,117,752 shared reads (about 39.6% PG-buffer misses among these accesses).
This is further historical evidence of buffer-read pressure, not a cold-start
experiment, a current miss rate, or proof of physical-disk latency.

PostgreSQL's buffer cache and the operating system page cache are distinct.
`EXPLAIN ... BUFFERS` shared reads mean a miss in PG shared buffers, **not
necessarily physical disk I/O**. Read timing includes reads served by the OS.
See the [PostgreSQL 16 statistics documentation](https://www.postgresql.org/docs/16/monitoring-stats.html).
Restarting PostgreSQL resets its buffers but may preserve the OS cache;
`DISCARD ALL` is not a data-cache reset. No host/VM-wide cache purge is used,
because other local applications share that environment.

Do not blindly prewarm the entire HNSW index into a smaller buffer cache.
Prewarming can evict its own earlier pages and other useful data; prewarmed
pages are not pinned. A startup-only cold penalty and sustained cache churn
need different mitigations. See the [official pg_prewarm documentation](https://www.postgresql.org/docs/16/pgprewarm.html).

`backend/scripts/benchmark_search_scale.py` provides a separate scale experiment:

- 997,935 synthetic 1,024-dimensional vectors and exactly 71,536,566 postings.
- Seeded clustered random vectors, 100 uniform vaults, four very common terms
  and a vocabulary of up to 683,360 terms. No copied production content.
- Production-like HNSW parameters (`m=16`, `ef_construction=64`) and retrieval
  settings (270 candidates, filtered iterative scan, `ef_search=200`).
- Dedicated loopback-only Docker PostgreSQL, 8 GiB measurement memory / four CPU limit,
  2 GiB shared buffers. Build-only container memory is 10 GiB and HNSW maintenance
  memory is 5 GiB (one builder) to avoid graph spill and shorten setup;
  it does not reproduce production index-build resource behavior.
- Per-case PG restart, immediate first query, repeated queries, then different
  dense/sparse queries competing for cache. Captures plans, shared reads/hits,
  I/O read time, temporary blocks and returned rows. First queries are labeled
  **PG-cold / OS-unknown**, never fully cold-disk benchmarks.
- Fixed `force_custom_plan` policy during cache comparisons, to avoid mistaking
  an automatic switch to a generic prepared plan for a cache effect.
- Separate baseline and covering-index measurements. The script leaves its
  dedicated database available between stages; stop its disposable container
  after all measurements to remove that test dataset.

This reproduces indexed row count and dimensionality, not production's semantic
distribution, hardware, storage latency, write traffic, metadata tables or
whole-application workload. Data size and index sizes must be reported alongside
latency. Do not infer model quality or production p95 from a few synthetic runs.

```bash
# Reserve substantial free space for tables, indexes, WAL and index build work.
# Never reuse a container that contains user data under this test name.
docker run --rm -d --name akb-search-scale-pg \
  --memory=10g --memory-swap=10g --cpus=4 --shm-size=3g \
  -p 127.0.0.1:15434:5432 -e POSTGRES_HOST_AUTH_METHOD=trust \
  pgvector/pgvector:pg16 \
  -c shared_buffers=2GB -c work_mem=32MB -c maintenance_work_mem=2GB \
  -c effective_cache_size=6GB -c random_page_cost=1.1 -c track_io_timing=on \
  -c max_wal_size=2GB -c max_parallel_maintenance_workers=2
cd backend
uv run python scripts/benchmark_search_scale.py build
docker update --memory=8g --memory-swap=8g akb-search-scale-pg
uv run python scripts/benchmark_search_scale.py measure --label baseline
uv run python scripts/benchmark_search_scale.py cover
uv run python scripts/benchmark_search_scale.py measure --label covering
docker stop akb-search-scale-pg
```

### Scale experiment results

The full-scale run completed. Exact local counts were 997,935 vectors and
71,536,566 postings. Local pgvector was 0.8.2, matching the inspected deployment.
The local HNSW index was 8,175,091,712 bytes versus 8,384,167,936 bytes in the
deployment. The freshly bulk-built posting primary key was smaller:
2,905,145,344 versus 4,882,440,192 bytes. Equal row counts do **not** reproduce
long-term physical fragmentation, insertion history, data skew or storage.
The fixture also uses sequential synthetic UUIDs and one source per chunk.

Raw settings, index sizes and **68 measurement records** are retained in
[`search-cache-scale-2026-09-07.jsonl`](../benchmarks/search-cache-scale-2026-09-07.jsonl).
Every measured leg returned 270 rows. This is not a relevance/recall comparison
or exact full-scale rank-parity test. The earlier small-corpus parity test is
separate. Times below are instrumented SQL execution times (`TIMING OFF` avoids
per-node timer overhead), except parallel-leg wall times; warm values are the
median of three repetitions, not production percentiles.

| Measurement | Baseline | Covering | Scope |
| --- | ---: | ---: | --- |
| Warm BM25 | 5,079 ms | 100 ms | 1% of vaults |
| Warm BM25 | 5,554 ms | 743 ms | 10% of vaults |
| Warm BM25 | 5,888 ms | 1,142 ms | 100% of vaults |
| Warm parallel dense + BM25 legs | 5,323 ms | 760 ms | 10% of vaults |
| HNSW after mixed workload | 1,481 ms | 23 ms | 10% of vaults |
| First HNSW after PG restart | 2,218 ms | 1,983 ms | 10% of vaults |

Two distinct issues were reproduced:

1. **Startup cache misses remain.** Baseline 10%-scope HNSW took 2,218 ms on
   the first query, including 2,181 ms of read time and 16,742 shared-read
   blocks. Identical repetitions took a median 16 ms with zero shared reads.
   The covering phase still took 1,983 ms initially, then 15 ms warm. At 1%
   scope the planner chose scoped bitmap lookup plus exact vector sorting,
   not HNSW; its first query was also slow (2,433 / 3,150 ms respectively).
   This is broader PG data/index loading behavior, not proof of a pgvector bug.
   Do not attribute between-phase cold-time differences to the covering index:
   OS cache state is uncontrolled.
2. **Sustained reads and mixed-workload eviction improved.** Baseline BM25
   repetitions still incurred about 673,297 shared-read blocks, approximately
   5.14 GiB of block-read events per query, and remained around 5–6 seconds.
   These are not unique bytes or necessarily physical-device reads. Covering
   repetitions incurred zero shared reads in these cases. Baseline 10%-scope
   HNSW went from 16 ms warm back to 1,481 ms after four different dense/sparse
   query pairs (15,018 shared reads); covering kept it at 23 ms and zero reads.
   This supports reducing the recurring working set, not just startup warming.

The plans explain the difference: the baseline read posting weights through
`idx_posting_term` with heap visits and repeated chunk-primary-key lookups.
The covering plan used `idx_posting_covering` with **zero heap fetches** for
postings. At 1% scope the scoped covering lookup also helped. At wider scopes,
repeated chunk lookups and aggregation still consumed CPU; the 100% case still
wrote/read temporary blocks for aggregation. Index-only access is not a cure
for all planner, aggregation or wide-scope costs.

The two additional indexes occupied **3,602,268,160 bytes (3.35 GiB)** and old
indexes were retained. The application/ranking logic and measurement memory
budget were unchanged. Only read-mostly data was tested: updates and visibility
map churn can reduce index-only benefits and need separate verification.

### Consequence for the next improvement

Keep the covering-index improvement as the first bounded change: it reduced
both repeated BM25 cost and the observed cache competition. Treat startup
warming as a separate follow-up, for example evaluating bounded hot-block
restoration through `pg_prewarm`/autoprewarm in this isolated environment.
Do not preload an entire 7.8 GiB index into a 2 GiB cache or raise all memory
limits indiscriminately. At that experiment stage no warming configuration was
enabled; the opt-in target profile below is the separately validated follow-up.
A true host/VM cold-disk experiment, concurrent writes, production
query diversity and whole-request embedding/reranker latency remain untested.

No production deployment, restart, index change or setting change was made.

## Million-vector target profile

The follow-up implementation adds an opt-in Helm overlay for a pgvector corpus
around one million 1,024-dimensional embeddings:
`deploy/helm/akb/tuning/pgvector-million-1024d.yaml`. It composes with both
`standalone` and `standalone-sso`; it is not a third application profile.

```bash
helm upgrade --install akb deploy/helm/akb \
  --namespace akb \
  --values deploy/helm/akb/profiles/standalone.yaml \
  --values deploy/helm/akb/tuning/pgvector-million-1024d.yaml
```

The overlay is a measured reference, not automatic capacity detection. It sets
`shared_buffers=16GB`, enables PostgreSQL autoprewarm, requests 16 GiB and limits
the database container to 36 GiB. The application loads the HNSW index plus the
vector heap/TOAST search working set before becoming ready. It refuses startup
when the selected relations exceed 90% of `shared_buffers`, so an undersized
deployment cannot report a misleading healthy warmup. `/health` publishes only
safe status, byte, block, source and duration metadata.

The database records autoprewarm state so PostgreSQL can restore the buffer set
after restart. The application also stores one completion record keyed by the
PostgreSQL postmaster start time, selected mode and relation signature. This
prevents duplicate loads across API replicas while ensuring a new database
process or changed relation is warmed again. Worker-only processes skip the
application prewarm.

Two query-path changes complement cache restoration:

- Source-filtered posting search materializes the already authorized chunk IDs
  before joining a common posting list. This changes join order, not BM25 scores;
  the regression fixture preserved ordered IDs and scores in all tested cases.
- `bm25_hybrid_max_df_ratio` optionally omits terms above a document-frequency
  ratio only when dense retrieval succeeded. The default `0` keeps exact BM25.
  The scale overlay uses `0.8`. A sparse-only deployment or embedding outage
  retains every term, and a failed cutoff-stat lookup falls back to exact BM25.

The vault-filter activation gate was also corrected for split deployments. The
background worker and API are separate processes, so the worker's memory-only
“backfill complete” flag could never activate the API. While gated, each API
process now checks the durable store at most once per 15 seconds and latches the
result locally. It fails closed to the correct source-ID path on any check error.

### Isolated Kubernetes result

The target was validated in namespace `akb-search-perf-20260908`; no production
resource was modified. The fixture contained 997,935 vectors, 71,536,566 posting
rows and 100 vaults. It exercised local login, 10% and 90% vault access, archive,
type, tag and explicit-vault filters, five concurrent requests, and cross-vault
result assertions.

After a PostgreSQL and backend restart, application prewarm loaded
14,509,932,544 bytes (1,771,232 blocks) in 10.20 seconds before readiness.
PostgreSQL autoprewarm restored 1,907,704 previously loaded blocks. The database
cgroup used about 32.4 GB of a 36 GiB limit and reported zero OOM events. A 24 GiB
limit tested earlier reached its hard ceiling and was rejected; `kubectl top`
alone understated the page-cache pressure.

Steady HTTP measurements (105 requests) were:

| Scenario | p95 | Maximum |
| --- | ---: | ---: |
| All tested requests | 985 ms | 1,009 ms |
| 90% access, default archive filter | 1,009 ms | 1,009 ms |
| 90% access, type filter | 616 ms | 616 ms |
| 90% access, tag filter | 467 ms | 467 ms |
| 10% access, default archive filter | 327 ms | 327 ms |
| Five concurrent requests | 763 ms | 763 ms |

The first request after process restart took 2.66 seconds because Kiwi initialized
its tokenizer process on first use; subsequent identical requests were about
0.19 seconds. This satisfies the chosen exceptional restart ceiling of 5 seconds,
but it is not counted as steady-state p95. A synthetic rare term below the 0.8
cutoff exercised both dense and BM25 legs in 360 ms, confirming that the profile
does not turn all searches into dense-only retrieval.

These numbers are specific to the synthetic distribution and test storage. Before
using the overlay elsewhere, measure relation size, cgroup memory including page
cache, real query document-frequency distribution, relevance, write load and the
actual embedding/reranker network stages. The common-term cutoff is intentionally
opt-in because some corpora may value exact lexical contribution over this
latency bound.

## Fixed-memory million-vector profile

The full-working-set profile above has a hard capacity boundary: its selected
14.51 GB relation set already consumes most of the 16 GB shared-buffer safety
budget and the PostgreSQL cgroup used about 32.4 GB. Keeping that strategy while
the corpus grows means repeatedly increasing memory. It is therefore a
latency-first option, not the default growth model.

`deploy/helm/akb/tuning/pgvector-million-bounded-1024d.yaml` provides the
complementary memory-first model:

```text
disk-backed corpus and HNSW (larger than RAM)
             |
             v
PostgreSQL 3 GiB shared-buffer hot set  -- recorded/restored by autoprewarm
             |
             v
8 GiB database cgroup ceiling          -- leaves room for OS cache and queries
```

It does not call the application full-relation prewarmer, so adding rows does not
eventually make readiness fail merely because every vector cannot fit in RAM.
PostgreSQL autoprewarm records at most the pages represented in the bounded
shared buffers; those pages are still evictable. This favors recurring queries
after restart while accepting that a new graph path may read from storage.

A clean local PostgreSQL 16 restart check confirmed that this database feature
does not depend on the AKB application prewarmer. With `shared_buffers=64MB`,
the `pg_prewarm` library in `shared_preload_libraries`, and no `pg_prewarm` SQL
extension installed, the background worker recorded the live buffer inventory.
After a clean container restart it restored 8,191 of 8,192 recorded blocks;
`pg_buffercache` observed 7,308 table and 494 index blocks from the exercised
relation. The disposable container and volume were removed after verification.

The first isolated API request after switching the existing million-vector
fixture to this memory limit exposed a separate correctness concern: the dense
leg spent about 33.1 seconds on a cold source-filtered HNSW path, crossed the
hard-coded 30-second main-pool limit, and the API returned HTTP 200 with
`degraded=true` and no results. The bounded overlay therefore configures a
90-second **pgvector retrieval-only** budget. A first restored query completed in
0.42 seconds, but the immediately following unseen path still crossed a
60-second trial budget after about 61.8 seconds in the dense leg. The driver
applies 90 seconds to both the client wait and PostgreSQL `statement_timeout`
inside the retrieval transaction; the main CRUD pool keeps its 30-second guard.
This prevents a measured valid cold read from being mislabeled as a zero-match
but does not claim a latency gain or make 90 seconds an acceptable SLO.

Autoprewarm recorded exactly 393,216 blocks, the configured 3 GiB shared-buffer
capacity. On the confirmation restart PostgreSQL accepted connections at
05:29:59 UTC and its Kubernetes Pod became Ready at 05:30:04, while the
background worker did not finish restoring those blocks until 05:30:27.9.
`pg_isready` therefore exposed a roughly 29-second connection-to-restore gap.
The bounded Helm overlay sets its PostgreSQL readiness initial delay to 45
seconds for this reference fixture. This is a measured deployment guard, not a
portable proof of completion; installations with different storage must time
their own restore and adjust the value.

The final restart experiment kept the 8 GiB cgroup limit and alternated restored
centres with previously untouched centres. The 90%-scope cold population used
centres 24–31, disjoint from both the hot set and all earlier cold probes. This
corrected an earlier 2–4 second observation whose vector pages had already been
read by the preceding 10%-scope run.

| Authorized scope / request state | p50 | p95 | Result |
| --- | ---: | ---: | --- |
| 10% / restored first request | 1.050 s | 1.709 s | 25 results |
| 10% / untouched first request | 70.199 s | 77.447 s | 25 results |
| 10% / untouched repeat | 0.191 s | 0.310 s | 25 results |
| 90% / restored first request | 1.349 s | 1.897 s | 25 results |
| 90% / independently untouched first request | 22.603 s | 25.493 s | 25 results |
| 90% / independently untouched repeat | 0.941 s | 0.956 s | 25 results |

The large difference between 10% and 90% cold latency is expected for this
filtered HNSW fixture: a narrow authorized set makes the approximate scan visit
more graph candidates before it can return 25 permitted rows. It is not evidence
that broader permissions are intrinsically faster in every corpus. More
importantly, both scopes collapse to sub-second or low-single-second latency on
repeat, confirming storage reads rather than embedding or reranking as the
dominant untouched-path cost.

A subsequent 105-request HTTP scenario covered login, 10% and 90% authorization,
archive/type/tag/vault filters, result isolation, and five concurrent searches.
All requests returned non-degraded authorized results. Overall p95 was 1.015
seconds and the five-request burst p95 was 0.784 seconds. One first tag-filter
request took 2.124 seconds and first vault-filter requests took 10.289–13.091
seconds; their repeats were fast. These outliers reinforce that the bounded
profile stabilizes the restored working set but cannot pre-populate every filter
and graph path. The database remained below its hard cgroup event threshold
(`memory.events max=0`, `oom=0`, `oom_kill=0`) throughout the sequence, although
Linux correctly used almost all otherwise idle memory for reclaimable cache.

The profile keeps the already validated covering indexes and 0.8 hybrid
common-term cutoff. Those changes reduce avoidable BM25 cache churn; they do not
pin dense pages or remove storage latency. A complete capacity result must report
restored and unseen queries separately, because averaging them hides the user
who happens to issue the first new search after restart.

Fixed memory is a bound, not magic compression. With additional data one or more
of cold latency, storage throughput, CPU, or ANN recall eventually changes. The
next structural step is justified only after the bounded profile misses its
measured objective: partition/reroute by vault or tenant where selectivity is
stable, or compare a disk-oriented vector store whose original vectors remain on
disk while a smaller navigation/quantized layer is cached. Any approximation
must pass exact filtered top-K recall gates; the earlier halfvec experiment was
rejected after recall@20 fell to 0.50–0.55 despite reranking 5,000 candidates.
