# AKB Backend — Changelog

The AKB backend ships as a Docker image and as the HTTP layer behind
the `akb-mcp` stdio proxy. This changelog tracks the backend
specifically; the proxy has its own log in
`packages/akb-mcp-client/CHANGELOG.md` and a separate version stream.

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
