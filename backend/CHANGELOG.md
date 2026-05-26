# AKB Backend — Changelog

The AKB backend ships as a Docker image and as the HTTP layer behind
the `akb-mcp` stdio proxy. This changelog tracks the backend
specifically; the proxy has its own log in
`packages/akb-mcp-client/CHANGELOG.md` and a separate version stream.

## 0.3.0 — 2026-05-27

**BREAKING** — a coordinated contract pass that takes the AKB API
from "mostly consistent with quiet gaps" to "every surface tells the
same story":

1. `akb_browse.depth` redesigned as true tree-depth (from misnomer).
2. **URI scheme made location-aware** — every URI carries an
   optional `/coll/<path>` segment that names its containing
   collection, so siblings/parents are discoverable by walking up
   the URI without an extra lookup.
3. `akb_graph.depth` renamed to `hops` so it does not collide with
   the new browse `depth`.
4. List-style tools (`akb_recall`, `akb_activity`) report what they
   returned, the corpus total, and whether more exists, instead of
   leaving callers to guess.
5. `akb_search` / `akb_grep` hits now carry an explicit
   `collection` field so clients group/filter without URI parsing.
6. `akb_drill_down` surfaces sub-section hints on a successful
   match so a drilling agent has its next step in hand.

### Location-aware URI scheme — every resource self-describes its place

Pre-0.3.0 the URI scheme placed the collection inside the doc path
(`akb://V/doc/specs/api.md`) but emitted table and file URIs with
no collection prefix at all (`akb://V/table/expenses`,
`akb://V/file/<uuid>`). 0.3.0 unifies the scheme:

    akb://{vault}                                       vault root
    akb://{vault}/coll/{coll_path}                      collection
    akb://{vault}/doc/{filename}                        root doc
    akb://{vault}/coll/{coll_path}/doc/{filename}       doc in coll
    akb://{vault}/table/{name}                          root table
    akb://{vault}/coll/{coll_path}/table/{name}         table in coll
    akb://{vault}/file/{uuid}                           root file
    akb://{vault}/coll/{coll_path}/file/{uuid}          file in coll

Two new helpers exist alongside the typed ones:

  * `vault_uri(vault)` — addresses the vault root, useful as the
    starting point of a drill-down chain.
  * `coll_uri(vault, path)` — collections are first-class URI
    citizens now (previously they were the only navigation type
    without a canonical handle).

`table_uri(vault, name, collection=None)` and
`file_uri(vault, file_id, collection=None)` gained an optional
`collection` parameter — pass it when building from a row that has
the collection FK already JOINed. The `doc_uri(vault, path)`
helper splits the doc's full path at the LAST slash and emits the
new canonical form automatically — call sites that already pass
`documents.path` need no change.

`akb_browse` accepts a `uri` argument that takes precedence over
the legacy `vault` + `collection` pair. Drill-down chains are now
a paste-back loop: every browse item carries `uri`, paste it back
into `akb_browse(uri=...)` to drill in. Doc / table / file URIs
passed to browse are rejected with a hint pointing at the
appropriate leaf tool.

**Migration 026** rewrites every persisted URI in `edges`,
`publications`, and `events` to the new canonical form. Doc
rewrites run as a pure SQL `regexp_replace`; table/file rewrites
JOIN through `vault_tables` / `vault_files` to recover the
collection. Frontmatter URIs inside markdown bodies are **not**
rewritten — old URIs there will not parse against the new scheme
and edge extraction logs a warning. An optional batch-rewrite
tool can be run later if needed.

`make_uri(vault, type, identifier)` (the bottom-level builder that
produced the legacy shape) is gone. Every emit site goes through
the type-specific helpers so the location prefix is built the
same way everywhere — a hand-built `f"akb://..."` string outside
`uri_service.py` is now an audit finding, not a routine pattern.

### `akb_browse` — true tree-depth

### `akb_browse` — true tree-depth

`depth` was historically a misnomer: `1 = collections only`,
`2 = + documents` (always-all tables and files regardless). Issues
#81 / #82 fixed in 0.2.5 patched the document asymmetry, but the
underlying mental model stayed broken — depth wasn't depth, just
an "include docs" toggle, and tables/files leaked from sub-collections
into every top-level browse.

0.3.0 redefines `depth` as **tree-depth from the browse root** —
the `tree -L N` convention:

- `depth=0` — direct children of the browse root only, no descent
  into any collection
