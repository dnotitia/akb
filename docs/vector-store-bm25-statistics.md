# BM25 statistics and VChord-only deployments

AKB keeps Kiwi term IDs in `bm25_vocab` for every sparse driver. The vocabulary
must never be cleared while vectors reference it. The IDs are dense, and only
`scripts/compact_bm25_term_ids.py` reassigns them
([Renumbering term IDs](#renumbering-term-ids)).
The vocabulary registry and external corpus statistics have different lifetimes.

| Consumer | Document/query weights | External `bm25_stats` and `bm25_vocab.df` |
| --- | --- | --- |
| pgvector posting or arrays | Saturated TF / IDF | Required |
| Qdrant or Seahorse Cloud | Saturated TF / IDF | Required |
| SeahorseDB HTTP or gRPC | Raw TF / 1 | Required for search-time metadata |
| pgvector VChord | Raw TF / 1 | Index owns N, avgdl and df; external stats unnecessary for this consumer |

`bm25_external_stats_mode: auto` is the default. It keeps external statistics
wherever something reads them: every consumer above except VChord, and under
VChord a `posting` table kept as the way back. A VChord database with no
`posting` table — every new VChord installation — has no reader, so neither the
startup nor the periodic recompute runs. Startup learns whether the table
exists when it settles the sparse shape (see `vector_store_sparse_shape: auto`
in `config/app.yaml.example`); until it has looked, the statistics are kept.
`required` keeps them regardless, for mixed deployments this process cannot
see. The refresher does not populate `sparse_bm25`, update the VChord index, or
restore posting vectors.

`bm25_external_stats_mode: vchord_only_verified` skips startup and periodic
external-statistics recomputation. Configuration rejects this mode unless the
local driver/shape is `pgvector`/`vchord`. The mode is an operator attestation,
not automatic discovery of other processes: verify all API and worker generations,
actual index validity, isolated filtered search and update/delete behavior, and
absence of external-statistics consumers before selecting it. No active recompute
is cancelled by this setting. Manual maintenance remains explicit. Encoding and
backfill continue to register vocabulary IDs when the refresher is disabled.

The `/health` BM25 snapshot includes `external_stats` with `mode`, `required`,
`consumers`, and `refresher_running_in_this_process`. API-only processes can report
required statistics with no local runner. Existing `last_recomputed_at` records
the last successful shared publication; `recompute_in_flight` is resumable scan
progress and `recompute_active` indicates a held recompute lock. A retained run
marker alone does not prove a worker is running. These observations remain
available when this process disables refresh.

## Returning to posting

Keeping external statistics fresh is necessary but insufficient for rollback.
VChord uses raw TF whereas posting/arrays use precomputed saturated weights;
VChord writes do not keep the posting representation current. Record the first
VChord write boundary, and retain the source corpus and vocabulary. Before serving
posting again, restore `required`, finish any necessary statistics recomputation,
re-encode/reindex affected documents into the posting representation, drain writes
and deletes, and validate filtered search. Do not merely switch the shape on old
posting rows or copy raw VChord weights into them. The acceptable statistics lag
and rollback window must be set for each deployment; this mode grants neither.

Deploying this policy requires a backend image containing the configuration,
lifecycle and encoder changes on every participating API/worker process. Changing
a configuration file cannot add the new policy to an older image. Index extension
and database image requirements are a separate deployment decision.

## Renumbering term IDs

Term IDs come from `bm25_term_id_seq`. Earlier versions of the encoder and the
statistics recompute drew IDs they then discarded, so a long-lived installation
can hold far more ID space than terms. The `vchord` index sizes its per-term
arrays by the largest ID, so that space costs index size, build time and VACUUM
time, and IDs of 2^30 or more alias other terms inside the index.
`scripts/compact_bm25_term_ids.py` gives every term the rank of its ID. BM25
scores do not depend on IDs, so rankings stay the same, and the command checks
that before it commits.

```bash
python -m scripts.compact_bm25_term_ids            # dry run: what it would do, and what forbids it
python -m scripts.compact_bm25_term_ids --apply    # renumber, verify, commit (or roll back)
python -m scripts.compact_bm25_term_ids --revert   # put the recorded numbering back
```

Exit codes: 0 done (or nothing to do), 1 verification or the rewrite failed and
was rolled back, 2 refused before anything changed.

Before `--apply`:

1. **Every backend process runs a version with the vocabulary epoch.**
   Migration 113 adds it. Indexing and the `sparse_bm25` backfill store IDs
   only if they were read under the current epoch; an older process does not
   check, and could store IDs from before the renumbering.
2. **Ingestion is paused**, and **`VACUUM <vector_store_schema>.chunks`** has
   run since. The command compares rankings from the index as it stands with
   those from the rebuilt one. That comparison needs index statistics that
   count only live rows, and the command refuses an index that still counts
   rows VACUUM has not removed.
3. **The dry run is clean.** It refuses:
   - Qdrant and SeahorseDB drivers;
   - a vector index in a separate database (`vector_store_dsn`);
   - the `arrays` shape;
   - IDs left in the shape that is not active, such as a `posting` table kept
     under `vchord` as the way back. Empty or drop that table first.

What `--apply` does, in one transaction:

- It rewrites `bm25_vocab` and the active shape's IDs (`vchord`: `sparse_bm25`
  and its index; `posting`: `posting.term_id`), and restarts
  `bm25_term_id_seq` after the last ID.
- It advances `bm25_vocab_epoch`, and records the mapping in
  `bm25_term_id_remap` for `--revert`.
- The vector table is rewritten, which rebuilds **every** index on it, a dense
  HNSW index included. On a large corpus that rebuild sets the length of the
  window. The dry run lists the indexes and their sizes. Give the rebuild
  memory with `--maintenance-work-mem` (default `1GB`); a parallel build takes
  it from `/dev/shm`.
- The vector table and `bm25_vocab` stay exclusively locked until the commit.
  Searches wait. Indexing hands its batches back and resumes after the commit
  without spending retries.
- A lock that stays busy longer than `--lock-timeout` (default 10 s) refuses
  the run instead of queueing everything behind it.

Keep `bm25_term_id_remap` and `bm25_term_id_remap_run` while a revert may be
wanted, and drop them afterwards. A later `--apply` replaces them.
