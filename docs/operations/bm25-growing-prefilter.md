# BM25 growing-segment prefilter verification

This records the query-time follow-up in [#748](https://github.com/dnotitia/akb/issues/748).
It is a synthetic, warm-read comparison, not a production relevance benchmark,
physical-cold-cache result, or capacity/SLO guarantee.

## Cause and change

The pinned upstream VChord-BM25 0.3.0 applies its executor snapshot/SQL-qual
prefilter to sealed candidates but not to growing candidates. High-scoring
growing rows that are invisible or outside the authorized scope can fill the
finite top-k and raise its pruning threshold. PostgreSQL subsequently rejects
them, so the filtered page is short even though more authorized matches exist.
AKB correctly repairs a short page with `bm25_limit=-1`, but common terms then
enumerate and sort millions of postings.

Patch `0006` in `deploy/postgres/vchord_bm25/` applies the same existing
prefilter to growing candidates **before they enter top-k**. It computes the
score first: zero-score and below-threshold candidates still avoid heap
visibility/qual checks. The operator's prefilter opt-out, SQL ACL predicates,
exact fallback, scoring, statistics, candidate limits and deadlines are unchanged.
There is no new cache, index partitioning scheme, or infrastructure service.

Upstream reference: [pinned scan implementation](https://github.com/tensorchord/VectorChord-bm25/blob/0.3.0/src/index/scan.rs).
The patch is offered under the extension's existing licensing terms.

## Controlled conditions

- Baseline application: main `63eb62ec` (same search code as the original QA
  baseline `99474c10`). Unrelated API fixes were present in both HTTP phases.
- PostgreSQL 16.15, pgvector 0.8.6, VChord-BM25 0.3.0 with the repository patches.
- Approximately 2.17 million synthetic 1,024-dimensional vectors, common terms
  and a long-tail vocabulary; sparse index approximately 8,203 MiB.
- Database: four CPUs, 64 GiB limit, 16 GiB shared buffers; two API replicas.
  No resource, timeout or configuration increase between phases.
- Only the isolated test database image changed. Existing persistent storage
  was retained; no REINDEX, VACUUM, fixture rewrite or manual cache flush was
  performed as part of the comparison. The index metapage was byte-for-byte
  identical before and after: 2,172,060 sealed IDs and 30,206 growing IDs,
  including obsolete candidates. IDs are not a count of live documents.
- Reader scope covered about 64% of fixture chunks and Writer about 96%, not
  a 10%/90% experiment. A narrow Vault used the existing materialized route.
- Embedding/reranking were mechanical fixtures. Query plans were measured
  without concurrent HTTP load; both HTTP phases used the same client, case
  order, credentials, direct-replica routing and arrival schedule.

## SQL/driver observations

One direct sparse-driver measurement per case, requesting 75 candidates:

| Common-term scope | Before (ms) | After (ms) | Candidate path |
| --- | ---: | ---: | --- |
| Unfiltered | 29.52 | 38.37 | finite in both |
| Hot Vault | 2,399.41 | 171.40 | finite + exact → finite |
| Reader broad | 2,373.03 | 28.07 | finite + exact → finite |
| Writer broad | 25.30 | 26.14 | finite in both |
| Narrow Vault | 40.08 | 58.41 | materialized in both |

These single observations demonstrate routing/work, not stable tail latency or
uniform speedups. The narrow and unfiltered routes did not acquire a new fast
path. In the Reader plan, the old finite scan returned only 20 rows, followed
by an exact scan with 15,626,371 shared-buffer hits. The new finite scan
returned all 75 with 123,814 hits and no exact completion. For the hot Vault,
the old finite page was empty; the new finite page returned 75.

The separate rare-term cases returned 20/0/0/14/20 rows across these scopes.
Their genuine short pages still completed exactly. Before/after driver times
were approximately 25–80 ms; this patch does not eliminate the cost of a
genuinely short or empty exact search. All ten before/after cases preserved
the exact-oracle score sequence and authorized scope; tied IDs are not assumed
to have a deterministic order.

## Fixed-arrival HTTP comparison

Each rate ran for 30 seconds. The seven equally interleaved cases were four
common-term scope searches, a selective empty search, a document GET and
liveness. Client timeout was 15 seconds, with at most 64 in-flight requests;
late/capacity-limited arrivals were **recorded as drops**, not delayed until
the system could accept them. There were no automatic retries.

| Offered rate | Phase | Offered / started | Valid successes | Timeouts | Other errors | Drops | Drain-inclusive completions/s |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| 2 RPS | before | 60 / 60 | 60 | 0 | 0 | 0 | 1.797 |
| 2 RPS | after | 60 / 60 | 60 | 0 | 0 | 0 | 2.021 |
| 10 RPS | before | 300 / 200 | 105 | 94 | 1 protocol error | 100 | 4.484 |
| 10 RPS | after | 300 / 300 | 300 | 0 | 0 | 0 | 10.015 |
| 20 RPS | before | 600 / 156 | 29 | 127 | 0 | 444 | 3.934 |
| 20 RPS | after | 600 / 600 | 600 | 0 | 0 | 0 | 19.910 |

Completions/s includes failed attempts and drain time, not successful requests
alone. At 20 RPS before the fix, **none of the four common-search cases
succeeded**; reporting only successful latency would hide the failure. After
the fix all 600 responses passed status, shape, completeness and scope checks.
No observed successful search response was degraded in either phase; client
timeouts do not prove what an eventual server response would have contained.

At 20 RPS after the fix, descriptive successful HTTP p95 (86 samples per search
case) was 528/412/391/403 ms for Reader/hot-Vault/narrow-Vault/Writer. Document
GET p95 was 251 ms (85 samples), with no errors. Before, document GET had six
successes and fourteen timeouts, with 65 offered arrivals dropped. The simple
API benefited from less shared-database contention; it was not independently
optimized here. The 30-second runs are not sustained-load or stable p99 proofs.

## Regression and delivery

- Seven original regression cases failed the old image at actual finite-result
  assertions: Vault/source/type scope, deleted/uncommitted/old-snapshot TIDs,
  and HOT-chain snapshot visibility. A no-overlap cost control passed the old
  implementation and rejects a naive check-before-score patch.
- Nine new tests pass the rebuilt image, including upsert/delete freshness
  without REINDEX. The selected extension suites total 88 passing tests:
  candidate budgets (prefilter on/off), shape/encoding, exact completion,
  index-build scoring/statistics and VACUUM regressions.
- CI explicitly runs the new suite against the built extension image. Ruff,
  mypy, repository-configured Bandit, secret scanning and independent review
  passed locally.
- Deploy the rebuilt **PostgreSQL image**, then establish new sessions. A
  backend-only restart cannot replace a server-side extension library. This
  patch changes no on-disk format or SQL extension version and requires no
  REINDEX or data migration. See [deployment details](../../deploy/postgres/README.md#bounded-search-and-exact-completion).

## Remaining boundaries

This fixes avoidable exact work caused by growing candidates, not every search
delay. Dense reads, hydration, other global/count workloads, genuine exact
completion, stale block bounds, and cancellation behavior during overload remain
distinct concerns. The overloaded baseline also drained slowly after client
timeouts; no request-timeout responsiveness guarantee is inferred from this
patch. Those general recovery/capacity questions remain with #749 and #750.
No production deployment or data was changed during this verification.