- `depth=N` (N ≥ 1) — descend N collection levels
- `depth=-1` — unbounded; the entire subtree of the browse root

Collection rows are always emitted as navigation aids regardless of
depth (the response would be useless without them). `doc` / `table` /
`file` rows are the ones gated. When `collection` is supplied, the
browse root is that collection, and depth is counted from inside it.

Schema bounds: `minimum: -1`, no maximum. Existing default
`depth=1` is preserved but its meaning shifts (root + 1 level
of collection contents, rather than the old misnomer).

Migration for the AKB frontend: `use-vault-tree.ts` now calls
`browseVault(vault, undefined, -1)` (unbounded) because the
client-side tree builder always wants the entire vault. External
MCP clients passing `depth=2` and expecting "everything" must
switch to `depth=-1`.

No data migration is required — depth is computed at query time
via PostgreSQL slash-counting (`length - length(replace(path, '/', ''))`),
the existing `collection_id` FK + path conventions cover every
case.

### `akb_recall` — corpus total + truncated flag

Pre-0.3.0 returned `list[dict]` straight out, with the MCP handler
synthesising `{memories, total}` where `total = len(memories)` —
i.e. it lied when `LIMIT` cut anything off. Callers had no way to
know more memories existed.

0.3.0 returns `{memories, returned, total, truncated}`:

- `total` is the **corpus count** matching the filter (one extra
  `COUNT(*)` query, cheap)
- `returned` is `len(memories)`
- `truncated` is `True` when `total > returned`

Callers that previously consumed the bare list now read `.memories`.
The REST `/api/v1/memory` mirror was updated symmetrically.

### `akb_activity` — truncated flag, drop the misleading `total`

Pre-0.3.0 returned `{vault, total: len(entries), activity: entries}`
where `entries` was already capped by `git log --max-count=limit` —
so `total` was the post-limit slice, never the corpus.

0.3.0 returns `{vault, activity, returned, truncated}`:

- `total` removed (it was wrong)
- `truncated` computed via peek-ahead (`git log --max-count=limit+1`)
- `returned` is `len(activity)` after any author post-filter

The `total` removal is the visible BREAKING change. `test_mcp_e2e.sh`
already reads the new `returned` field.

### `akb_graph` — `depth` → `hops`

Graph traversal radius is now spelled `hops` instead of `depth` to
disambiguate from `akb_browse.depth` (collection-tree depth). The
two parameters meant different things — one counts edges
followed, the other counts folder levels — and sharing a name
forced callers to memorize the difference. REST `?depth=` is
renamed to `?hops=` on `/graph` as well. No alias; explicit
rename so half-migrated callers fail loudly instead of using the
wrong radius.

### `akb_search` / `akb_grep` — `collection` field on hits

Hit envelopes now include `collection` (the containing-collection
path, null at vault root). Sourced from a `LEFT JOIN collections`
in the hydrate query — same row already gives us the URI, so the
cost is one extra column per type. Clients that grouped or
filtered hits by collection used to parse the URI themselves;
now they read the field directly.

### `akb_drill_down` — sub-section navigation hints

A successful match returns `sub_sections` — the immediate
children of the matched heading that actually appear in the
document. A `hint` field points at the next concrete drill step
(`section='Setup/Install'` or `mode='outline'`), so an agent that
just matched "Setup" gets one-step suggestions instead of having
to re-fetch the outline.

### Defense-in-depth — vault-template seed

`document_service.create_vault` (template seed path) now skips
`coll_repo.get_or_create` when the template's `collections[i].path`
is empty, with a warning log. Every shipped template already has
non-empty paths; the guard exists so a future template typo cannot
quietly resurrect the `path=''` phantom row that issues #81/#82
were about.

### Migration notes

- AKB frontend: included in this PR (`depth=-1`, memory type widened).
- `seahorse-mcp-agent-server` and any external MCP consumer: needs a
  coordinated update before deployment to environments where it runs
  (e.g. KISA prod). Hold off on AWS-prod rollout until the demo-agent
  team has aligned. On-prem rollout is safe in isolation.

## 0.2.5 — 2026-05-24

