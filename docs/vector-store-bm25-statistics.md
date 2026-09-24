# BM25 statistics and VChord-only deployments

AKB keeps stable Kiwi term IDs in `bm25_vocab` for every sparse driver. IDs must
never be reassigned or the vocabulary cleared while vectors reference them.
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
