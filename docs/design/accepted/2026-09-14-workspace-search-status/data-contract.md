# Search-update observation contract

Status: accepted and implemented; companion to [the interface design](README.md).

## 1. Current wire contract and units

Use only `GET /health/vault/{name}` after authenticated Vault discovery.
The [route](../../../../backend/app/main.py) requires reader access. Its
[aggregator](../../../../backend/app/services/health.py) exposes these stages:

| Existing field | Unit / scope | Semantics |
| --- | --- | --- |
| `vector_store.backfill.upsert` | Stored search chunks | `pending` excludes terminal `abandoned`; `retrying` is a subset of pending; `indexed` is current indexed chunks, not documents |
| `metadata_backfill` | External-Git documents awaiting automatic metadata | Pending excludes terminal abandoned; retrying is a subset of pending; no runtime-enable metadata |
| `native_file_projection` | One mutable latest desired-state intent per File | Pending includes retrying and exhausted; abandoned is terminal and outside pending |
| `native_derived` | Resource-revision invalidation intents | Pending includes retrying and exhausted; terminal outcomes include historical revisions; not a current-resource counter |

Both Native stages expose `status` (`ok`, `reconciling`, `degraded`), `pending`,
`retrying`, `exhausted`, and `abandoned`. Derived also exposes outcome totals
(`applied`, `superseded`, `deleted`, `direct_grep`); these are not completion
denominators. `exhausted` is at the final retry/lease boundary, not proof of a
terminally failed final attempt. Its distinction from abandoned must survive UI
normalization. The current backend marks either as degraded.

No returned identifiers deduplicate File projection → derived intent → chunks.
Never add these stage counts. A single Resource can contribute at several
stages or revisions. `pending + retrying` is wrong for every current stage;
`pending - abandoned` is wrong for current chunk/metadata stats.

The per-Vault surface does **not** expose vector-delete backlog (its outbox cannot
be scoped through already-deleted chunks), vector-driver reachability, dense
embedding coverage, or a guarantee that every resource kind is searchable.
Do not fetch global `/health` or `/stats` to fill those gaps. Badges and Operations
describe **reported search-update queues**, not end-to-end freshness or service health.

Sources: [embed stats](../../../../backend/app/services/embed_worker.py),
[metadata stats](../../../../backend/app/services/metadata_worker.py),
[File projection](../../../../backend/app/services/native_file_projection.py), and
[derived intents](../../../../backend/app/services/native_derived_worker.py).

## 2. Small additive backend change

Keep the route, authentication, existing keys, and their current meanings intact.
Add optional `search_update_status` version 1. No migration, stored new counter,
health polling service, worker-behavior change, or new aggregate endpoint.

Illustrative JSON showing the new envelope only; existing keys remain alongside it:

```json
{
  "search_update_status": {
    "version": 1,
    "observed_at": "2026-09-14T03:00:00Z",
    "stages": {
      "search_index": {
        "mode": "enabled",
        "unit": "chunks",
        "scope": "stored_chunks",
        "pending": 3,
        "retrying": 1,
        "abandoned": 0
      },
      "file_projection": {
        "mode": "enabled",
        "unit": "file_updates",
        "scope": "latest_file_intents",
        "pending": 0,
        "retrying": 0,
        "exhausted": 0,
        "abandoned": 0
      },
      "content_preparation": {
        "mode": "enabled",
        "unit": "revision_updates",
        "scope": "current_heads",
        "pending": 2,
        "retrying": 0,
        "exhausted": 0,
        "abandoned": 0
      },
      "metadata": {
        "mode": "disabled",
        "unit": "documents",
        "scope": "external_git_documents",
        "pending": 0,
        "retrying": 0,
        "abandoned": 0
      }
    }
  }
}
```

Version 1 fixes the four known stage identities and these rules:

- `mode` is intended pipeline configuration (`enabled`, `disabled`,
  `not_applicable`, `unknown`), **not worker liveness**. Do not expose settings
  values, URLs, credentials, internal exceptions, or infrastructure names.