`akb_search` response now carries a `truncated` boolean and an
optional `hint`, mirroring the contract that `akb_grep` got in 0.2.4.
The motivation: `total_matches` in hybrid search was always the
size of the source-deduped *prefetch pool*, not a corpus-wide hit
count — vector ANN is fundamentally top-K. When the pool fills to
the `rerank_prefetch` ceiling (default 30), the corpus may hold
many more hits than ever reach the response, but the existing
contract (`total_matches >= returned`) made it look like the
caller had seen the truth.

Now:

- `truncated=true` iff `total_matches >= target_unique` (the
  prefetch ceiling computed from `rerank_prefetch` /
  `search_prefetch` / `limit`). Treats "pool filled" as "there may
  be more in the corpus."
- `hint` set when `truncated=true`, recommending `akb_grep` with
  `count_only=true` for an exact literal-substring count and noting
  that semantic queries can't be exhaustively enumerated.
- Model docstring on `SearchResponse` rewritten to call out the
  pool-depth semantics explicitly.

The `total_matches` value itself is unchanged — same number,
honest framing. `total` (deprecated alias of `returned`) stays.

## 0.2.4 — 2026-05-24

`akb_grep` default response now reports the true corpus-wide totals
even when the line snippets get truncated under `limit`. The old
shape aggregated `total_docs` / `total_matches` over the post-limit
slice — agents that read those as "how many hits exist in the corpus"
got false-low counts and made early-termination mistakes. Symptom:
"the pattern only appears in N docs" when the actual count was much
higher.

New default-mode fields:

- `returned_docs` / `returned_matches` — what fit under `limit`
- `total_docs` / `total_matches` — full ILIKE scan (no cap)
- `truncated` (bool) + `hint` — set when there's more than `limit`
  could hold, recommending `count_only=true` or
  `files_with_matches=true` instead of bumping `limit`

