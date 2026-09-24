---
status: accepted
stage: applied
created: 2026-09-24
updated: 2026-09-24
---

# VChord as the default sparse shape

## Decision

```yaml
vector_store_sparse_shape: auto      # new default
bm25_external_stats_mode: auto       # new default
```

`auto` is decided once per database, at startup, and recorded in that database.
A new database gets `vchord` where the PostgreSQL server can give AKB's role the
`vchord_bm25` extension, and `posting` where it cannot. A database that already
serves a shape keeps it. Naming a shape in the configuration still overrides
everything.

## Why

The `vchord` shape hands the terms to a block-max BM25 index inside PostgreSQL.
On a 2.1M-chunk corpus, the same 160 search pairs, asked at the same time of
the two shapes, measured:

| | `posting` | `vchord` |
| --- | --- | --- |
| sparse leg p50 / p90 / max | 0.13 / 1.31 / 8.88 s | 0.09 / 0.76 / 2.50 s |
| whole response p90 / max | 2.86 / 13.7 s | 2.51 / 6.3 s |
| top result identical | — | 154 of 156 pairs |

The gain is in the terms `posting` answers slowest, because `posting` sums every
posting of every query term; the worst measured term took 10–18 s there and
under 1 s here. The cost is on the other side of the same property: a term that
is common across the corpus but asked inside a small scope reads the global
postings under `vchord`, and there `posting` is faster by a median of 0.04 s and
at most 1.35 s in the same measurement. `vchord` also keeps no side table, whose
size here was about twice the BM25 index and column, and has no corpus-wide
statistics recompute to run.

## How the database's shape is decided

In order, by `backend/app/services/vector_store/sparse_shape_state.py`:

1. **The recorded shape.** Every successful schema setup records the shape it
   set up in `<vector_store_schema>.install_state`, including a configured one,
   so a later `auto` follows the operator's last explicit choice.
2. **A `posting` table → `posting`.** An installation that has served `posting`
   moves only through `scripts/backfill_bm25_vector.py` and an explicit
   `vchord` setting. The BM25 index exists before that switch, so it is not
   evidence that the switch happened.
3. **The BM25 index → `vchord`; the `arrays` columns → `arrays`.**
4. **Populated chunks with none of those signatures → refused by name.**
5. **A new database → `vchord`** when the extension is created and usable by
   AKB's role, or available and the role is a superuser; **`posting`**
   otherwise, with a note that says how to get `vchord`. An extension that
   somebody created but that the role cannot use is refused with the GRANT
   that fixes it.

API and worker processes decide the same way, from the same database, before
the vector store is built. `/health` reports `vector_store.sparse_shape`.

## What this does not change

- **Existing installations.** The previous code default was `posting`, and most
  configurations never named a shape. Deciding `vchord` for such a database
  would meet the populated-table guard and return empty search, dense results
  included, while readiness stayed green. The order above is what prevents
  that.
- **Where the extension comes from.** AKB publishes no PostgreSQL image. Its
  install paths build `deploy/postgres/Dockerfile`, the pinned pgvector image
  plus the extension, where they run it:
  - Compose, `deploy/k8s/deploy.sh` and the CI runtime e2e build it themselves.
  - Helm and the Native Kubernetes overlay take it through the image value that
    their install commands set.
  - The all-in-one leaves it out. It is the one image AKB publishes, and
    publishing the extension is distribution of it (AGPLv3 or ELv2), a decision
    this record does not make. A new all-in-one database gets `posting`.

  Building and running it is use, not distribution. The chart and the
  Kubernetes base manifest keep the stock pgvector image as their literal
  default. Upgrading a release that never set it therefore cannot point a
  running database at an image nobody has built.

## Consequences

- **The extension needs a superuser.** Where AKB's role is one, AKB creates it.
  Otherwise a superuser runs `CREATE EXTENSION vchord_bm25` and
  `GRANT USAGE ON SCHEMA bm25_catalog TO <role>` before AKB first starts.
- **External statistics under `auto`.** A `vchord` database with no `posting`
  table has no reader for them, so the recompute does not run. With a `posting`
  table, they and the table are kept current as under `required`.
- **Startup reads the vector database** before building the store. Under `auto`,
  an unreachable separate `vector_store_dsn` now fails startup.

## Known limits

- **Index builds.** `vchord_bm25` 0.3.0 saves some block summaries too early
  when it builds an index with `CREATE INDEX` or `REINDEX`, and a bounded scan
  then skips those blocks. A new database builds the index empty and fills it
  by insert, which does not have the defect; a restore that rebuilds indexes,
  a `REINDEX`, and the backfill runbook's `--index` do. A page shortened this
  way is completed exactly; a full page can still miss better matches.
- **Sealing.** Inserts accumulate in the index's growing segment until it
  reaches `bm25_catalog.segment_growing_max_page_size` pages, and every search
  reads that segment in full. The insert that seals it moves the segment into
  the sealed postings; in one measurement, sealing 19,691 rows took 24.7 s, and
  searches waited for it.

## Rollback

Set `vector_store_sparse_shape: posting` for a database that has a `posting`
table. For a database that never had one, `posting` starts empty and has to be
re-indexed from the source chunks.