- Counters are optional nonnegative integers. Missing/null means unknown, never
  zero. A stage object stays present even if its counters cannot be observed.
  Unavailable envelope-only reads may expose only `mode`, `unit`, and `scope`.
- Decoder checks include safe-integer bounds, known units/scopes, and versioned
  subset invariants (`retrying <= pending`, `exhausted <= pending`, and, for Native
  stages, `retrying + exhausted <= pending`). Invalid combinations become unknown,
  not repaired by clamping. An unrecognized stage also limits coverage rather
  than silently certifying a complete catalog.
- `pending` excludes terminal abandoned; retrying/exhausted are subsets of pending.
  Exhausted applies only to the Native stages. No client arithmetic invents an
  “actively running” count from these values.
- `search_index`, `file_projection`, and `content_preparation` describe core
  preparation. `metadata` is auxiliary enrichment, never a blocker of all search.
- An enabled core stage needs known pending/abandoned and, for Native stages,
  exhausted values before it can be reported clear. Disabled core work with a
  positive outstanding count is paused, not clear; disabled optional metadata
  is informational. Unknown mode is not evidence of a stopped worker.
  `not_applicable` is reserved for a genuinely inapplicable, empty stage; a
  disabled consumer with retained work must report disabled instead.
- `observed_at` is observation time, not last successful indexing or a pipeline
  watermark. Queries are not one transactional end-to-end progress snapshot.
- Indexed/outcome totals remain available in the existing fields; the proposed
  envelope deliberately does not add completion percentage or document totals.

### Do not let a historical failure become a permanent current warning

Raw `native_derived.abandoned` includes historical revisions. The new
`content_preparation` counters must join the namespace-scoped intent to
`native_resources` and require
`intent.revision_id = resource.head_revision_id`. Keep delete Heads eligible:
failure to propagate a deletion can also leave search stale. Do not filter them
away merely because the Resource lifecycle is deleted. Count at the intent unit;
do not silently replace the existing operational ledger totals.

The old raw counters remain unchanged. If an old failed revision has been
superseded by a successfully applied Head, the current-head summary can clear
while the operational history remains visible. Test this explicitly. If the
extra read fails, leave this stage unknown; never fall back from a missing
current-head count to a historical count while claiming current scope.

This is the only new count query required. It must remain Vault-scoped, use the
existing resource/revision keys, and be measured on a large intent ledger before
shipping. An envelope-only failure must not break otherwise successful legacy
health fields. Existing failures of the original stats queries may retain the
current non-2xx route behavior; the frontend handles them as unavailable.

### Configuration modes must match execution gates

Derive modes with shared backend capability predicates, not guessed frontend
settings or copied, divergent predicates:

- Embedding-worker startup is unconditional. Missing `embed_base_url` selects
  sparse-only indexing; it does **not** disable the search-index stage.
- Metadata startup currently requires canonical Bare Git, external Git enabled,
  an LLM base URL, and either an LLM API key or platform-hard model governance.
  Its worker also guards external-Git disablement during processing.
- File projection executes under raw `postgres_native` selection.
- Native derived execution includes `postgres_native`, `native_ledger_m1`, and
  the existing dedicated M1 file-measurement condition. Preserve these actual
  gates; do not accidentally “simplify” runtime behavior for a UI feature.
- Counters can still exist after configuration disables their consumer. Preserve
  them in details and explain the mode; do not imply an empty or healthy queue.

Extraction/reuse of these predicates must be behavior-preserving, covered by
gate tests, and need no change to configuration files or deployment defaults.

## 3. Frontend normalized model

Keep three independent dimensions: **observed work**, **coverage**, **freshness**.
Do not encode all of them as one `pending` number or one `incomplete` boolean.

