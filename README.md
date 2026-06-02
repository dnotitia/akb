<p align="center">
  <img src="docs/assets/akb-hero.png" alt="AKB — agents reading and writing into a permissioned knowledge vault of docs, tables, and files, linked by a URI graph" width="100%">
</p>

# AKB — Agent Knowledge Base

> **Organizational memory for AI agents.** Git-backed knowledge base served
> over the **Model Context Protocol (MCP)** — agents read and write directly
> with hybrid semantic + keyword search, structured tables, files, and a URI
> graph. Drop-in alternative to Confluence / Notion for Claude Code, Cursor,
> Windsurf, and any MCP-aware agent.

[![License: BSL 1.1](https://img.shields.io/badge/license-BUSL--1.1-blue.svg)](./LICENSE)
[![npm: akb-mcp](https://img.shields.io/npm/v/akb-mcp.svg?label=npm%3A%20akb-mcp)](https://www.npmjs.com/package/akb-mcp)
[![MCP](https://img.shields.io/badge/MCP-Streamable%20HTTP-orange.svg)](https://modelcontextprotocol.io)

## Works with

Any agent client that speaks **MCP (Streamable HTTP or stdio)**:

- **Claude Code** — CLI / VS Code / JetBrains
- **Claude Desktop** — macOS / Windows
- **Cursor**, **Windsurf**, **Cline**, **Continue** — via the
  [`akb-mcp`](https://www.npmjs.com/package/akb-mcp) stdio proxy
- Custom agents — direct HTTP `POST /mcp/` with a Bearer token

## Why AKB

Most knowledge tools are built for humans clicking through a UI. Agents need a
different shape: structured documents, semantic + keyword search in one call,
explicit relations, and full version history. AKB gives agents a single set of
tools (`akb_put`, `akb_search`, `akb_browse`, `akb_relations`, …) over a
backing store of Git bare repos and a PostgreSQL hybrid index.

## Retrieval quality

Memory is only useful if the right note comes back. AKB's hybrid retrieval
(dense + BM25, source-level dedup) was benchmarked on
[LongMemEval](https://github.com/xiaowu0162/LongMemEval)-S — 500 long-context
questions, ~50 chat sessions per question. **Recall@5 = 98.4%**, with no
reranker in the loop.

| System | R@5 | n | Reranker | Source |
|---|---:|:---:|:---:|---|
| **AKB hybrid** | **98.4%** | 500 | no | this repo |
| MemPalace hybrid + rerank | 98.4% | 450 | yes | [MemPalace](https://github.com/mempalace/mempalace) |
| gbrain hybrid | 97.6% | 500 | no | [gbrain-evals](https://github.com/garrytan/gbrain-evals) |
| gbrain vector | 97.4% | 500 | no | gbrain-evals |

Methodology, per-category breakdown, and a one-command reproducible harness
live in [`eval/longmemeval/`](eval/longmemeval/). The embedding model differs
across systems (AKB: `bge-m3@1024`), so read this as a stack-level comparison.

## Design philosophy

**Core stays small; flexibility comes from extension, not built-in
automation.** AKB does not ship its own consolidator, summariser, or
"knowledge gardener" — instead every write emits a structured event to a
Redis Stream (`akb:events`). Operators wire any external consumer
(periodic synthesis bot, doc-rot reaper, weekly-digest agent, audit
trail, …) on top, with no patches to the core. The base contract is a
read/write store; opinions about *what to do with* the knowledge live
outside.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                  Access Layer                            │
│   MCP Server  │  REST API  │  Web UI                     │
├──────────────────────────────────────────────────────────┤
│                  Core Services                           │
│   Document (Put/Get)  │  Search (Hybrid: dense+BM25)     │
│   Relations (graph)   │  Session  │  Publications        │
├──────────────────────────────────────────────────────────┤
│                  Storage Layer                           │
│   Git bare repos       │  PostgreSQL 16 (text + meta SoT)│
│                        │  Vector store (driver):         │
│                        │    pgvector  (default, same PG) │
│                        │    qdrant    (optional)         │
│                        │    seahorse  (managed, optional)│
└──────────────────────────────────────────────────────────┘
```

PostgreSQL is the source of truth — chunk text + metadata + BM25 vocab.
The vector store is a driver-pluggable derived index holding dense
embeddings and corpus-side sparse vectors. Full vector-store loss is
recoverable from PG by setting `chunks.vector_indexed_at = NULL` and
letting the indexing worker re-populate.

## Key Concepts

- **Vault** — A Git bare repo. The unit of access control and physical isolation.
- **Collection** — A directory inside a vault. Topical grouping of documents.
- **Document** — Markdown + YAML frontmatter, optimised for agent read/write.
- **Hybrid Search** — Dense (semantic) + BM25 (lexical) fused via RRF in one call.
- **Relations** — `depends_on`, `related_to`, `implements` in frontmatter form an explicit knowledge graph.
- **Vault isolation in `akb_sql`** — Enforced by PostgreSQL ACL. Each
  AKB user has a corresponding PG role (`akb_user_<uid>`) and each
  vault has three group roles (`akb_vault_<vid>_{reader,writer,admin}`).
  `akb_sql` runs the user's SQL inside a transaction with
  `SET LOCAL ROLE`; cross-vault references return PG `42501`
  directly. No application-side regex inspects user SQL for forbidden
  identifiers. See `docs/designs/pg-native-rbac/`.

## MCP Tools (selection)

| Tool | Description |
|------|-------------|
| `akb_list_vaults` / `akb_create_vault` | Vault management |
| `akb_put` / `akb_get` / `akb_update` / `akb_delete` | Document CRUD (Git commit + indexing) |
| `akb_put_file` / `akb_get_file` / `akb_delete_file` | File attachments — proxy-side (requires local filesystem) |
| `akb_create_table` / `akb_alter_table` / `akb_drop_table` / `akb_sql` | Tabular content — per-doc tables + SQL |
| `akb_browse` | Tree traversal (collection → docs) |
| `akb_search` / `akb_grep` | Hybrid search (dense + BM25) / literal grep |
| `akb_drill_down` | Section-level retrieval |
| `akb_relations` / `akb_link` / `akb_unlink` / `akb_graph` | Knowledge graph |
| `akb_edit` / `akb_diff` / `akb_history` | In-place edit, diff, Git history |
| `akb_grant` / `akb_revoke` / `akb_set_public` | Permission boundaries — per-user, per-org, public |
| `akb_publish` / `akb_unpublish` | Public publication |

Agent memory and session lifecycle are not MCP tools — they live on
the dedicated `/api/v1/agent-sessions` REST surface, driven by AKB
lifecycle plugins (`akb-claude-code`, `akb-cursor`, …) that hook into
the agent's own SessionStart / PreCompact / SessionEnd events. As an
agent, your own memory vault (`agent-memory-{username}`) is browsable
through the standard `akb_search` / `akb_browse` / `akb_get` tools
exactly like any other vault.

The full tool catalogue is exposed via `akb_help()` from any MCP client.

## Document Format

Every vault resource has a location-aware AKB URI — the canonical handle
used by every tool and stored in relations. As of 0.3.0:

```
akb://{vault}                                          vault root (browse target)
akb://{vault}/coll/{coll_path}                         collection (browse target)
akb://{vault}[/coll/{coll_path}]/doc/{filename}        document
akb://{vault}[/coll/{coll_path}]/table/{name}          table
akb://{vault}[/coll/{coll_path}]/file/{uuid}           file
```

The `/coll/{coll_path}` segment is omitted for resources at the vault
root. Walking up a URI to its parent collection is a pure string
operation — paste the parent into `akb_browse(uri=...)` to list
siblings without an extra lookup.

```yaml
---
title: "Payment API v2 migration plan"
type: plan              # note | report | decision | spec | plan | session | task | reference
status: active          # draft | active | archived | superseded
tags: [payments, api]
domain: engineering
summary: "REST → gRPC transition plan."
depends_on: ["akb://eng/coll/specs/doc/payment-api-v2.md"]
related_to: ["akb://eng/coll/meetings/doc/2026-05-01-payments.md"]
---

# Payment API v2 migration plan
...
```

## Quick Start

AKB ships as a **3-container stack** (PostgreSQL with pgvector + backend +
frontend). You bring an OpenAI-compatible embedding endpoint (OpenAI,
OpenRouter, self-hosted vLLM/TEI, etc.) — that's the only required external
dependency for core CRUD and search. Prefer running a separate Qdrant
cluster, or pointing at a managed Seahorse Cloud table? See *Vector store*
below.

```bash
# 1. Configure
cp config/app.yaml.example   config/app.yaml
cp config/secret.yaml.example config/secret.yaml
$EDITOR config/secret.yaml   # set embed_api_key (and jwt_secret for any non-local deploy)

# 2. Run
docker compose up -d

# 3. Open
open http://localhost:3000
```

`config/app.yaml` and `config/secret.yaml` are the **single source of runtime
configuration** — no environment variables are read by the backend. Mount the
`config/` directory at `/etc/akb/` in any deployment.

### Vector store (driver-pluggable)

Hybrid search (dense + BM25 sparse, RRF-fused) runs through a driver
interface. Three drivers ship; pick at config time:

- **`pgvector`** (default) — uses the same Postgres container that holds
  application data. The pgvector/pgvector image pre-installs the
  extension; the driver creates a separate `vector_index` schema, so the
  main `chunks` table stays plain PostgreSQL. RRF fusion runs
  application-side. No external service to operate.
- **`qdrant`** — runs a separate Qdrant container; native RRF via the
  Query API. Useful when you already operate Qdrant or want to scale
  the vector store independently of Postgres.
- **`seahorse`** — points at a managed [Seahorse Cloud][shc] table over
  its TABLE_V2 + BFF API (Bearer auth, per-table host). No
  infrastructure to run on your side; you provision a table in the
  Seahorse console (or let the driver auto-create one) and AKB stores
  its chunks there. Native RRF, server-side BM25. See
  [`docs/vector-store-seahorse.md`](./docs/vector-store-seahorse.md)
  for the end-to-end setup walkthrough (sign-up → token → schema →
  config).

[shc]: https://console.seahorse.dnotitia.ai

Switching drivers is a config edit (no schema migration on the main DB):

```bash
# Default flow targets pgvector.
docker compose up

# Qdrant:
docker compose -f docker-compose.yaml -f docker-compose.qdrant.yaml up
$EDITOR config/app.yaml     # vector_store_driver: qdrant
                            # vector_url: http://qdrant:6333

# Seahorse Cloud (managed; full guide in docs/vector-store-seahorse.md):
docker compose up           # no extra container needed
$EDITOR config/app.yaml     # vector_store_driver: seahorse
                            # seahorse_tenant_uuid: <your tenant>
                            # seahorse_table_name: <your table>
$EDITOR config/secret.yaml  # seahorse_token: shsk_<...>
```

Embedding model + dimensions are also fully pluggable via
`embed_base_url` / `embed_model` / `embed_dimensions` — the codebase has
no hard-coded model. For pgvector with HNSW, keep `embed_dimensions ≤ 2000`
(or 4000 with `halfvec`); larger models fall back to exact scan.
Qdrant/Seahorse have no such limit (Qdrant up to 65536, Seahorse up to
its table-defined dim).

### LLM features (optional)

LLM is only used by the `metadata_worker` to auto-tag documents imported via
external git mirroring. Core CRUD/search works without it. To enable, set
`llm_base_url` / `llm_model` in `app.yaml` and `llm_api_key` in `secret.yaml`.

### Event fanout (optional)

The PG `events` outbox is always written. Set `redis_url` in `app.yaml` to
have the `events_publisher` worker drain the outbox to a Redis Stream
(`akb:events`) so external services can subscribe via `XREAD` / consumer
groups. Leave blank to disable; events still accumulate in PG and you can
build an SSE endpoint on top of the LISTEN/NOTIFY trigger without Redis.

### Production deployment

For Kubernetes, see [`deploy/k8s/README.md`](./deploy/k8s/README.md). The
`deploy/k8s/` directory contains a generic kustomize base; provide your
own registry, hostname, and TLS issuer via the documented env vars or an
operator-private overlay under `deploy/k8s/internal/`.

## Project Structure

```
akb/
├── backend/                  # Python 3.11 / FastAPI / asyncpg / GitPython
│   ├── app/
│   │   ├── api/routes/       # REST endpoints
│   │   ├── services/         # Business logic + workers
│   │   └── db/               # PostgreSQL schema + migrations
│   ├── mcp_server/           # Streamable HTTP MCP server
│   └── tests/                # E2E shell tests
├── frontend/                 # React 19 + TypeScript + Vite + Tailwind
├── packages/
│   └── akb-mcp-client/       # stdio ↔ HTTP MCP proxy (npm: akb-mcp)
├── agents/                   # Reference Python agent runtime (think/act loop over MCP)
├── templates/                # Doc templates (ADR, PRD, runbook, …) and vault profiles
├── design-system/            # Frontend design system docs
├── config/
│   ├── app.yaml.example      # Non-secret runtime settings
│   └── secret.yaml.example   # API keys, passwords (gitignored when not .example)
├── deploy/
│   └── k8s/                  # Generic kustomize base for Kubernetes
└── docker-compose.yaml       # 3-container local stack (postgres + backend + frontend)
```

## Tech Stack

- **Backend**: Python 3.11, FastAPI, Uvicorn, asyncpg, GitPython, MCP SDK
- **Database**: PostgreSQL 16 (main DB needs no extension; the same
  pgvector/pgvector image hosts the optional vector_index schema)
- **Vector store**: driver-pluggable (pgvector default; Qdrant or
  Seahorse Cloud optional — hybrid dense + BM25 sparse, RRF fusion)
- **Event stream** (optional): PG `events` outbox + Redis Streams fanout
- **Frontend**: React 19, TypeScript, Vite, Tailwind CSS v4, Radix UI
- **Auth**: JWT + Personal Access Tokens (PATs)
- **MCP**: Streamable HTTP (backend) + stdio proxy (`akb-mcp` on npm)

## Versioning

AKB follows [SemVer](https://semver.org/). The product version lives in
`backend/pyproject.toml` (`[project].version`) and is mirrored to
`frontend/package.json` via `scripts/bump-version.sh <x.y.z>`. Each
`deploy/k8s/deploy.sh` run tags the Docker images with both the explicit
version (`:${VERSION}`) and `:latest`, so historical builds remain
pullable for rollback.

`packages/akb-mcp-client` (the `akb-mcp` npm proxy) follows its own npm
semver lifecycle and is **not** tied to the product version.

## License

The AKB backend, frontend, and deployment manifests are licensed under
the [Business Source License 1.1](./LICENSE) — source-available, with
an Additional Use Grant that permits production use (commercial or
non-commercial) up to a seat-count threshold, automatically converting
to **Apache License 2.0** four years after each version's first public
release.

The npm `akb-mcp` proxy (`packages/akb-mcp-client/`) is separately
licensed under the **MIT License** so it can be freely embedded in any
agent client without restriction.

**Free production use of the backend** — you may deploy AKB in
production, commercial or not, provided your aggregate deployment
serves **fewer than 100 Named Seats** (distinct human user accounts in
the `users` table, per deployment; service accounts and
90-day-inactive accounts excluded — see [LICENSE](./LICENSE) for the
precise definition).

**Commercial license required** for any of:

- Production use of the backend at or above 100 Named Seats.
- Offering AKB (modified or not) as a hosted service, on-premises
  product, embedded component, or rebranded distribution to third
  parties — regardless of seat count.

**Trademarks** — "AKB", "Dnotitia", and "Seahorse" are trademarks of
Dnotitia, Inc. The software license does not grant trademark rights.
Forks and derivative works must be distributed under a different name.
See [TRADEMARKS.md](./TRADEMARKS.md).

For commercial licensing, the rationale behind the BSL transition, or
trademark permission requests, see
[LICENSE-CHANGE.md](./LICENSE-CHANGE.md) or contact
**support@dnotitia.com**.

## Security

Found a vulnerability? See [SECURITY.md](./SECURITY.md) — please report
privately, not via public issues.

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md).