This aligns `akb_grep` with the `returned` vs `total_matches`
contract that `akb_search` already follows (issue #35). The
`count_only=true` and `files_with_matches=true` response shapes are
unchanged — they always reported full-scan counts and are now also
the official escape hatch when the default shape reports
`truncated=true`.

One small correctness side-effect: chunk hits that produced no
line-level matches after `strip_chunk_metadata_header` (header /
summary metadata artifacts riding along with every chunk) no longer
appear as zero-match docs in the response. They were never real
grep hits.

## 0.2.3 — 2026-05-23

Agent-facing polish on the search tools introduced in 0.2.1 / 0.2.2.
Three small changes driven by the agentic-bench v7 review of the tool
surface; all backward-compatible.

`akb_drill_down` gets a `mode` argument. Previously the only way to
get a document's outline was to trigger the empty-match fallback —
agents that just wanted the structure had to ask for sections,
discard the bodies, and parse heading paths out. `mode='outline'`
makes that a first-class call (no body fetch, cheap), with the same
`truncated` / `hint` metadata as the empty-match path. `mode='sections'`
is the default and preserves the old behaviour.

`akb_list_vaults` and `akb_browse` rename their substring-filter
argument from `query` to `filter`. The old `query` name collided with
`akb_search.query` (a natural-language retrieval string), and an
agent picking between the tools shouldn't have to remember that the
same parameter name means two different things. The old `query`
parameter remains accepted as an alias and is marked DEPRECATED in
the schema; it will be removed in 0.3.0.

Response shape standardisation. List-style responses now share four
common fields where applicable: `total` (matches before pagination),
`returned` (items in this response), optional `truncated` (true when
output was capped beyond the agent's control), optional `hint` (a
one-line guidance string for retry / pagination). Existing fields
remain in place for backward compatibility.

## 0.2.2 — 2026-05-23

Two more MCP tool overhauls along the same axis as 0.2.1's
`akb_list_vaults` slim-down. After fixing vault discovery in v7 of
the agentic-bench, the next failure modes the bench surfaced were
the *next* steps in the routing chain — `akb_browse` payloads
truncating before the agent could see the target collection, and
`akb_drill_down` returning nothing when the heading guess was off
by a word.

`akb_browse` — slim by default. The per-item `summary` field is
multi-paragraph English text and was 80-90 % of the response bytes
in vaults with 70+ collections (`legalize-kr-external-ro` at the
6 KB cap). Now dropped unless `include_summary=true`. Adds the
same `query` / `limit` / `offset` filters as list_vaults so an
agent searching for a specific collection (e.g. `query='민법'`)
gets one row, not a truncated list. Response gains `total` /
`returned`.

`akb_drill_down` — substring grep inside sections + outline
fallback. The old behaviour matched `section` against heading
paths only, so queries like `section='부칙'` either fetched the
entire 부칙 (often > 6 KB and truncated) or returned nothing when
the agent guessed the wrong heading. Two additions:

- `pattern` arg: case-insensitive substring filter on section
  bodies. Lets the agent grep *inside* a large section without
  refetching the whole document — useful for `'부칙'` + a
  specific 호수, or for finding a cross-reference like
  `'「개인정보 보호법」 제23조'` without scanning every section.
- When the (section, pattern) query returns no sections, the
  response now carries an `outline` field listing the document's
  available headings (capped at 200) plus a `hint` to retry or
  call `akb_get`. Replaces the silent empty-result trial-and-
  error loop observed in agentic-bench v7.

Schemas extended additively; existing callers keep working.

## 0.2.1 — 2026-05-23

`akb_list_vaults` MCP tool overhaul. The previous handler returned
every accessible vault with full metadata (id, role, created_at,
status, public_access), which inflated to ~80 bytes per vault. In
tenants with 70+ vaults the payload hit ~6 KB — exactly the size at
which the stdio proxy / agent client truncated. Vaults whose name
sorted late in the alphabet were silently invisible to the agent,
which then either hallucinated answers or claimed the data wasn't
in AKB at all. (Observed under
[agentic-bench v5–v6](../eval/agentic-bench/): the `A3_tree` arm
hovered around 2-4% PASS purely because `legalize-kr-external-ro`
was being trimmed off the end of the list.)

The new handler returns `{name, description}` only by default, plus
optional `query` / `limit` / `offset` / `include_archived` filters
and a `total` / `returned` count. Same MCP tool name, additive
schema, so existing callers continue to work; the new args are
opt-in. REST callers that need the full rows still use
`GET /api/v1/vaults`.

## 0.2.0 — 2026-05-21

First public minor release. The headline is the **security model
switch**: vault isolation in `akb_sql` is now enforced by
PostgreSQL ACL, not by application-side identifier filters. The
boundary lives in the platform's trusted base, not in a regex the
maintainer has to keep cat-and-mouse with new PG catalog names.

This is an additive change. Existing deployments upgrade in place;
no data migration, no breaking contract changes.

### Highlights

- **PG-native vault isolation for `akb_sql`.** Each user gets an
  `akb_user_<uid>` PG role, each vault gets three group roles
  (`akb_vault_<vid>_{reader,writer,admin}`), and `vault_access` rows
  map 1:1 to role memberships. The akb_sql executor opens a tx,
  `SET LOCAL ROLE` to the caller's role, and runs the query. PG
  returns `42501` directly for any cross-vault reference — the
  application no longer inspects user SQL for forbidden identifiers.
  Public vaults reach all authenticated users via a wildcard
  `akb_authenticated` role. Design in
  `docs/designs/pg-native-rbac/`.

- **ACL hardening at the MCP boundary.** `akb_search` now forwards
  the caller's `user_id` into the service layer so the ACL prefilter
  actually fires, and falls closed with `check_vault_access` when a
  vault arg is supplied (#66, #67). `SearchService.search` itself
  now raises `ValidationError` if both `vault` and `user_id` are
  None — mirroring the existing guard in `grep` (#70, #71).

- **Concurrency & atomicity audit follow-through.** Eight weeks of
  audit (`docs/reviews/2026-05-20-concurrency-audit/`) landed as
  hardening across the write path: deterministic lock order in
  `bm25_vocab` UPSERT, race-safe `pgvector.ensure_collection`,
  serialized GitPython IndexFile ops, vault-level advisory locking
  on put/update/edit/delete.

- **Server-side JWT revocation.** `users.tokens_revoked_before` is
  checked on every request so admin / user / self-initiated session
  invalidation reflects immediately, without rotating the global
  `jwt_secret`.

### Operational endpoints (admin-only)

- `GET  /api/v1/admin/role-state` — read-only diff of PG role state
  vs catalog (drift inspection). Returns missing/orphan user roles,
  missing memberships, missing or stale public-access grants, and
  per-table GRANT drift in one call.
- `POST /api/v1/admin/reconcile-roles` — on-demand reconciler if
  diff reveals drift. Same idempotent pass that runs at startup.
- `GET  /api/v1/admin/users` — list every user with stats.
- `DELETE /api/v1/admin/users/{user_id}` — admin-driven user
  deletion (owned vaults cascade).
- `POST /api/v1/admin/users/{user_id}/revoke-sessions` — invalidate
  every JWT for a user (incident response / offboarding).
- `POST /api/v1/admin/users/{user_id}/reset-password` — generate a
  one-time temporary password.

### Observability

- `/health.rbac` — `RoleSync` hook-failure counters + last reconcile
  outcome + timestamp. Surfaces silent drift to dashboards without
  log grep.
- `lifecycle.start_workers` now joins a periodic `RoleSync`
  reconcile loop (configurable via
  `role_sync_reconcile_interval_secs`, default 3600 s, 0 to
  disable).

### Versioned reads

- `akb_get` / REST `GET /documents/{id}?version=<commit>` —
  retrieve any historical commit of a document. Frontmatter is
  parsed against the version's metadata when possible, with a
  `metadata_is_current` flag when the version predates a
  frontmatter shape change. Git is canonical for body; PG metadata
  is best-effort for older commits.

### Search & indexing

- Parallel `embed_worker` via `indexing_concurrency` config — N
  workers drain the chunks queue in parallel, with per-row
  transactions so a crash doesn't double-index.
- BM25 corpus stats (`avgdl`, per-term `df`) refreshed on a cadence
  (`bm25_recompute_interval_secs`, default 6 h) so the sparse leg
  doesn't stay degenerate after fresh installs.
- Rerank fusion scoring (Reciprocal Rank Fusion) improved to
  preserve first-stage signal when rerank reorders.
- LongMemEval hybrid retrieval tuning — see
  `eval/longmemeval/results/2026-05-20-longmemeval-s.md`.
- Embed worker always sends the configured `embed_dimensions`
  param so MRL models match the schema dim.

### Skill-flow polish

- `akb_help(topic="vault-skill", vault=<name>)` returns the vault's
  skill doc + a default fallback when the vault hasn't customised
  it. `text/markdown` content-type for direct rendering.
- New `GET /api/v1/help/skill-template` exposes the default
  vault-skill body for previewing in the UI before vault creation.
- Vault create seeds `overview/vault-skill.md` with a "Guide" title
  + 6-relation enum + Document Template section.

### Internal / removed

- The application-side `akb_sql` sandbox (`_validate_sql_surface`
  identifier blocklist, `_FORBIDDEN_TOKEN` regex, `_VT_IDENTIFIER`
  match, `allowed_pg_tables` enumeration, read-only-tx forcing) is
  removed. The boundary moved to PG; these become redundant.
- `POST /api/v1/auth/tokens` no longer accepts a `scopes` array.
  The DB column stays, but the input was a user-visible lie — no
  request handler ever enforced it. Re-introduce with the matching
  check when scope enforcement is wired in.

### MCP tool surface

No tool was removed. Tools whose internal flow changed:

- `akb_sql` — runs via PG-RBAC executor; cross-vault references
  return PG `42501`; admin users bypass the per-user role and run
  as the backend service role (matching existing trust model).
- `akb_search` — forwards `user_id`; fails closed on
  unauthorized-vault arg.
- `akb_set_public` — moved out of the MCP handler into
  `access_service.set_public_access`; the handler is now a thin
  adapter. Same semantics, cleaner separation.

### Test plan

- 50 cases in the new `test_pg_rbac_e2e.sh` covering positive
  paths, 15 cross-vault SQL surface variations, app pre-flight
  reject, reader scope, public-vault access, and lifecycle drift
  recovery.
- 14 unit cases in `test_role_sync.py` covering helpers, lifecycle
  hook idempotency, public-access transitions, drift detection
  including per-table GRANT drift, hook metrics.
- New `scripts/bench_pg_rbac.py` microbenchmark. Measured locally:
  `SET LOCAL ROLE` adds ~63 µs per `akb_sql` tx; reconcile of
  50 users × 25 vaults + 251 grants finishes in 108 ms.
- All 8 existing e2e suites pass unchanged (`test_mcp_e2e` 75/75,
  `test_security_edge` 65/65 after #69, etc.).

### Acknowledgments

- **@MackDing** for #65 (proxy keep-alive perf) and #66/#67 (MCP
  search ACL enforcement) — first external contributor; the
  security PR pushed maintainer audit into the surrounding handlers
  and surfaced #70 / #71 follow-ups.

## 0.1.0

Initial OSS release. See git history for the pre-OSS development
series.
