# AKB Project Guide

## Architecture

- **Backend**: Python 3.11, FastAPI + Uvicorn, PostgreSQL 16 + pgvector, GitPython (bare repo)
- **MCP**: Anthropic MCP SDK (Streamable HTTP) — backend serves as HTTP MCP server
- **Proxy**: `packages/akb-mcp-client/` — Node.js ESM, zero dependencies, stdio ↔ HTTP bridge
- **Frontend**: React 19 + TypeScript + Vite + Radix UI + Tailwind CSS v4

## 2-Layer MCP Architecture

Backend (Streamable HTTP) handles all business logic. Proxy (stdio) handles local filesystem operations.

- **Proxy-only features**: `akb_put_file`, `akb_get_file`, `akb_delete_file`, `file` param on `akb_put`/`akb_update`
- **Proxy injects** these into `tools/list` response — backend never sees them
- **Rule**: Anything requiring local filesystem access MUST be in the proxy, never in the backend

## Deployment

Two supported paths:

- **Local / dev**: `docker compose up -d` from repo root. Frontend on `:3000`,
  backend on `:8000`. See `README.md` for the quickstart.
- **Kubernetes**: generic manifests under `deploy/k8s/`. `deploy/k8s/deploy.sh`
  builds + pushes images to `$REGISTRY` (override via env) and applies the
  kustomize base. `deploy/k8s/internal/` (gitignored) is where operator-
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

- `backend/tests/test_mcp_e2e.sh` — main E2E (75 tests), covers core CRUD, search, tables, access control
- `backend/tests/test_edit_e2e.sh` — akb_edit E2E (33 tests)
- `backend/tests/test_stdio_files_e2e.sh` — file upload/download E2E (18 tests)
- `backend/tests/test_put_file_param_e2e.sh` — file param E2E (15 tests)
- `backend/tests/test_security_edge_e2e.sh` — security & edge cases (44 tests)
- `backend/tests/test_graph_replace_e2e.sh` — graph, replace, unicode, cross-vault (29 tests)
- `backend/tests/test_defensive_e2e.sh` — defensive / lifecycle (33 tests)
- `backend/tests/test_probes_e2e.sh` — /livez /readyz /health + concurrent-burst regression
- All tests target whatever `AKB_URL` points at (default `http://localhost:8000`).
- Tests create ephemeral users/vaults and clean up after themselves.

## Key Conventions

- Embedding: any OpenAI-compatible `/v1/embeddings` endpoint
  (configured via `embed_base_url` / `embed_model` / `embed_dimensions` in
  `config/app.yaml`). Defaults in `app.yaml.example` target OpenAI.
- LLM (summarization, optional): any OpenAI-compatible `/v1/chat/completions`
  endpoint (`llm_base_url` / `llm_model`). Only used by `metadata_worker`
  for auto-tagging external-git imports — leave blank to disable.
- Git: bare repos per vault at `{git_storage_path}/{vault_name}.git`.
- Auth: JWT + Personal Access Token (PAT).
- npm package: `akb-mcp` on npmjs.org.

## Indexing Pipeline

- **PG is source of truth.** Main `chunks` holds text + metadata only —
  no `embedding` column, no pgvector dependency on the main DB. BM25
  vocab + stats live in `bm25_vocab` / `bm25_stats`.
- **Vector store is a driver-pluggable derived index.** `pgvector`
  (default; can share the main PG instance under a separate
  `vector_index` schema), `qdrant` (separate StatefulSet), or
  `seahorse` (managed Seahorse Cloud table over BFF + per-table host
  API; no infra to run). Each driver holds the dense embedding and the
  corpus-side BM25 sparse vector. Selection in `config/app.yaml`
  (`vector_store_driver`).
- **Write path (POST /documents)** only touches PG + git. No vector-store
  round-trips on the request thread — chunks go in with
  `vector_indexed_at = NULL`.
- **Indexing worker** (`embed_worker`, started in `lifespan`) drains
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

## Git Storage

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
