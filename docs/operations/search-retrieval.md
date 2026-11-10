# Scoped pgvector retrieval

AKB combines dense vector and sparse keyword candidates before hydrating and
optionally reranking the selected documents. Search filters apply to the full
authorized corpus; returning a bounded ranked result does not provide an
exhaustive semantic document listing.

## Query paths

Explicit Native document URIs resolve through separate `(namespace, path)` and
UUID index lookups. Each identifier stays paired with its vault. Path and UUID
matches are unioned, preserving ambiguity and duplicate handling without a
resource-corpus scan or a `resource_id::text` comparison.

For the `posting` sparse shape, PostgreSQL statistics select an exact SQL path:

- **Term first:** start with matching terms, then apply the authorized scope.
  This remains the default for rare terms, broad scopes, or missing statistics.
- **Scope first:** materialize matching chunk IDs, then join their postings.
- **Point lookups:** probe `(term_id, chunk_id)` when a small scope and common
  terms would otherwise produce many candidates. Check the actual scope size
  first: at most 10,000 chunks and 100,000 term/chunk probes qualify. A larger
  scope falls back to the term-first path, retaining every matching candidate.

All paths use the same filters and sparse score formula. Statistics choose the
query shape; they do not decide which rows match. Per-request custom plans keep
prepared calls sensitive to the current term and filter distributions.

Dense search retains HNSW candidate retrieval, materializes those candidates,
and sorts by actual distance with an ID tie-breaker before reciprocal-rank
fusion. Iterative scans also apply to source-type-only filters. HNSW remains an
approximate search; distance reordering does not turn it into exhaustive search.

## Retrieval budget and incomplete results

Configure this in `config/app.yaml`:

```yaml
search_retrieval_timeout_secs: 30
```

The accepted range is greater than zero and at most 30 seconds. The default is
30 seconds. The setting applies to the pgvector driver, including all its
sparse shapes. Other vector drivers retain their own timeouts.

One absolute budget covers pool acquisition, concurrent dense/sparse queries,
vector payload loading, and bounded cancellation/reset. Small portions are
reserved for payload loading and cleanup, so a retrieval leg can expire before
the configured total. Forced termination has a bounded 10 ms scheduling grace.
A stricter nonzero PostgreSQL session statement timeout remains in force.

This does **not** bound the entire HTTP request: schema initialization, query
embedding, Native candidate resolution, canonical document hydration and
reranking are outside the driver budget.

| Outcome | Driver behavior |
| --- | --- |
| One leg times out | Keep the completed peer, mark `dense_leg_timeout` or `sparse_leg_timeout` |
| Both legs or payload retrieval time out | Empty incomplete response, `retrieval_timeout` |
| One leg has a transient availability failure | Keep the completed peer, mark `dense_leg_failed` or `sparse_leg_failed` |
| Retrieval is unavailable | Empty incomplete response, `retrieval_unavailable` |
| Authorization, schema, programming or vocabulary-fence failure | Propagate to the existing service error/retry handling |
| Caller cancels | Cancel and drain active retrieval work |

A connection belongs to the search supervisor until bounded pool release
begins. The pool then owns cancellation/reset; repeated request cancellation
must not race it by terminating the same connection. Closed-transaction cleanup
wrappers preserve the underlying query error.

The search dialog labels incomplete results, keeps available hits openable,
retains the selected scope when retrying, and distinguishes incomplete empty
responses from a successful search with no matches. Counts describe displayed
results rather than claiming a complete result count.

No migration, index rebuild, vocabulary renumbering, or sparse-shape switch is
required by these application changes. Existing epoch fencing, sparse-shape
selection and VChord short-page completion remain in place.

## Comparative observations

A synthetic corpus contained 2,101,940 chunks, 525,537 resources, 134 vaults,
152,241,792 postings and 1,024-dimensional vectors. PostgreSQL had a 4-CPU/
20-GiB limit, the API 1 CPU/3 GiB, and the deterministic embedding/reranking
fixture 1 CPU/1 GiB. Both versions used the same database and `posting` shape.

Three query types (common, rare, mixed), five scopes, rerank on/off, limit 10,
and concurrency 1/4 produced 60 searches plus 12 document/liveness probes per
version. Each case ran once per concurrency level. The baseline preceded the
candidate; cache state was not flushed or interleaved. The baseline was 0.16.1
at `fb6195e9`. These are comparative observations, not a capacity certification
or a relevance evaluation.

| Scope | Baseline median, ms | Candidate median, ms |
| --- | ---: | ---: |
| All vaults | 4,777 | 4,890 |
| Largest vault | 3,213 | 1,570 |
| Smallest vault | 706 | 75 |
| Two vaults | 3,205 | 1,556 |
| 26 explicit sources | 6,910 | 72 |

All 72 candidate requests succeeded without recorded degradation or scope
violations. Compared with baseline, 24/60 searches retained ordered IDs and
scores, 30/60 retained membership, and 60/60 retained count/flags. All 12
explicit-source cases retained ordered IDs and scores. Dense candidate and
ordering changes mean baseline result equivalence is not claimed.

Repeated small-scope sparse SQL fell from 911 ms to 12.7 ms, with temporary
blocks dropping from 4,935 read/written to zero. Broad sparse SQL still took
about 5.4 seconds with 7.24 million shared-buffer hits and temporary spill;
its corresponding dense query took about 12 ms. Zero shared-block reads on
that repetition do not measure physical cold-read performance. Broad common-
term aggregation remains an open performance limit.

These measurements cover the application changes under `posting`; they do not
measure VChord speed or validate production latency. The full-scale fixture is
not required by the regression suites below.

## Regression checks

From `backend/`, after installing the normal runtime and test dependencies:

