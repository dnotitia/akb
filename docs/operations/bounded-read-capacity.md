# Bounded Table browsing and recent document reads

Routine navigation should fetch a page, not count every matching record or
sort the entire resource catalog. This change removes those avoidable costs
without returning approximate totals or changing the exact-count API contract.

## Causes confirmed on PostgreSQL

The isolated synthetic installation had 5,699,793 rows in one Table, about
136,000 documents, two API replicas, and a four-CPU PostgreSQL limit with 64 GiB
memory and 16 GiB shared buffers. Models were mechanical fixtures. No production
installation was modified, and these measurements are not production SLOs.

Actor-specific `EXPLAIN (ANALYZE, BUFFERS)` separated three costs:

| Request shape | Before | After indexes | Scope |
| --- | ---: | ---: | --- |
| Unordered 25-row page | 2.119 ms | 2.005 ms | Already bounded |
| Page plus exact total | 836.340 ms | 458.157 ms | Still scans every qualifying index entry |
| Viewer order, 25 rows | 4,111.813 ms | 0.192 ms | `created_at DESC, id DESC` |
| Global recent, custom-plan observation | 85.747 ms | 0.721 ms | Newest live documents before payload verification |

These are individual plan observations, not HTTP p95 values or physically
cold-storage claims. The baseline exact count already had **zero heap fetches**;
per-row permission lookup or missing VACUUM was not its demonstrated cause.
The default ordered page read 109,611 blocks; the indexed page touched 29 cached
blocks. PostgreSQL selected the new smaller page-order index for the unfiltered
count too, reducing its work, but exact counting remains proportional to data.

The row compiler already limits the page independently from its count CTE.
There was no window-count rewrite to remove. Instead, the frontend requested
`count=exact` for every page, and catalog discovery counted **every Table in a
Vault**, even when the consumer needed schema only.

## Changes

1. Migration 117 adds a partial B-tree on live document resources ordered by
   `(updated_at DESC, resource_id DESC)`. ACL and subscription checks remain
   before `LIMIT`; payload integrity/placement verification is unchanged.
2. New Tables receive a B-tree on `(created_at DESC, id DESC)`. Migration 118
   adds the same index to existing registered Tables. PostgreSQL maintains it
   across REST, MCP, raw SQL and application writes. Names use the existing
   length-bounded, schema-global identifier generator.
3. `GET /api/v1/tables/{vault}?include_row_counts=false` returns schema without
   `row_count`. Omission means unmeasured, never zero. The default remains
   counted and exact for existing clients.
4. `listVaultTableRows` retains its counted default; the viewer explicitly opts
   out and requests `pageSize + 1`. The extra row only determines Next and is
   never displayed. The offset stride remains `pageIndex * pageSize`.
5. Count rows explicitly uses the existing `limit=0` / `Prefer: count=exact`
   contract with the current filters. Empty exact counts are distinct from
   unknown counts. Errors allow browsing and count retry independently.

Counts describe the requested snapshot, not a live counter. Changing filters,
account/access identity, refreshing or mutating rows clears the observation.
An old count never discards an observed row/sentinel or caps a direct page jump
after another writer inserts rows. Last counted page is explicitly a snapshot
shortcut; sentinel-based Next remains authoritative. An exact count can correct
a genuinely empty, current (not placeholder) out-of-range page by replacing its
URL, preserving the previous history entry. Unknown counts do not trigger this
correction or a chain of speculative page requests.

A filtered count is never presented as the total destroyed by dropping the
Table. If its size is unknown, deletion says all its rows rather than claiming
zero. Existing name confirmation, permissions and reference-conflict behavior
remain unchanged.

## Reproduction and comparison

Measure the original four equally weighted cases: a document GET, global
recent, an exact-count page and narrow-Vault search. Warm each case 20 times,
then offer 2/10/20 requests per second for 30 seconds each. Independently offer
two small document reads per second and 0.2 integrity-checked write workflows
per second. Each workflow creates, reads, CAS-updates, reads and deletes its
own canary. Verify head transitions, hashes and final cleanup.

Use a fixed-arrival generator with a 15-second client deadline, 64 main
in-flight slots and a 250 ms start tolerance. Record admission drops separately
from HTTP/server errors. Include the final drain in completion-rate calculations.
Do not retry failures invisibly or publish success-only latency without counts.

The same exact-count mix must be compared separately from the new bounded
browsing mix: the latter intentionally does not request a total. Do not claim
that a page-only request has the semantics or cost of exact counting.

| Rate | Original exact-count mix: success / timeout / drop | Same exact-count mix after indexes | Bounded browsing mix after changes |
| --- | --- | --- | --- |
| 2 RPS, 60 offers | 60 / 0 / 0 | 60 / 0 / 0 | 60 / 0 / 0 |
| 10 RPS, 300 offers | 188 / 69 / 43 | 300 / 0 / 0 | 300 / 0 / 0 |
| 20 RPS, 600 offers | 180 / 79 / 341 | 253 / 87 / 260 | 600 / 0 / 0 |

The original and indexed exact-count lanes use the same request, including an
exact 5,699,793 total. At 20 RPS the indexes alone **do not remove overload**.
Routine browsing replaces that lane with 25 visible rows plus a sentinel in
the viewer order. The separately tested Count rows action still returns exact
totals; it is not invoked once per page.

