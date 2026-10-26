# AKB Project Guide

## Coding agent workflow

This file is the instruction file for every coding agent and for people working
in this repository. Claude Code and Codex both read `AGENTS.md` directly; there
is no separate per-agent instruction file.

- **Model and effort are each person's choice.** Nothing in this repository
  sets a coding-agent model or reasoning effort; Claude Code and Codex use
  whatever you configured. Product inference models (`embed_model`,
  `llm_model` in `config/app.yaml`) are a separate, product-level setting.
- **Helper roles** are optional and defined once in `.agents/roles.toml`:
  `explorer` and `reviewer` are read-only, `worker` makes bounded edits.
  `.codex/` and `.claude/agents/` are rendered from that file by
  `python scripts/agent-roles.py`; edit the source, never a rendered file.
  `scripts/check.sh` fails on a hand-edited or stale rendered file and on any
  model or effort key in the source or in `.claude/settings.json`.
- **Verify before saying something passes.** `bash scripts/check.sh` is the
  static gate CI runs; its tool setup is in the Static Analysis Gate section
  of `CONTRIBUTING.md`, and it names any missing prerequisite. Also run the
  checks that exercise what you changed: the frontend gate below for UI work,
  the suites in `scripts/ci/README.md` for backend behavior. Report what ran
  and its result, and say so when a check was not run.
- **Do not break the contracts below** (proxy-only filesystem tools, the doc
  ID lookup, the indexing write path's crash-safe ordering, release tag
  prefixes). A change to one updates its section here in the same pull request.
  Preserve unrelated local work.
- **This repository is public.** Code, comments, docs, commit messages and
  pull-request text must not contain internal hostnames, private planning
  references, credentials, or references to internal issue trackers. Explain
  the reason in self-contained prose instead of pointing at private material.

## Architecture

- **Backend**: Python 3.14, FastAPI + Uvicorn, PostgreSQL 16 + pgvector; PostgreSQL Native revisions by default, GitPython for explicit Bare Git installations
- **MCP**: Anthropic MCP SDK (Streamable HTTP) — backend serves as HTTP MCP server
- **Proxy**: `packages/akb-mcp-client/` — Node.js ESM, zero dependencies, stdio ↔ HTTP bridge
- **Frontend**: React 19 + TypeScript + Vite + Radix UI + Tailwind CSS v4 + Tiptap (`@akb/markdown-editor`; lazy-loaded in Edit / New-document flows)

## Frontend Design System

**Read `frontend/DESIGN_SYSTEM.md` before any frontend UI work** — it is the
authoritative reference for the design system (akb-platform family: Dnotitia
teal/orange palette, Pretendard, rounded corners, soft shadows, glass + aurora).
Centrally governed:

- **One token source**: `frontend/src/index.css` — all colors/radii/shadows/motion
  live in the Tailwind v4 `@theme { … }` block (+ a `.dark` override). Components
  **never hardcode a hex** — they read `var(--color-*)` / Tailwind token classes.
  Brand: teal `#004059` (`--color-primary`, links/primary actions/active) +
  orange `#e55e2c` (`--color-accent`, highlights) with `--color-accent-strong`
  `#c44a1e` for white-text fills (WCAG AA). Light = paper-cool; `.dark` = slate.
- **Primitive vocabulary**: `frontend/src/components/ui/*` — compose pages from
  `Button`/`Panel`+`PanelHeader`/`PageHeader`/`StatTile`/`Eyebrow`/`CodeSnippet`/
  `Badge`/`Input`/`Textarea`/`Select`/`Dialog`/`ConfirmDialog`/`Tabs`/`Tooltip`
  instead of re-rolling patterns inline.
- **Build guard**: `frontend/scripts/design-check.mjs` (`npm run design:check`,
  also in `build`) fails on raw 6-digit hex in components + the legacy
  `bg-foreground text-background` slab. Exempt: `index.css` + tests.
- **Color placement**: teal = interactive text/links/active/primary buttons;
  orange = exactly one marquee CTA per view (create/publish) + fresh-token
  highlights — never a second competing orange; semantic state always pairs
  color with an icon/text (never color alone).
