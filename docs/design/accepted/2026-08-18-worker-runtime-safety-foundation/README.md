---
status: accepted
stage: implementation
created: 2026-08-18
updated: 2026-08-18
relates:
  - docs/design/proposal/2026-07-27-multi-process-topology
  - docs/design/proposal/2026-08-10-worker-tier-and-write-path
---

# Worker Runtime Safety Foundation

## Decision

AKB now supports three explicit process roles:

| `AKB_PROCESS_ROLE` | Entrypoint | Responsibility |
|---|---|---|
| `all` (default) | `uvicorn app.main:app` | Local/development compatibility: API and durable workers together |
| `api` | `uvicorn app.main:app` | HTTP/MCP serving, API-local audit/tool-usage sinks, query tokenizer |
| `worker` | `python -m app.worker_main` | Durable queue workers, external-git reconciliation, periodic maintenance |

The Kubernetes base runs `api` and `worker` as separate containers in the same
Pod. This is an intentional intermediate topology: it isolates the serving
event loop immediately while preserving the current RWO PVC and synchronous
Bare-Git write contract. It does **not** authorize additional API replicas or
removal of the API container's PVC mount. Those changes still require the
single-writer gitd boundary and the remaining gates in the linked proposals.

```text
one Recreate Pod, one RWO PVC

  API container                         worker sidecar
  AKB_PROCESS_ROLE=api                  AKB_PROCESS_ROLE=worker
  FastAPI + MCP                         durable workers
  query tokenizer (1 child)             corpus tokenizer (2 children)
  API-local audit/tool queues           queue rescuer + periodic reconcile
            \                              /
             +---- PostgreSQL + shared Git PVC ----+
                    storage flock + ref CAS
```

## Correctness changes that make the split safe

### Shutdown

All durable runners receive their stop signal before any runner is awaited.
They then share one 35-second absolute join budget. Kubernetes grants 45
seconds, leaving ten seconds for DB/HTTP closure, audit draining, and process
teardown. Kiwi uses one native worker inside each bounded tokenizer child so
memory use cannot multiply by host CPU count.

The worker exec probe checks a container-local heartbeat written by the worker
event loop. A stale heartbeat is deleted before startup, so an old process
cannot make a new one ready prematurely.

### Durable queue claims

The vector-index, vector-delete, S3-delete, metadata, event, and native-derived
queues increment their attempt counter in the claim transaction. A process
death therefore consumes an attempt even when no exception handler runs.
Claims carry explicit claimed and abandoned timestamps; successful work resets
the retry epoch; batch consumers credit back rows they did not attempt.

A singleton PostgreSQL-advisory-lock rescuer stamps final claims abandoned once
their visibility timeout expires. It never deletes vector/S3 outbox evidence.
Queue and runner state is exposed by `/health` under `queue_claims` and
`workers`.

### Shared Git writes

The API and worker containers can both reach Bare Git during this intermediate
phase. A storage-backed per-vault `flock`, paired with the existing process
lock, now covers the shared worktree mutation interval. Commits capture their
exact parent, skip empty trees, and advance `HEAD` with `update-ref` compare-and-
swap. Same-name vault creation holds a second storage lock across creation and
rollback so one process cannot compensate by deleting another process's repo.

The race regression test starts two spawned processes against the same linked
worktree and verifies both files, ancestry, and `git fsck`.

### Event-loop and retry safety

Known synchronous S3/filesystem/hash operations are offloaded. Legacy regular
expression grep no longer executes PostgreSQL regex or Python regex on the API
loop; it uses the native grep path's killable spawned-process timeout and fixed
candidate byte/resource budgets. Large MCP response encoding is also off-loop.

The stdio proxy retries only an explicit read-only tool allowlist after an
ambiguous transport failure. Mutations and unknown future tools are never
replayed automatically. This behavior is versioned as `akb-mcp` 2.2.1.

### Multi-process startup

Schema migrations are serialized by a PostgreSQL session advisory lock. Each
migration and its ledger receipt commit in the same transaction. Role
reconciliation runs from one repeatable-read snapshot under an advisory lock;
each recoverable DDL unit has a savepoint so one failure cannot poison the
entire convergence transaction.

## Deployment and rollback

The local/default behavior remains `all`. Kubernetes selects `api` and
`worker` explicitly; `app.worker_main` refuses to run without the `worker`
role, and Uvicorn refuses the `worker` role. This fail-closed composition
prevents accidental duplicate durable workers.

Rollback is a manifest rollback to the previous single-container Pod. The
claim-lifecycle migration is additive, and the default all-in-one entrypoint
understands the new columns, so no destructive schema rollback is required.

## Remaining gates before the target topology

This foundation deliberately keeps `replicas: 1`, `strategy: Recreate`, and
the API PVC mount. The following are not claimed complete:

1. a networked single-writer gitd and fail-closed `gitd_url` composition;
2. removal of every API-side Bare-Git write/read dependency and the API PVC;
3. drift detector and operator repair/restore drill;
4. MCP multi-replica session ownership or routing;
5. publication throttling and audit-chain authority suitable for API replicas;
6. API `RollingUpdate` and horizontal replica increase.

Until those gates are implemented and exercised, raising Uvicorn workers or
Deployment replicas remains unsupported.