In the complete bounded 2/10/20 RPS run, the 20 RPS phase drained in 30.123
seconds, with HTTP p95 document/recent/page/search of
413.90 / 452.78 / 481.31 / 641.03 ms (150 successful samples each). All 60
background reads and six write workflows succeeded in that phase. An additional
independent 20 RPS repeat, without a concurrent local frontend test gate,
again succeeded for all 600 requests, 60 background reads and six workflows;
its p95 was 167.24 / 157.04 / 100.17 / 388.05 ms and drain 30.129 seconds.
This spread illustrates client/network/scheduling variation, not two distinct
performance guarantees. All runs retain their failures rather than averaging
them away. Post-load integrity workflow and fixture-Vault cleanup succeeded.

Background failures are separate from the main-lane table. Before changes the
20 RPS phase had four background read timeouts and 18 dropped background starts;
one attempted write workflow failed, one cleanup failed and one workflow was
not started. Indexes alone still had background/write failures at 20 RPS.
The bounded browsing run had none. No data-integrity mismatch was observed in
successful checks, which is not a claim about the unseen outcome of timeouts.
The baseline 2 RPS background lane also recorded one transport protocol error
(59 successful reads); it was not retried or reclassified as a count defect.

The QA installation held previously verified fixes, including the BM25 growing
prefilter correction, constant across these comparisons. They are not part of
this main-based diff. An initial harness used `items` instead of the actual
recent response's `changes`; that run was retained as invalid and excluded,
not counted as a product failure. The reported baseline used the corrected
contract. A separate preliminary bounded run with functional probes overlapping
was also retained; the complete run and independent repeat above are separate.

## Verification

- Real PostgreSQL: default/explicit counted catalog, schema-only no-count
  calls, existing/new page indexes, migration idempotence and UUID tie order.
- Existing recent-feed ACL, watch identity, equal-timestamp cursor and deletion
  tests; existing query compiler, security, schema and idempotent-create tests.
- Real HTTP: counted/default and unmeasured catalog, page/sentinel boundaries,
  exact and filtered count-only requests, row update/CAS/cleanup, Reader access,
  Outsider denial and Reader write denial.
- Frontend: unknown/exact/empty totals, count failure/retry, sentinel trimming,
  refresh invalidation, concurrent-insert navigation and the unchanged row
  management flows. Chromium checks mobile, desktop, dark and landscape layouts,
  reduced motion, keyboard page entry, retry and horizontal overflow.

Selected backend regression: 149 passed, including real PostgreSQL. Real HTTP:
six passed. Frontend: 1,745 tests in 178 files passed, plus four Chromium layout
scenarios. Ruff, selected-source mypy, configured Bandit medium/high checks,
design checks, TypeScript, lint and secret scanning passed. The local frontend
runtime was Node 24; hosted CI remains responsible for the pinned Node 22 lane.
An independent review caught stale-count navigation and its input maximum;
both were removed and covered by deterministic concurrent-insert regressions.
Integration with the current Table workspace also covers schema-overlay focus,
roving cell keys, page history, and exact-count correction only after a current
empty-page receipt. Loaded rows always take precedence over a historical count.

## Upgrade and limits

Deploy the backend for the catalog option/migrations and the frontend for the
new consumer flow. No PostgreSQL image change is required for these indexes.
Migrations 117–118 run in the existing atomic migration runner; index creation
can block writes until commit. The measured fixture build was about 0.1 seconds
for recent and 4.6 seconds for all registered Table page indexes, including a
roughly 221 MiB index on the large Table. Larger installations should schedule
the first upgrade accordingly and reserve disk for the indexes.

No database memory, CPU, timeout, query-plan setting or search candidate limit
was increased. Custom sorts/filters can need their own appropriate indexes;
deep OFFSET pages and repeated full exact counts are not constant-cost. An
ordered global feed can still examine many inaccessible resources for a narrow
actor. This change does not bypass filtering to manufacture bounded work.

Client timeout is not proof that server work stopped, and a timed-out write
can have committed. Cancellation/transient failure contracts are separately
tracked; always verify by observing server work and reading committed state.
An actual HTTP cancellation probe first observed a bounded four-second
read running in PostgreSQL, then cancelled the client. That same backend PID
was still active at 0.55, 2.03 and 3.11 seconds after cancellation and absent
at 4.49 seconds. No cancellation fix is claimed here; this is concrete input
for the separate transient/cancellation follow-up, not evidence of a hung DB.

PostgreSQL documents the [linear work of an exact full-table count](https://www.postgresql.org/docs/16/functions-aggregate.html)
and how a matching [B-tree avoids sorting the complete input for ORDER BY/LIMIT](https://www.postgresql.org/docs/16/indexes-ordering.html).

## Review validation on current main

The integrated branch preserves the shared denied-query cache boundary. A
recount returning 403/404 now clears the previous successful count, and a later
503 cannot resurrect it. A fresh authorized count may populate it again;
regressions cover that sequence without a focus change or access-revision bump.

The six catalog/page/count/CAS/ACL HTTP contracts run in the mandatory
`backend/tests/mcp_e2e/test_read_capacity_e2e.py` runtime lane. Every case uses a
new non-admin owner; Reader and Outsider actors are minted through the same
isolated API. No optional endpoint or actor PAT can skip this coverage. The
historical large-corpus measurements above are separate from this review's
focused correctness validation; they are not claimed as newly rerun results.

Integration with PR #761 preserves sanitized transient errors and cancellation
of selected abandoned GET reads, including Table catalog/page requests. Counted
catalog reads propagate dependency failures rather than reporting a false zero;
schema-only reads still avoid the count altogether. The cancellation probe above
is historical evidence from before that separate fix, not the current cancellation
contract. See [database recovery](database-recovery.md) for its scope and limits.

Integration with PR #762 also preserves bounded Vault navigation and ancestry.
The Table location publishes its resource reference from the authorized
schema-only catalog; tree paging and count/page query caches remain separate.
Unknown Table sizes use the shared deletion wording without inventing a zero.