- **Frontend check gate** (run before committing frontend changes):
  `cd frontend && npm run design:check && npm run typecheck && npm run lint && npm run test`.

## 2-Layer MCP Architecture

Backend (Streamable HTTP) handles all business logic. Proxy (stdio) handles local filesystem operations.

- **Proxy-only features**: `akb_put_file`, `akb_get_file`, `akb_delete_file`, `file` param on `akb_put`/`akb_update`
- **Proxy injects** these into `tools/list` response — backend never sees them
- **Rule**: Anything requiring local filesystem access MUST be in the proxy, never in the backend

## Deployment

Two supported paths:

- **Local / dev**: follow the Native prepare/bootstrap quickstart in `README.md`.
  Use `docker-compose.yaml` with `docker-compose.native.yaml`. Frontend on `:3000`,
  backend on `:8000`. See `README.md` for the quickstart.
- **Kubernetes**: generic manifests under `deploy/k8s/`. `deploy/k8s/deploy.sh`
  requires an explicit legacy profile and builds + pushes images to `$REGISTRY`
  (override via env). New Native installations use `deploy/k8s/native/` with
  generated identity and pinned images; see `docs/operations/native-installation.md`. `deploy/k8s/internal/` (gitignored) is where operator-
  specific overrides live — `deploy-internal.sh` and any private overlay.
- Both backend and frontend Deployments use `imagePullPolicy: Always`, so a
  `kubectl rollout restart deployment/backend -n <ns>` picks up a new
  `:latest` push.

### Deploy checklist (backend changes)

1. Build + push + apply (mechanism depends on the target cluster).
2. Verify pods: `kubectl get pods -n <ns>`.
3. Run E2E against the deployed URL:
   `AKB_URL=https://<your-host> bash backend/tests/test_mcp_e2e.sh`.

### Deploy checklist (proxy changes)

1. Bump version in `packages/akb-mcp-client/package.json`.
2. `cd packages/akb-mcp-client && npm publish`.
3. Commit the version bump.

## Doc ID Format

- DB primary key: full UUID (e.g. `0c37e906-6db0-48c2-ac5d-576d0797b3f7`)
- User-facing ID: `d-` prefix + first 8 hex of hash (e.g. `d-94d8657f`), stored in `metadata->>'id'`
- All doc lookups MUST match by: `d.id::text = $X OR d.metadata->>'id' = $X OR d.path LIKE '%' || $X || '%'`
- Central function: `document_repo.find_by_ref()`

## Testing

The endpoint-driven suites, isolated repository-owned runtime, and optional
Ubuntu host bootstrap are documented in
[`scripts/ci/README.md`](scripts/ci/README.md).

- The canonical gate and setup commands live in [scripts/ci/README.md](scripts/ci/README.md).
- `backend/tests/mcp_e2e/` covers authenticated MCP product behavior through
  the Python SDK; `test_detail_e2e.py` includes exact-text editing, OCC,
  Unicode, graph and replacement regressions.
- Shell suites retain transport, REST, PostgreSQL ACL and local-file
  boundaries. `test_mcp_e2e.sh` is a transport boundary suite, not the full
  product gate. Do not substitute it for the SDK suite.
- Use the repository-owned isolated runtime for the complete gate. Tests
  create ephemeral data; follow its cleanup contract rather than targeting
  an unrelated installation.

## Key Conventions

- Embedding: any OpenAI-compatible `/v1/embeddings` endpoint
  (configured via `embed_base_url` / `embed_model` / `embed_dimensions` in
  `config/app.yaml`). Defaults in `app.yaml.example` target OpenAI.
- LLM (summarization, optional): any OpenAI-compatible `/v1/chat/completions`
  endpoint (`llm_base_url` / `llm_model`). Only used by `metadata_worker`
  for auto-tagging external-git imports — leave blank to disable.
