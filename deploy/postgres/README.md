# PostgreSQL image with the BM25 index extension

This image adds [`vchord_bm25`](https://github.com/tensorchord/VectorChord-bm25),
which stores raw term frequencies and owns corpus statistics and BM25 scoring
in a block-max index. With `vector_store_sparse_shape: auto`, the default, a new
database on a server that provides it gets the `vchord` shape. Posting stores
application-computed weights; identical rankings across the two scorers are not
a compatibility guarantee.

The repository's install paths build this image rather than pull a prebuilt one:

| Install path | How it gets this image |
| --- | --- |
| Compose (`docker-compose.yaml`, the README quickstart) | builds it as the `postgres` service |
| `deploy/k8s/deploy.sh` | builds and pushes `akb-postgres`, and puts it in the rendered manifests |
| Helm | the README's install commands build it and set `postgres.image` |
| `deploy/k8s/native/` | its guide adds an `images` entry for it |
| CI runtime e2e | builds it for its dependency stack |
| all-in-one | does not include it; a new database there gets `posting` |

Building and running it is use of the extension. Publishing an image with the
extension in it is distribution (see the Dockerfile's licensing note), which is
why the all-in-one, the one image AKB publishes, leaves it out.

A server left on the stock `pgvector/pgvector:pg16` image gives a new database
`posting`, the table the backend maintains itself. The chart and the Kubernetes
base manifest keep that image as their literal default, so an upgrade never
points a running database at an image nobody has built.

The shape is decided once per database, at startup, and recorded in
`<vector_store_schema>.install_state`, so an existing installation keeps the
shape it serves whatever image it later runs on. `/health` reports it under
`vector_store.sparse_shape`: what was configured, what is in effect, and why.

## Build

```bash
docker buildx build -f deploy/postgres/Dockerfile \
  --platform linux/amd64,linux/arm64 \
  -t <registry>/akb-postgres:pg16-bm25 --push .
```

Both stages are pinned by multi-architecture index digest, so the build is
reproducible and enabling the extension does not move the PostgreSQL version at
the same time. To move either pin, follow the procedure in
[`../k8s/README.md`](../k8s/README.md).

## Enable

The extension needs a superuser to create. Where AKB's database role is one —
the stock image's `POSTGRES_USER` is — AKB creates it on a new database by
itself. Otherwise a superuser runs, in AKB's database, before AKB first starts
on it:

```sql
CREATE EXTENSION vchord_bm25;
GRANT USAGE ON SCHEMA bm25_catalog TO <akb role>;
```

The GRANT is not optional: the extension's schema carries no USAGE for other
roles, and without it a plain role fails the schema setup with "permission
denied for schema bm25_catalog". AKB checks both before it decides, and stops
with this instruction rather than quietly choosing `posting` for a database
somebody prepared for `vchord`.

The extension installs into its own `bm25_catalog` schema and needs no
`shared_preload_libraries` entry.

Without a preload the library loads the first time a session calls into it, and
its settings (`bm25_catalog.bm25_limit`) exist only from then on. The backend
reads that setting on connections that may not have loaded the library yet, so
it loads it first; a value set in the server configuration is read as is.
Tests must run against a server started the same way: the upstream image
preloads the library from its CMD, which hides exactly this, so CI starts it
with a plain `postgres` command.

A database that already serves `posting` stays on it under `auto`, even on this
image. Moving it is `scripts/backfill_bm25_vector.py`, and then naming `vchord`
in the setting: select it only after `--index` has built the index. Until then
the backend refuses the shape rather than building the index itself: at startup
it would build it inside its schema transaction, blocking writes for the build,
over a column the backfill had not finished. A new, empty database gets the
index at startup, built empty and filled by inserts, which also keeps it clear
of the build-path defect described below.

Switching back is kept possible for as long as it is wanted. While a `posting`
table exists, `bm25_external_stats_mode` `auto` — the default — and `required`
keep writing it too, so setting the shape back to `posting` serves the rows as
they are. Once the way back is no longer needed, set `vchord_only_verified`: the
writes to `posting` and the statistics recompute both stop, and the table can
be dropped. A new vchord installation has no `posting` table, and under `auto`
nothing recomputes the external statistics for it.

## Tokenization is not affected

The extension ships no tokenizer of its own; `vchord_bm25` 0.3.0 is the index
and the scoring, nothing else. Its vector type is built from an integer array
of term ids, which is what AKB's `bm25_vocab` already mints, so the Korean
tokenizer stays exactly where it is.

## Before moving the extension pin

The sparse query path filters on the sign of the score: a document holding any
query term scores strictly negative, one holding none scores exactly `-0`, and
the filter keeps the negatives. That holds because this extension's IDF is a
log1p variant which stays positive at every document frequency — measured at
`df = N`, all fifty matching documents still scored below zero.

Classic BM25 IDF, `log((N-df+0.5)/(df+0.5))`, turns negative once a term is in
more than half the corpus. An extension version that switched to it would make
the filter discard real matches for common terms, silently and only for the
terms most queries contain.

So when moving this pin, check that a document containing a query term still
scores below zero at high document frequency before accepting the bump.

## Licensing

`vchord_bm25` is published under the GNU Affero General Public License v3 or
the Elastic License v2, at the recipient's option. It is a separate program
that runs inside the PostgreSQL server and is reached over the PostgreSQL wire
protocol, so it does not change the licensing of AKB itself.

Building and running this image is use. **Distributing** the built image is
distribution of that extension and carries the corresponding obligation —
unmodified, that is an offer of the upstream source, which the link above
satisfies.

## Bounded search and exact completion

`bm25_catalog.bm25_limit` is the size of the extension's internal top-k. A search
asks for one finite page first. When that page comes back full, it is the answer.
When it comes back short, the same query runs again with `-1` and that result is
the answer (akb#673).

A short finite page is not proof that nothing else matches:

- Growing-segment rows are scored without the query's filter, and they take top-k
  slots. So can rows the current snapshot cannot see.
- In `vchord_bm25` 0.3.0, an index built by `CREATE INDEX` or `REINDEX` can store a
  best score of 0 for the first full blocks of a term's list. The build saves a
  128-posting block's summary before its best posting is counted. A bounded scan
  skips those blocks outright. The next section covers what this means for full
  pages.

`-1` reads every posting of the query terms and leaves visibility and the
filter to the executor, so it is the only complete scan. What it costs is those
postings plus the growing segment, and one heap check for each candidate the
executor reads before the page fills. The corpus size is not the measure. A page
that came back short had already read every posting it could, because pruning
only starts once the internal top-k holds more than twice its size. Completing it
therefore repeats work of the same order. On a 2.1M-chunk corpus, completing took
60 ms to 2.5 s.

Before this, a short page widened the finite budget up to 65,535 and then refused
exact work whenever the corpus held more than 10,000 rows, which is always in
production. Each widening probe scored every candidate again in the executor.
That path took 0.1–8 s and ended with `degraded: true` on every search whose scope
held fewer matches than the page. In the 90 such searches measured, the final
top-10 matched what the `posting` shape returned for the same request.

Materialising a selective scope scores every row in the scope, so it is limited to
scopes of at most 10,000 rows (`_VCHORD_MAX_MATERIALISED_ROWS`). A larger
selective scope is not refused. It is searched index-led, like any wider filter.
Which shape runs is a latency decision and must never change the rows: refusing
there left every scope between 10,000 vectors and 1% of the corpus with no
sparse results at all (akb#626).

Finite queries, the selectivity lookup and exact queries keep the existing caller
and database-pool timeouts. This path does not install a shorter wall clock or
statement timeout, so a query that takes longer than five seconds can still finish
within the caller's budget. Caller cancellation and server timeouts still roll
back the local plan, candidate-budget and search-path settings.

### Full pages from a rebuilt index

The block defect above also affects a page that comes back full. The skipped
blocks' postings never compete for the page, so a full page can be missing better
matches.

Measured on an index built with `CREATE INDEX CONCURRENTLY` over 2.1M chunks: for
150 randomly sampled terms with 128 to 20,000 matches, plus nine query terms,
compared one term at a time, the bounded top-90 had a strictly worse score than
the exact top-90 at some rank for 7 of the 159 terms.

- An index built empty and filled by inserts does not have the defect, because
  inserted rows are merged in by a different path.
- A `REINDEX` brings the defect back.

Check this before rebuilding the index or moving the extension pin.

Run `test_vchord_candidate_budget_postgres.py` against the pinned extension. It
covers:

- finite pages and their exact completion;
- scoped materialised ranking;
- every exact route, including a page shortened by a rebuilt index;
- connection recovery.

Hybrid and service regressions cover:

- short sparse pages completed beside the dense leg, in both completion orders;
- ACL filters;
- degradation accounting for drivers that do return partial results.
The test DSN must identify an isolated PostgreSQL instance;
the tests create disposable databases.
