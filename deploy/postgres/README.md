# PostgreSQL image with the BM25 index extension

This image adds [`vchord_bm25`](https://github.com/tensorchord/VectorChord-bm25),
which stores raw term frequencies and owns corpus statistics and BM25 scoring
in a block-max index. The Dockerfile compiles it from the 0.3.0 source with four
fixes, described under [Index builds](#index-builds-akb679),
[Document length statistics](#document-length-statistics-akb684) and
[VACUUM](#vacuum-akb687). With `vector_store_sparse_shape: auto`, the default, a new
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
docker buildx build -t <registry>/akb-postgres:pg16-bm25 --push deploy/postgres
```

This directory is the build context: the Dockerfile copies `vchord_bm25/` from
it. The first build compiles the extension, which takes under a minute on a
many-core host and several on a laptop.

What decides the extension is pinned:

| Input | Pin |
| --- | --- |
| PostgreSQL and pgvector | the pgvector base, by digest; the same digest as `deploy/k8s/postgres.yaml`, so adding the extension does not move the PostgreSQL version |
| Rust | 1.91.1, by digest; the compiler the upstream 0.3.0 binary was built with |
| Extension source | the upstream 0.3.0 tarball, by sha256 |
| Crates | `vchord_bm25/Cargo.lock`, built `--locked`; 0.3.0 ships no lockfile |
| The fixes | `vchord_bm25/*.patch`, applied with `set -e` and `--fuzz=0` so a patch that no longer fits fails the build |

The PostgreSQL headers are those of exactly the base's server version. PGDG's
main repository carries the last few releases of a major, and its archive
carries all of them, so the pin keeps resolving after main drops this release;
a source that cannot be read fails the build. bindgen uses the LLVM those
headers depend on. The SQL install and upgrade scripts and the control file
come from the source tree and are byte-identical to the upstream release's.
The Debian toolchain (gcc, libc, LLVM 19) follows bookworm's point releases, so
a later build can differ in bytes, though not in source.

The base references are multi-architecture indexes, so
`--platform linux/amd64,linux/arm64` works, but compiling a platform under
emulation is slow. To move the pgvector pin, follow the procedure in
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
Tests must run against a server started the same way, because a preload hides
exactly this. CI builds this image and starts it with a plain `postgres`
command.

A database that already serves `posting` stays on it under `auto`, even on this
image. Moving it is `scripts/backfill_bm25_vector.py`, and then naming `vchord`
in the setting: select it only after `--index` has built the index. Until then
the backend refuses the shape rather than building the index itself: at startup
it would build it inside its schema transaction, blocking writes for the build,
over a column the backfill had not finished. A new, empty database gets the
index at startup, built empty and filled by inserts.

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
the filter keeps the negatives. That holds because this extension's IDF,
`ln((N + 1) / (df + 0.5))`, stays positive while `df` does not exceed `N` —
measured at `df = N`, all fifty matching documents still scored below zero.
`0005` (below) holds a `df` that runs ahead of `N` at `N`, which happens until
an interrupted VACUUM's recount is finished, and computes the ratio in f64, so
the IDF stays positive at every `df` and every `N`.

Classic BM25 IDF, `log((N-df+0.5)/(df+0.5))`, turns negative once a term is in
more than half the corpus. An extension version that switched to it would make
the filter discard real matches for common terms, silently and only for the
terms most queries contain.

So when moving this pin, check that a document containing a query term still
scores below zero at high document frequency before accepting the bump. Then
run `test_vchord_index_build_postgres.py` and `test_vchord_vacuum_postgres.py`
against the new version with and without the patches; see [Tests](#tests).

## Index builds (akb#679)

An index built over existing rows goes through the extension's build path:
`CREATE INDEX`, `CREATE INDEX CONCURRENTLY`, `REINDEX`, every `pg_restore`, and the
backfill runbook's `--index`. Upstream 0.3.0 flushed a full 128-posting block
before it recorded the block's best posting. A block whose best posting was its
last one therefore kept the best recorded for an earlier block, or 0 if none
had been. Ties make that common. A bounded scan skips a block by that summary,
so a page, full or short, could miss better matches. Rows that arrive by insert
reach sealed blocks through the seal path, which records the best first and
never had this.

`vchord_bm25/0001-record-block-max-before-flush.patch` records the best posting
first. It changes no on-disk format and no SQL. Measured against the same SQL:

| Build | 300 identical rows, `REINDEX`, bounded top-90 | 20,000-document corpus, `CREATE INDEX`: terms with a worse bounded top-90 |
| --- | --- | --- |
| upstream 0.3.0 | 44 | 14 of 1,227 |
| this image | 90 | 0 of 1,227 |

On a 2.1M-chunk index built with `CREATE INDEX CONCURRENTLY` by the upstream
binary, 7 of 159 sampled terms had a worse bounded top-90.

On this image, rebuilding the index is safe. An index that the upstream binary
built over existing rows keeps its summaries until it is rebuilt. After moving
such an installation to this image, run:

```sql
REINDEX INDEX CONCURRENTLY vector_index.idx_vi_chunks_bm25;
```

Use the installation's `vector_store_schema` in place of `vector_index`.

## Document length statistics (akb#684)

The metapage keeps the sum of document lengths, and BM25 takes the average
document length from it. The index stores a one-byte length code per document,
which keeps lengths up to 40 and rounds longer ones down to their bucket's
start: 139 is stored as 136, 300 as 280.

Upstream 0.3.0 added exact lengths on insert and build, and VACUUM subtracted
the code itself: 62 for a document of length 139. Every update and delete
therefore left length behind (1 at 41, 14 at 64, 77 at 139), and the average
grew until the index was rebuilt. Subtracting the code's length instead would
still leave up to a bucket's width per replaced document, 3 to 4% of the
average for each replacement of the whole corpus.

`vchord_bm25/0002-count-stored-document-lengths.patch` makes the build, the
insert and VACUUM all count the stored length. What VACUUM subtracts is exactly
what was added, however often a document is replaced. The average is now the
mean of the lengths BM25 scores documents with, a little below the mean of
exact lengths. An index the upstream binary built keeps its sum as it was,
neither growing further nor healing, until a `REINDEX` moves it down once: by
that difference, plus whatever the upstream VACUUM had added.

## VACUUM (akb#687)

VACUUM reaches the index in two steps. The bulk delete marks the documents whose
rows it removes, and the cleanup recounts how many live documents hold each
term. A search reads the index's metapage for its whole scan and an insert
writes it, and upstream 0.3.0 held the metapage through both steps.

- **The bulk delete** held it for a pass over every document id the index has
  assigned, so a search that started meanwhile waited for the whole pass: 3 s
  for a million documents. The pass logged each delete mark as it went and the
  counts once, at the end. A backend killed mid-pass kept its marks and lost the
  counts, and the next VACUUM skipped the marked documents, so the counts stayed
  too high until a rebuild.
- **The cleanup** visited every term id below the largest one indexed, not just
  the terms that exist, and stored each count with its own page write and WAL
  record. It could not be cancelled. At 728,984,818 term ids it stalled searches
  for 44 minutes and inserts for 48, wrote 35.4 GiB of WAL, and allocated
  2.72 GiB outside `maintenance_work_mem`.

`vchord_bm25/0003-mark-deletions-a-bitmap-page-at-a-time.patch` works through one
delete bitmap page (65,280 document ids) at a time. It finds the page's dead
documents with no lock held, then sets their marks and takes them off the counts
under one WAL record. A crash keeps both or neither, and a VACUUM that runs
again after a crash or a cancel takes no document off twice.

`vchord_bm25/0004-recount-term-statistics-a-page-at-a-time.patch` recounts one
term statistic page (2,040 term ids) at a time. It holds the metapage only while
it reads a page's stored counts, writes a page only if a count on it changed,
and holds no buffer lock from one page to the next. Searches and inserts go on,
and a cancel stops it at the next page. It holds the seal lock throughout, so an
insert that would seal the growing segment skips sealing until the recount ends.

A recount that a cancel or a crash cuts short leaves the statistics it has not
reached still counting the deleted documents. Upstream recounted only when the
same VACUUM removed documents, so a VACUUM that found every dead document
already marked, or had none, never repaired them. The bulk delete now records,
in the WAL record that takes documents off the counts, that a recount is owed,
and only a recount that reaches its last page clears it. The next VACUUM that
cleans up the index, autovacuum included, finishes the recount whatever it
removes; one run with `INDEX_CLEANUP OFF`, or stopped by the wraparound
failsafe, leaves it for the one after, and ANALYZE never runs it. Measured on 100,000 documents with 40,000 deleted, the VACUUM
backend killed during the recount and then a VACUUM with nothing to remove:
42,001 statistics stayed too high under the upstream condition, none with this.

`vchord_bm25/0005-keep-idf-positive-when-a-statistic-runs-ahead.patch` covers
the time until then. A term whose statistic still counts deleted documents can
show more documents than remain, and its IDF went negative: a search for that
term alone found nothing, 0 rows where 10 were due in the same shape. The IDF
now holds the statistic at the document count, and takes its ratio in f64. On
a settled index nothing moves beyond f32 rounding: 58 queries kept their top 20
in the same order, and no score in them moved by more than 1.4e-7 of itself. A
term in nearly every document moves by up to about 0.2%, the same for every
document that holds it, so no order changes. The seal path uses the same IDF
for block summaries, and a seal while a statistic ran ahead recorded summaries
that bounded scans then skipped for good: the best document was missing from a
bounded top 10 even after the recount. 0005 prevents that.

| | 0.3.0 with 0001 and 0002 | This image |
| --- | --- | --- |
| A search started during the bulk delete (1M documents, 800,000 deleted) | 2.86 s | 0.001 s |
| `doc_cnt` after a kill mid-pass and the next VACUUM (200,000 true) | 419,689 | 200,000 |
| The cleanup at 728,984,818 term ids | 2,906 s | 2.7 s |
| The longest search during that cleanup | 2,633 s | 0.002 s |
| Its WAL and the backend's memory | 35.4 GiB, 2.72 GiB | 0.3 MiB, 3.4 MiB |
| A cancel during the cleanup (3M term ids) takes effect after | 10.8 s | 0.001 s |

Its reads still follow the largest term id. The cleanup reads the whole term
information array, 4 bytes per id: 2.7 s at 729M ids, 17.5 s under autovacuum's
default cost delay. A statistic page it writes carries a full-page image after
each checkpoint, so over a sparse id space, where most terms sit alone on their
page, one VACUUM of 40,000 documents wrote 2.75 GiB.

On this image, the fix itself needs no rebuild. Two kinds of damage an older
build left keep until `REINDEX INDEX CONCURRENTLY`: counts a crash damaged
under the upstream binary, and block summaries sealed while a term's statistic
ran ahead of the document count.

## What a bounded scan can still differ on

A block summary is the block's best posting, chosen with the average document
length of the moment it is written. BM25 at query time uses the current average,
so a large change in that average can make an old summary underestimate its
block. With the length statistics fixed, the average moves only as the corpus
itself changes. `REINDEX INDEX CONCURRENTLY` rewrites every summary with the
current average.

## Known limits in the extension

- **Term ids at or above 2^30.** The index addresses its per-term arrays with
  32-bit byte offsets at 4 bytes per id, and the release build does not check
  the multiplication. An id of 1,073,741,824 or more therefore lands on the id
  2^30 below it, whichever way the document arrived:
  - built over existing rows (`CREATE INDEX`, `REINDEX`, every `pg_restore`), a
    search for the high id reads the lower id's entries, and the document that
    holds the high id cannot be found through it;
  - inserted and then sealed, its posting joins the lower id's list and its
    document the lower id's count, so searches for the lower term rank a
    document that does not hold it, and both terms' IDF is wrong.
  AKB draws term ids from `bm25_term_id_seq`, which has to stay below 2^30.
- **The recount blocks sealing while it runs.** It holds the seal lock from its
  first page to its last, so the growing segment, which every search reads in
  full, keeps growing until it ends.
- **Sealing stalls search.** Inserts collect in a growing segment that every
  search reads. The insert that seals it holds searches for the duration; one
  seal of 19,691 rows took 24.7 s.

## Tests

`test_vchord_index_build_postgres.py` holds the fixes for builds and length
statistics:

- blocks whose last posting is the best, built by `REINDEX`, compared with the
  exact scan;
- the same rows sealed by inserts, the path that never had the defect, compared
  with the exact scan;
- the metapage's length sum through a build, inserts and a VACUUM, which must
  take back exactly what was added, at lengths that are bucket starts and at
  lengths that are not;
- a 20,000-document corpus built over existing rows, compared with the exact
  scan one term at a time and in queries of two or three terms.

`test_vchord_vacuum_postgres.py` holds the VACUUM fixes:

- a cancel during the bulk delete stops it within a second, and the next VACUUM
  leaves the counts exact;
- no search waits for the bulk delete;
- the cleanup over a term id space of 20,000,001 finishes in seconds, no search
  waits for it, and every term's statistic matches the rows;
- a VACUUM cancelled after its bulk delete, then one cancelled during its
  recount, then one with nothing to remove: meanwhile a search for a term in
  every document still finds its rows, and the last VACUUM leaves every
  statistic exact.

Upstream 0.3.0 fails all but the sealed case of the first file. Without 0003 and
0004, the first three tests of the second fail; without the owed recount in
0004 or without 0005, the fourth does. Drop a patch only when a new upstream pin
passes its test without it.

## Licensing

`vchord_bm25` is published under the GNU Affero General Public License v3 or
the Elastic License v2, at the recipient's option. It is a separate program
that runs inside the PostgreSQL server and is reached over the PostgreSQL wire
protocol, so it does not change the licensing of AKB itself.

The patches in `vchord_bm25/` are offered under the extension's own terms.
Building and running this image is use. **Distributing** the built image is
distribution of the modified extension and carries the corresponding
obligation: an offer of its source, which is the upstream tarball plus those
patches.

## Bounded search and exact completion

`bm25_catalog.bm25_limit` is the size of the extension's internal top-k. A search
asks for one finite page first. When that page comes back full, it is the answer.
When it comes back short, the same query runs again with `-1` and that result is
the answer (akb#673).

A short finite page is not proof that nothing else matches:

- Growing-segment rows are scored without the query's filter, and they take top-k
  slots. So can rows the current snapshot cannot see.

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

Run `test_vchord_candidate_budget_postgres.py` against the pinned extension. It
covers:

- finite pages and their exact completion;
- scoped materialised ranking;
- every exact route, and a rebuilt index answering a bounded page in full;
- connection recovery.

Hybrid and service regressions cover:

- short sparse pages completed beside the dense leg, in both completion orders;
- ACL filters;
- degradation accounting for drivers that do return partial results.
The test DSN must identify an isolated PostgreSQL instance;
the tests create disposable databases.