- Git: bare repos per vault at `{git_storage_path}/{vault_name}.git`.
- Auth: JWT + Personal Access Token (PAT).
- Vault isolation in `akb_sql`: enforced by **PostgreSQL ACL** via
  per-user PG roles (`akb_user_<uid>`) and per-vault group roles
  (`akb_vault_<vid>_{reader,writer,admin}`). A user's `akb_sql`
  query runs inside a tx with `SET LOCAL ROLE akb_user_<uid>`; PG
  returns `42501` for any reference to a table outside their grant.
  System tables (`users`/`vaults`/`tokens`/`chunks`/...) are
  unreachable from `akb_user_*` roles by default.
  - Lifecycle (signup, vault create/delete, grant/revoke) emits the
    corresponding PG role DDL via `RoleSync`
    (`backend/app/services/role_sync.py`). Hooks are best-effort;
    the reconciler in `lifecycle.init_storage` rebuilds full role
    state from the catalog at startup (and on
    `POST /admin/reconcile-roles`).
  - `UserSqlExecutor` (`backend/app/services/user_sql_executor.py`)
    is the sole entry point for user SQL. System admins
    (`users.is_admin=TRUE`) bypass the role switch.
  - Design: `docs/designs/pg-native-rbac/00-overview.md`.
- npm package: `akb-mcp` on npmjs.org.

## Release & Versioning (monorepo)

Two independently versioned components, each with its own tag
namespace + GitHub Release stream + changelog:

| Component | Source of truth | Tag prefix | Changelog |
|---|---|---|---|
| Backend | `backend/pyproject.toml` `version` | `backend-vX.Y.Z` | `backend/CHANGELOG.md` |
| akb-mcp proxy | `packages/akb-mcp-client/package.json` `version` | `akb-mcp-vX.Y.Z` | `packages/akb-mcp-client/CHANGELOG.md` |

Prefix is required so the two streams never collide on the same
`vX.Y.Z` (`v2.0.0` was historically used for proxy; new convention
disambiguates).

**GitHub `--latest` flag**: the most recently published release wins
the badge (single repo-wide badge). Dependency tools that care
(npm, docker, etc.) don't read the badge — they read their native
registries. So `--latest` is UI-only.

**Legacy proxy tags** `v2.0.0` and `v2.0.1` stay (external links).
Same commits also carry `akb-mcp-v2.0.0` / `akb-mcp-v2.0.1` as
dual tags. Every release going forward uses the prefixed form
only.

### Release flow per component

- **Backend**: bump `backend/pyproject.toml`, add CHANGELOG entry,
  PR + merge → `git tag -a backend-vX.Y.Z <merge-commit> -m "..."` →
  push → `gh release create backend-vX.Y.Z [--latest] --notes "..."`
  → build the release image from the bumped version. Deployment is an
  operator-specific step; use [Native installation](docs/operations/native-installation.md)
  for supported public deployment instructions.
- **Proxy**: bump `packages/akb-mcp-client/package.json`, add
  CHANGELOG entry, PR + merge → for an authorized release, run
  `cd packages/akb-mcp-client && npm publish --access public`
  using the verified publishing account →
  `git tag -a akb-mcp-vX.Y.Z <bump-commit> -m "..."` → push →
  `gh release create akb-mcp-vX.Y.Z [--latest] --notes "..."`.

## Indexing Pipeline

- **PG is source of truth.** Main `chunks` holds text + metadata only —
  no `embedding` column, no pgvector dependency on the main DB. BM25
  vocab + stats live in `bm25_vocab` / `bm25_stats`.
- **Vector store is a driver-pluggable derived index.** `pgvector`
  (default; can share the main PG instance under a separate
  `vector_index` schema), `qdrant` (separate StatefulSet), or
  `seahorse-cloud` (managed Seahorse Cloud), or the self-hosted
  `seahorse-db` / `seahorse-db-grpc` drivers. Each driver holds the dense embedding and the
  corpus-side BM25 sparse vector. Selection in `config/app.yaml`
  (`vector_store_driver`).
- **Write path (POST /documents)** persists authoritative revisions in PG
  for `postgres_native`; explicit `bare_git` installations also use Git.
  No vector-store round-trips occur on the request thread — chunks go in with
  `vector_indexed_at = NULL`.