```ts
type ObservationState = 'checking' | 'fresh' | 'stale' | 'unavailable';
type Coverage = 'complete' | 'partial' | 'unsupported';
type WorkState = 'clear' | 'updating' | 'attention' | 'paused' | 'unknown';

interface VaultSearchObservation {
  vaultId: string;
  vaultName: string;
  observation: ObservationState;
  coverage: Coverage;
  receivedAt?: number;
  attemptedAt?: number;
  stages: StageObservation[];
  core: WorkState;
  auxiliary: WorkState;
  historicalFailureReported: boolean;
}
```

`StageObservation` retains stage identity, source version, mode, unit, evidence
scope, optional counters, and normalized work state. Its discriminated schema
must prevent “current_heads” from being assigned to legacy intent totals.
`receivedAt` is the client timestamp of a valid response; `attemptedAt` updates
on refresh attempts. A failed request never refreshes `receivedAt`.

Retain **sets of Vault IDs** for internal scheduling and normalized state:
observed updating, confirmed current attention, paused, unknown/stale, and accessible.
These affected-Vault sets do not supply the visible badge count.
Transport success alone is not complete stage coverage. Metadata absence does
not block core coverage when versioned metadata is explicitly not applicable or
disabled; unknown required core stages do.

The badge selector sums only fresh, valid `search_index.pending` chunks.
Versioned stages must be enabled; legacy counts remain usable without mode
metadata. Omit stale, unavailable, invalid, paused, and unknown-mode versioned
observations. Do not add retries, subtract abandoned, or include another stage.
The global header selects all accessible observations; Overview selects its own
Vault. Unknown indexing coverage produces a known positive subtotal formatted
`N+ indexing`, with title and screen-reader text explaining “at least” and the
missing counts. If no positive count is known, render no badge; absence never
asserts healthy search or complete indexing.

Full stage detail remains only in Vault Settings → Operations. Stale values are
explicitly last-known, not current totals. Historical-only failures appear as
`Recorded processing failures` with an explicit “current impact is not available”
explanation, not as a current missing-document count. There is no global status
panel, Home processing notice, or attention-priority header label.

## 4. Old-server and failure behavior

| Response | Interpretation |
| --- | --- |
| Valid version 1 | Use envelope semantics; validate each stage independently |
| Envelope absent, recognizable raw fields | Conservative legacy adapter; retain all four known stages that exist; mode/scope completeness may remain unknown |
| Older chunk/metadata shape | Show reported stage data only; do not infer absent Native support or worker mode from zero/missing fields |
| Unknown future envelope version | Do not apply v1 semantics; use recognizable raw fields with limited coverage, otherwise unavailable |
| Missing/null/invalid count | Omit that count; mark required observation incomplete |
| 200 with HTML / malformed JSON / no recognized stage | Status unavailable; possible proxy/contract error, not idle |
| 401 | Hand off to existing session revalidation; clear private observation state |
| 403 | Remove that Vault's cached detail immediately and refresh accessible scope |
| 404 | Refresh directory: removed Vault leaves scope; still-listed Vault is unavailable, possibly unsupported; do not guess which from status alone |
| 405 / 501 | Mark endpoint unsupported for the session, with explicit recheck; no repeated 15s request storm |
| Network error / timeout / 429 / 5xx | Retain authorized last-good data as stale; respect Retry-After and back off |

Legacy `pending` semantics may differ between deployments. Never subtract
abandoned or add retrying based on a numeric heuristic. Positive pending can be
labelled **reported queued work**, not confirmed active processing. With pending
and abandoned both positive and no version guarantee, avoid asserting their
relationship. Native historical abandonment remains a recorded warning only.

Unversioned all-zero replies hide the badge; Operations retains the legacy
qualification rather than saying `All up to date`. A missing API is not
“coming soon” unless support is actually scheduled; use `This server does not
provide detailed status` only when the contract is confirmed unsupported.

## 5. Shared observation lifecycle

Use the existing QueryClient plus **one authenticated observation owner**.
Global header, Overview, and Settings select from that cache; the
old independent timers and UI arithmetic are removed as part of integration.

