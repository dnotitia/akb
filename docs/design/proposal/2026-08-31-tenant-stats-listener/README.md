---
status: proposal
stage: implementation
created: 2026-08-31
updated: 2026-08-31
---

# Tenant Stats Listener

## Context

A control plane operating many AKB tenants needs coarse inventory per tenant —
how much storage it holds, how large its corpus is, how much it was used
yesterday — to drive a customer dashboard and capacity work. It does not need,
and must not have, access to the tenant's knowledge.

AKB's existing surfaces cannot supply that split. `/health` and `/readyz` are
operational probes with no inventory in them, and every path that does know the
inventory sits on the API port behind tenant authentication. Putting a stats
path on the API port would mean the boundary between "may read counters" and
"may read documents" is a line of application code — the wrong layer for a
guarantee the platform has to make about a workload it hosts for someone else.

A metrics exporter was also rejected. It would add a Prometheus dependency to
the tenant image for one consumer, and its natural read-on-scrape shape turns a
misconfigured poller into database load.

## Decision

Serve a cached inventory snapshot as plain JSON at `GET /stats` on a **second
listener in the serving process**, off unless an operator configures a port.

**Separate port, no authentication.** The enforcement layer available to the
platform is a Kubernetes NetworkPolicy, which selects on port. Reachability is
therefore the authorization: the socket is not bound unless `stats.port`
(app.yaml) or `AKB_STATS_PORT` is set, and once bound the policy decides who
may connect. There is no credential to distribute, rotate, or leak into a
monitor's configuration. This is only sound because the payload is aggregate
counters — no vault names, no document titles, no actor identities — and
because the platform treats a missing policy as a deployment blocker. **Do not
bind this port where no such policy restricts it.**

`AKB_STATS_PORT` is the second deliberate environment-variable exception in
this codebase, alongside `AKB_PG_POOL_MIN_SIZE`/`MAX_SIZE`. Same reason: the
control plane provisions the port per tenant and renders the matching
NetworkPolicy from the same value, so it must be settable without re-rendering
the tenant's YAML. `stats:` in app.yaml remains the source of truth for
everything else about the surface.

**Requests never compute.** A sampler writes the snapshot every
`stats.sampler_interval_secs` (default 300) and the endpoint returns that
cache. Callers get 503 until the first sample; after that a failed sample keeps
serving the last good snapshot, and the payload's own `computed_at` is how a
consumer sees staleness. Consumers should poll at half the sampler interval or
faster — polling at the same period drops a snapshot whenever the two phases
drift apart, and reading the cache is free.

The listener runs in the serving process rather than a sidecar: the snapshot is
computed against the serving pool, and a sidecar would need its own database
credentials and its own copy of the schema knowledge.

## Contract

`app/stats/schema_v1.json` is the canonical contract. **The platform vendors
that file at a pinned version**; it ships inside the backend wheel, so it is
also readable from the running image at `app/stats/schema_v1.json`.
`golden_v1.json` and `golden_v1_unmeasured.json` are the fixtures both sides
test against.

Three properties the payload is built around:

- **Absent is not zero.** Every numeric field is nullable and optional, and a
  value that cannot be measured is omitted rather than defaulted. `0` is a
  measurement; a consumer must be able to tell "no files" from "we could not
  measure the files". This currently applies to `vector_bytes` (the vector
  index is not in this database), `file_bytes` (some confirmed file has no
  recorded size — a partial sum reads as a total), and `distilled_doc_count`
  (see below).
- **Corpus is one snapshot; storage is not.** The corpus counts come from a
  single REPEATABLE READ transaction, so a subset can never exceed its
  superset. Physical sizes are collector-maintained approximations with no
  relationship to a transaction snapshot, so `storage` carries its own
  `observed_at` watermark instead of borrowing the corpus timestamp.
- **Activity is a closed fact.** `activity` covers the previous complete UTC
  day, folded once into `tenant_activity_daily` (migration 087) five minutes
  after the day closes and served from that row forever. A consumer keeps the
  first value it sees for a window, so a restart must not be able to publish a
  different number for it. A window whose volume was unmeasurable — usage
  tracking was off — is stored with NULL counts, not 0: the fold is permanent,
  so a fabricated zero could never be corrected.

`distilled_doc_count` is **omitted**: this repository has no marker recording
that a document was produced by distillation, and the cross-repo design left
the criterion to us. Deriving it from `vault_write_policy.managed_by` was
rejected — it counts hand-written documents in a managed vault, and
re-pointing a vault reclassifies its history. The accepted direction is an
explicit per-document marker written by the distillation path, which spans this
repository and the gardener and is therefore out of scope here.

## Compatibility

Additive. Nothing is composed when no port is configured: no socket, no
sampler, no change to any existing surface. `/health` gains a top-level
`oldest_pending_enqueued_at` (the enqueue time of the oldest chunk still
awaiting indexing, omitted when the queue is empty), also available per vault
under `vector_store.backfill.upsert` — the top-level key is the contract term
and must not depend on the shape of the operational block beneath it.

`schema_version` is bumped only for a breaking shape change; an unrecognised
version makes a consumer reject the whole snapshot rather than read the fields
it knows. An additive optional field may ship producer-first.

**Known note:** the two surfaces format timestamps differently — `/stats` emits
`2026-08-21T09:00:00Z` at second precision, `/health` emits this repository's
existing `.isoformat()` (`+00:00`, microseconds). Both are RFC 3339 and both
parse; unifying them is not planned.

## Acceptance criteria

- No port configured (neither `stats.port` nor `AKB_STATS_PORT`) leaves the
  process unchanged; a malformed `AKB_STATS_PORT` fails the boot rather than
  silently disabling the surface.
- The second listener binds, answers `/stats`, and serves nothing else — API
  paths 404 on that port — while leaving the process signal handlers alone, so
  SIGTERM still reaches the API server.
- `/stats` returns 503 before the first sample, the cached payload after it,
  and the same payload while sampling fails.
- A poll never reaches the database.
- Payloads and both golden fixtures validate against `schema_v1.json`, and the
  schema itself marks every numeric field nullable and optional.
- An unmeasurable field is absent from the payload, never 0.
- A folded activity window is unchanged by a restart that would compute it
  differently, and is stored with NULL counts when usage tracking is off.