- **Indexing worker** (`embed_worker`, started in the worker or combined process role) drains
  rows where `vector_indexed_at IS NULL` and runs the full pipeline
  atomically per chunk: embed → BM25 sparse encode →
  `vector_store.upsert_one()` → `vector_indexed_at = NOW()`.
- **Delete worker** (`vector_indexer`) drains the
  `vector_delete_outbox` and removes points from the configured
  driver. Outbox is written in the same transaction as the
  `chunks DELETE` so a crash never leaves orphan vectors.
- **Crash-safe ordering**: PG INSERT first (NULL flags), then async
  vector-store upsert, then `UPDATE chunks SET vector_indexed_at = NOW()`.
  A crash leaves NULL flags; the worker catches up.

## Revision storage

New installations use `postgres_native`: authoritative revisions live in
PostgreSQL and no Git worktree is required. Existing databases require the
explicit [cutover procedure](docs/operations/native-revision-existing-database-cutover.md);
changing the selector alone does not migrate data.

### Explicit Bare Git installations

The following Git layout and locking apply only to the Bare Git backend:

- Bare repo per vault at `/data/vaults/{vault_name}.git`
- **Persistent linked worktree** per vault at
  `/data/vaults/_worktrees/{vault_name}` (created once via
  `git worktree add`). Commits go through the worktree — no clone,
  no push (object store is shared with the bare).
- Per-vault `threading.Lock` in `GitService` serializes concurrent
  writes (asyncio.to_thread dispatches commits onto a thread pool).
- All `GitService` write methods are invoked via `asyncio.to_thread`
  from the async layer so the event loop isn't blocked on git I/O.

## Health Endpoints

- `GET /livez` — return 200 immediately (liveness). No deps.
- `GET /readyz` — DB ping + vector-store ping, 30s TTL-cached on success.
  The vector store is a **soft** check: a failed ping reports
  `"vector_store": "degraded:..."` in the detail but still returns
  ready (search degrades, everything else keeps working).
- `GET /health` — detailed status for dashboards. Returns
  `vector_store.{reachable, backfill, bm25_vocab_size}`,
  `external_git`, `metadata_backfill`, `events`. Backfill stats come
  from `vector_indexer.pending_stats()` (single source of truth for
  the unified embed+sparse+upsert stage). Not probed by kubelet.
  Also carries top-level `oldest_pending_enqueued_at` — when the
  indexing queue is non-empty, the enqueue time of its oldest pending
  chunk. Omitted entirely when the queue is empty (never 0 or epoch).

## Tenant stats (`/stats`, separate port)

`app/stats/` serves a cached inventory snapshot — storage bytes,
corpus counts, and the previous complete UTC day's call volume — on
its **own listener**, configured by `stats.port` in `app.yaml` or
`AKB_STATS_PORT`. Unset = no socket bound, no sampler, nothing
changes. It is not part of the API app and does not appear in the
OpenAPI document.

- Separate port because a control plane must read inventory without
  reaching tenant data, and the enforcing NetworkPolicy selects on
  port. The surface has **no authentication**: reachability is the
  authorization, so do not bind it where the network policy does not.
- **Requests never compute.** A sampler writes the snapshot every
  `stats.sampler_interval_secs` (default 300); `/stats` returns the
  cache, or 503 before the first sample. A failed sample keeps serving
  the last good one.
- **Absent is not zero.** Unmeasurable numeric fields are omitted, not
  defaulted. `app/stats/schema_v1.json` (the contract consumers
  vendor) marks every one nullable and optional;
  `golden_v1*.json` are the fixtures both sides test against.
- Yesterday's activity window is folded **once** into
  `tenant_activity_daily` and served from there forever, so a restart
  cannot publish a different number for a window a consumer already
  stored (migration 087).
- No Prometheus dependency — plain JSON, deliberately not a metrics
  endpoint.

## Product decisions

Keep product intent under `docs/prd/{backlog,wip,applied,denied}/` and durable
engineering decisions under `docs/design/{proposal,accepted,denied}/`. Each
existing item owns its `README.md`, review rounds, and feedback; update its
status/stage/date when promoting it. Ordinary implementation does not require
a new planning document or a fixed critique sequence. Mirror notes to a live
AKB vault only when that write is within the user's requested scope.