- Key by backend origin, identity/session generation, and stable Vault ID.
  Keep credentials out of keys. Use the existing authenticated fetch carrier.
- Resolve accessible Vaults through the existing directory; preserve its full
  pagination contract if it changes. No data fetch before authentication proof.
- A maximum of five health requests runs concurrently. Each has an 8s timeout;
  a sweep has a 60s deadline. No overlapping sweeps or duplicate current-Vault
  requests; consumers and manual refresh join in-flight requests.
- Use existing 15s active / 60s idle intervals, scheduled after completion.
  Pause background polling when the tab is hidden/offline. On foreground or
  reconnect, revalidate identity/scope before publishing cached names or counts.
- Cap retained “fresh” observations at 120s from receipt; failed refresh makes
  affected data stale immediately. Do not age-reset successful rows merely
  because another Vault replied. Unchecked rows and deadline overruns remain
  incomplete indexing coverage. Resume the next sweep with unchecked Vaults
  without starving the remainder; there is no panel-driven priority selection.
- Initial scope failure is unknown, not an empty list. On a failed scope refresh,
  current-workspace totals lose their completeness claim. Keep last-known rows
  only within the still-verified session; following session/scope invalidation,
  hide private rows until verification succeeds.
- Any account switch, logout, QueryClient identity reset, or access revocation
  aborts old work, advances the generation, and removes affected observations.
  Late responses must match both identity and scope generations before publish.
- Coalesce refresh requests while busy and prevent rapid repeated requests. They
  are GET-only. Known unsupported endpoints recheck only explicitly or after a new
  authenticated session, rather than polling continuously.
- No localStorage/IndexedDB persistence of status, names, or failures. No panel,
  focus-return, or incident-dismissal state. No new analytics or background automation.

The deadline/freshness bounds may result in partial coverage for very large
accounts. Say so rather than inventing a complete total. A bulk reader-gated
endpoint is a separate optimization only if measured load justifies it; it is
not a prerequisite or an implicit promise of this design.

## 6. Permissions and recovery boundary

Every reader can view the status of an accessible Vault. The header and Overview
badges are passive; Operations provides read-only details, not recovery controls.

`native_file_projection.requeue_abandoned()` is an internal service method, not
a reader-authorized HTTP recovery endpoint. Its explicit namespace argument
does not itself enforce user permissions. Native-derived has no matching public
requeue API. Do not wire either through an invented frontend route or imply
refresh will restart processing. Recovery tooling needs a separate authorization
and safety design.

## 7. Contract tests required before implementation is complete

- Preserve existing keys and reader access tests in
  [test_security_edge_e2e.sh](../../../../backend/tests/test_security_edge_e2e.sh).
- Extend [health aggregation tests](../../../../backend/tests/test_stats_endpoint_unit.py)
  with envelope shape, nullable/absent observations, and old-field preservation.
- Verify current subset semantics against
  [embed stats](../../../../backend/tests/test_embed_worker_stats_unit.py),
  [metadata](../../../../backend/tests/test_metadata_worker.py),
  [derived stats](../../../../backend/tests/test_native_derived_worker_unit.py), and
  [projection](../../../../backend/tests/test_native_file_projection_pg.py).
- Current-head derivation: old abandoned + new applied does not warn; current
  abandoned warns; current delete failure remains represented; cross-Vault
  intents never enter the aggregate; unknown current-head read never means zero.
- Configuration modes match worker gates, including sparse-only indexing,
  optional metadata, Native and M1 variants, without exposing secret values.
- Frontend fixtures cover every compatibility row plus invalid integers,
  mixed units, duplicate Vault membership across stages, stale data, request
  races, timeout bounds, manual refresh coalescing, and auth/scope reset.
- Prove badge counts use only fresh pending chunks, preserving legacy support,
  partial-subtotal wording, and stale/paused omission. Header and Overview share
  observations, not independent component requests. No fallback request to global
  health or the stats port.

See the main design's verification record for the badge rendering, responsive,
accessibility, and isolated local verification scope.
