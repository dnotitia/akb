# PostgreSQL image with the optional BM25 index extension

AKB's sparse retrieval leg does not need this. It works against the `posting`
table the backend maintains, on the stock `pgvector/pgvector:pg16` image named
everywhere else in this repository.

This image adds [`vchord_bm25`](https://github.com/tensorchord/VectorChord-bm25),
which stores raw term frequencies and owns corpus statistics and BM25 scoring
in a block-max index. Posting stores application-computed weights; identical
rankings across the two scorers are not a compatibility guarantee. An operator who wants that trade can build the image here and
get the same bytes we do.

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

```sql
CREATE EXTENSION vchord_bm25;
```

The extension installs into its own `bm25_catalog` schema and needs no
`shared_preload_libraries` entry. A database that never runs `CREATE EXTENSION`
behaves exactly like the base image.

Note that installing the extension is not by itself enough for AKB to use it —
the sparse leg selects its implementation separately. This image only makes the
option available.

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

## Bounded search and exact fallback

The VChord reader widens finite candidate budgets up to 65,535. A short page is
not proof that all matches were visited: growing segments and invisible index
entries can consume that budget. Exact fallback remains available only when a
bounded cardinality probe finds at most 10,000 non-NULL vectors. Index-led `-1`
checks the global corpus, since the extension may scan it before filtering;
materialized ranking checks its vault/source scope. Planner estimates alone do
not authorize exact work. Operator-configured `-1` and oversized top-k requests
use the same guard.

A scope the selectivity estimate would materialize, but that holds more than
10,000 vectors, is not refused: it is searched index-led, like any wider
filter. Which shape runs is a latency decision and must never change the rows —
refusing there left every scope between 10,000 vectors and 1% of the corpus
with no sparse results at all, while the index-led shape answered the same
scope.

Finite index queries, selectivity lookup and exact queries retain the existing
caller and database-pool timeouts. This path does not install a shorter wall
clock or statement timeout: a query that takes longer than five seconds can
still finish within the caller's budget. Caller cancellation and server timeout
still roll back local plan, candidate-budget and search-path settings.

When the exact row cap refuses completion, the driver retains already fetched,
scoped finite sparse candidates and waits for the independent dense leg. It
fuses and fetches the surviving hits, then carries them in `VectorSearchDegraded`
with reason `sparse_search_budget_exceeded`. The search service passes these hits
through its normal hydration/filtering and returns `degraded: true` with that
reason. A sparse-only request can retain finite hits too; if neither leg has
usable hits, the response is explicitly degraded and empty. Size refusal does
not masquerade as a complete result or discard a successful dense leg. Genuine
store/payload failures and caller cancellation keep their existing behavior.

The 10,000-row threshold is a conservative exact-work safeguard, not a relevance
or latency acceptance target. A broad underfilled query can still be incomplete;
validate that tradeoff before deploying VChord. Preserving hits does not prove
exact top-k completeness for that request.

Run `test_vchord_candidate_budget_postgres.py` against the pinned extension to
exercise candidate widening, scoped exact ranking, global fallback refusal and
connection recovery. Hybrid/service regressions cover retained dense and sparse
hits, both leg completion orders, ACL filters, and explicit degradation accounting.
The test DSN must identify an isolated PostgreSQL instance;
the tests create disposable databases.