```sh
python -m pytest -q \
  tests/test_pgvector_hybrid_deadline_unit.py \
  tests/test_search_release_ownership_unit.py \
  tests/test_posting_scope_strategy_unit.py \
  tests/test_search_retrieval_config_unit.py \
  tests/test_native_search_selection_unit.py \
  tests/test_vector_search_degraded_unit.py \
  tests/test_bm25_query_epoch_unit.py \
  tests/test_bm25_vocab_epoch_unit.py
```

For database checks, point `AKB_SEARCH_TEST_DSN` at a disposable PostgreSQL server
with pgvector and `AKB_VCHORD_TEST_DSN` at one with the repository's patched
VChord extension. The login must be able to create/drop databases. Tests create
isolated databases and clean them up; no pre-existing application schema or
large corpus is needed.

```sh
python -m pytest -q \
  tests/test_pgvector_hybrid_deadline_postgres.py \
  tests/test_pgvector_scope_routing_postgres.py \
  tests/test_native_search_source_scope_postgres.py \
  tests/test_hybrid_leg_failure_postgres.py \
  tests/test_bm25_query_epoch_postgres.py \
  tests/test_vchord_candidate_budget_postgres.py
```

The live-PostgreSQL job in
[backend-pytest.yml](../../.github/workflows/backend-pytest.yml) supplies those
servers and runs every backend test outside `backend/tests/mcp_e2e` that does
not declare `@pytest.mark.needs_resource`, failing on any skip, so these suites
cannot skip in both CI jobs.
For UI and repository checks, follow [CONTRIBUTING.md](../../CONTRIBUTING.md).

## Document time windows in the storage service

`SearchService.search` accepts an internal `DocumentTimeRange` with
`source_type="document"`. MCP and REST discovery do not advertise this input.
Both endpoints of the range are timezone-aware instants. Supply at least one
of `gte` (inclusive) and `lt` (exclusive). Two bounds must form a nonempty
interval. Conversion to UTC preserves the instant, including at day boundaries.

Use the clocks returned by a **current document read**:

| Operation | `created_at` | `updated_at` |
| --- | --- | --- |
| Create, including import as a new OKF document | AKB creation time; OKF's source timestamp is informational | Initial write |
| Edit body or update tags/metadata through the document service | Preserved | New document revision |
| Move to a new path with the same resource identity | Preserved | Move revision, even when body bytes are identical |
| Rebuild derived chunks or process a Native indexing intent | Preserved | Preserved |
| Bare Git external mirror catalog upsert | First local sync of that document | Timestamp supplied by the updating sync |

On **Bare Git**, both fields come from the PostgreSQL `documents` row. The Git
author/committer date and the indexing worker's completion time do not enter
the predicate.

On **Native**, creation time follows the current head's `created_at`
frontmatter, with missing metadata filled from a matching frozen legacy row
after cutover. When neither supplies a value, the resource's creation time
applies. An imported creation instant may precede the Native resource by
years. Updated time is `native_resources.updated_at`, the authority clock for
the published head. A move advances that clock even when the moved payload
still contains an older frontmatter timestamp.

### Selection and limits

The service intersects the time predicate, readable Vaults, document filters
and any explicit source URIs before dense/sparse retrieval and reranking.
The driver receives eligible source IDs and the active document source kind.
An empty candidate set returns immediately. A driver that returns an ID or kind
outside the requested scope fails the operation. Hydration also checks the
current timestamp, so a write that leaves the window during retrieval cannot
surface as an in-window result.

- Bare Git binds the bounds in catalog SQL. It fetches at most 10,001 IDs to
  distinguish a supported set of up to 10,000 from a scope that needs narrowing.
- Native filters `updated_at` in SQL. To match public creation dates and
  imported metadata, it verifies canonical head bodies in the bounded scope.
  The candidate transaction refuses scopes above 10,000 resources or 128 MiB
  of canonical bodies before fetching payload bytes. A missing canonical body
  or an ambiguous creation timestamp fails explicitly.
- The date request uses `search_retrieval_timeout_secs` (default 30 seconds)
  as an overall deadline, including candidate reads and pool waits. A caller
  cancellation propagates. The integration suite also holds a real catalog
  lock to exercise timeout, cancellation and pool reuse.
- pgvector receives the ID and kind predicates on its dense and sparse legs.
  Qdrant translates these to payload filters and Seahorse Cloud, REST and gRPC
  use their shared SQL filter builder. These drivers reuse existing retrieval
  failure/degradation handling. No retry broadens the query by dropping the
  scope. The integration measurement below uses live pgvector; external
  driver verification covers request construction in their contract suites.

### Local fixture measurement

`tests/test_document_search_time_postgres.py` exercises both document
services on disposable PostgreSQL, real Bare Git repositories and a real
pgvector index. The ranking fixture contains 53 better-scoring, out-of-window
documents (per-leg prefetch 50), one eligible weaker hit, one same-Vault
document omitted from the requested URIs, and one matching private document.
The owner/read scope and source URIs leave 54 readable, requested documents
before the date test. Each dated query returns exactly the weaker hit.

One local run used fixed query features, a two-dimensional pgvector arrays
index, reranking disabled, limit 1, one warm-up and 20 measured service calls
per case. p95 is the 19th sorted observation. Bytes below are canonical
**candidate** body bytes read, excluding final hit hydration. These timings
describe this fixture only.

| Storage | Field | Candidate body bytes | Eligible IDs sent | Service p95 |
| --- | --- | ---: | ---: | ---: |
| Bare Git | created_at | 0 | 1 | 37.54 ms |
| Bare Git | updated_at | 0 | 1 | 33.76 ms |
| Native | created_at | 9,864 (54 heads) | 1 | 46.62 ms |
| Native | updated_at | 175 (1 head) | 1 | 36.93 ms |
