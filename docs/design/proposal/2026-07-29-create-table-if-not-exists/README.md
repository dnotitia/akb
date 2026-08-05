---
title: akb_create_table — opt-in idempotent create (`if_not_exists`)
status: implemented
stage: proposal
created: 2026-07-29
updated: 2026-07-29
reviews:
  - feedback/2026-07-29-codex-round1.md
  - feedback/2026-07-29-codex-round2-impl.md
  - feedback/2026-07-29-codex-round3-impl.md
  - feedback/2026-07-29-codex-round4-impl.md
  - feedback/2026-07-29-codex-round5-impl.md
  - feedback/2026-07-29-codex-round6-impl.md
  - feedback/2026-07-29-codex-round7-impl.md
  - feedback/2026-07-29-codex-round8-impl.md
  - feedback/2026-07-29-codex-round9-impl.md
  - feedback/2026-07-29-codex-round10-impl.md
  - feedback/2026-07-29-codex-round11-impl.md
  - feedback/2026-07-29-codex-round12-impl.md
  - feedback/2026-07-29-codex-round13-final.md
---

# `akb_create_table(if_not_exists=true)` — opt-in idempotent create

> Revised across thirteen Codex rounds; every finding is folded in below. See
> `feedback/`.

## Problem

`akb_create_table` has no idempotent mode. A caller that wants "make sure this
table exists" has one option: call create, catch the 409, and infer that the
table already exists.

Production tool-usage data (2026-07-29, first ~110 minutes after enabling
`tool_usage`) shows this is the dominant use of the tool:

```
tool               calls   errors   code       followed by
akb_create_table     461      461    conflict   alter_table 66.6% / browse 33.4%
akb_alter_table      308        0    —          —
```

Every `akb_create_table` call in the window returned 409; every one was followed
in the same session by a successful call. A representative caller is a job that
runs every 5 minutes, 29 calls, ~50s, whose steps 2–7 are three create/browse
probe pairs.

The caller is using an error response as a return value.

## Why this is worth fixing

1. **The recovery branch is a guess, not a contract.** It works because someone
   inferred that `conflict` means "already exists" from the error string. AKB
   never promised that; any new client must rediscover it.
2. **Ambiguity the caller cannot resolve.** A `conflict` after a timeout could
   mean the caller's own attempt won, or another actor's did.
3. **The metric is destroyed.** 100% failure on a healthy tool cannot be alerted
   on, and a real defect in the same tool would be indistinguishable from the
   routine 461.
4. **TOCTOU.** `find_by_name` then create is check-then-act; the pre-check can be
   lost to a concurrent creator.

## Internal precedent

| Tool | Name collision behaviour |
|---|---|
| `akb_create_collection` | **Idempotent** — returns current row state, `created=False` |
| `akb_put` | **Auto-avoids** — appends `-shortid` to the slug and creates |
| `akb_create_table` | **409 Conflict** |

`collection_service.py:137` states the response contract explicitly: *"an
idempotent re-create against an existing collection with 5 docs surfaces
doc_count=5, not 0."*

**Not** a precedent for events: `create_collection` emits `collection.create`
unconditionally, including on the no-op (`collection_service.py`). This
proposal deliberately diverges (see Events below).

## Proposal

Add an optional `if_not_exists: bool = False` to `akb_create_table` (MCP) and
`POST /api/v1/tables/{vault}` (REST, `tables.py`).

```
if_not_exists=false (default)
    table absent  -> create
    table present -> 409 ConflictError            (unchanged)

if_not_exists=true
    table absent  -> create           -> created=true
    table present -> no write at all  -> created=false
```

The flag changes the request, not the reporting: without it the caller says
"create this table"; with it, "ensure this table exists". This is why SQL spells
them `CREATE TABLE` and `CREATE TABLE IF NOT EXISTS`.

### Concurrency: take a lock, do not recover from the race

Codex round 1 established that the original design — catch
`asyncpg.UniqueViolationError` and re-read the committed row — **is not
implementable**. The exception surfaces while the outer `conn.transaction()` is
still active, so PostgreSQL is in an aborted state and every subsequent query on
that connection fails until rollback.

There are also more race outcomes than the two originally listed. A concurrent
winner can commit:

| Window | Loser observes | Raised as |
|---|---|---|
| before `find_by_name` | registry row present | (the intended `created=false`) |
| between `find_by_name` and the physical preflight | `to_regclass` non-null | `ConflictError` (fusion message) |
| during `CREATE TABLE` | `asyncpg.DuplicateTableError`, or a `pg_type` uniqueness failure | `ConflictError` |

Only the first is a decision; the two later ones are unrecoverable, because by
the time they surface the transaction is aborted.

Note the registry insert is **not** a third post-check window for the same
logical table: once one transaction has created the physical table, the other
cannot reach the insert — its `CREATE TABLE` fails first. The
`UniqueViolationError` arm around the insert is defensive, covering a registry
row that arrived by some other route.

**Resolution: serialize on the logical identity rather than classify each race
outcome after the fact.** Take a transaction-scoped advisory lock keyed by `(vault_id, name)`
before the existence check, following the existing precedent in
`table_migration_service.py:240`:

```python
async with conn.transaction(isolation="read_committed"):
    ...
    await conn.fetchval(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        f"table-create:{vault_id}:{name}",
    )
```

Two details the first draft got wrong, both caught in review:

* **The key is domain-prefixed.** `table_migration_service` calls the same
  function with an *unprefixed* `f"{vault_id}:{key}"`, so a migration key equal
  to a table name would alias onto exactly this lock. Harmless for correctness
  — the two paths each take one such lock and migration's `alter_table` takes
  no second one, so no deadlock — but it is needless blocking.
* **The transaction pins `read_committed`.** The lock only helps if the loser
  can *see* the winner's row after the wait. Under `REPEATABLE READ` the vault
  lookup would fix a snapshot *before* the wait, so the loser would still miss
  the winner and fall through into the constraint violation it cannot recover
  from. The pool sets no isolation level (`app/db/postgres.py`), so a
  deployment-level default must not be able to break this.

This makes both later windows impossible for the same logical table, so only
the `find_by_name` check is left to decide — which is the one that should. It also closes the TOCTOU listed as
cost 4 above — something the caller's catch-409 pattern cannot do, because on
seeing a 409 it has no way to know whether its own transaction or another's won.

If a uniqueness failure still surfaces (a different logical table fusing onto the
same physical name), it **must remain a conflict** — see Security.

`grant_table_in_conn` and `emit_event` stay winner-only inside the successful
transaction, and a no-op must return before the post-commit metadata indexing
(`index_table_metadata`, outside the create transaction).

### Security: `if_not_exists` must never cross a vault boundary

Two separate concerns, both load-bearing.

**(a) Cross-vault physical-name fusion (#285).** Vault and table names are joined
with `__`, and hyphens in vault names map to underscores, so vault `a--b` table
`c` and vault `a` table `b__c` both resolve to `vt_a__b__c`. `table_service.py`
raises `ConflictError` via `_physical_name_conflict_message()` for exactly this, and that function's docstring already treats the raw PG
error as a disclosure — it *"confirms another tenant's physical table by name"*.

**Every one of those sites raises the same `ConflictError` type.** An implementation
that broadly catches `ConflictError` when `if_not_exists=true` would convert a
cross-vault fusion into `created=false` and then return the other tenant's
schema.

> **Invariant.** `if_not_exists=true` suppresses the conflict **only** when a row
> with the exact `(vault_id, name)` exists. Every other conflict — physical-name
> fusion, `to_regclass` collision, `DuplicateTableError`, uniqueness violation
> without a matching logical row — raises unchanged.

Measured evidence that this boundary is real: PostgreSQL's `information_schema`
is privilege-filtered and AKB issues per-vault PG roles. A writer role for a
vault holding 61 tables sees exactly those 61 of the 518 `vt_` tables on the
cluster, and 670 columns, all its own. The other 457 tables are invisible to it.
So leaking another vault's columns here would be a genuine new disclosure, not a
restatement of something already reachable.

**(b) Write scope does not imply read scope.** The original draft argued the
enriched response discloses nothing because "a writer could learn the same from
`akb_browse`". That is **wrong**. `token_has_scope` is
`"admin" in granted or required in granted` (`auth_service.py:213`) — there is no
implication. `akb_create_table` is `_WRITE_SCOPE` and `akb_browse` is
`_READ_SCOPE` (`server.py:205`, `:232`), so a write-only token can create tables
and cannot browse. Managed-vault wildcard grants can likewise authorize a write
before reader membership is checked (`access_service.py:254`).

Existence itself is already disclosed by success-vs-409 in the current strict
create, so the flag adds no existence bit. But **the stored schema, collection
and URI are new disclosure to a write-only caller.**

Resolution: the enriched no-op body requires read authority, resolved at the
REST/MCP authorization boundary and handed to the service as a capability
(`can_read_existing`), never as raw scopes. It defaults to `False`, so a caller
that forgets to pass it gets the minimal envelope rather than the stored schema.
A caller with write but not read receives exactly:

```jsonc
{"kind": "table", "name": "issues", "created": false, "outcome": "already_exists"}
```

`matches_request` and `mismatches` are withheld along with the schema: they are
themselves schema oracles — repeated probing reconstructs the stored schema
without it ever being returned.

Both boundaries must consult **both** scope systems. `token_has_scope(None, …)`
returns True by design — `None` means an unscoped credential, i.e. a JWT login —
but an OAuth credential *also* carries `token_scopes=None` and keeps its grants
in `oauth_scopes`. A gate that checks only the former waves through an OAuth
token holding nothing but `akb:vault:write`. (The REST helper had exactly this
defect in the first implementation; Codex round 3 caught it.)

Separately and out of scope here: REST's `get_current_user` does not enforce
OAuth scopes at all (`app/api/deps.py`), so a read-only or zero-scope OAuth
token with writer membership can currently POST mutations. That predates this
change and needs its own fix.

(Alternatives considered: define write as implying read, or require read scope
to pass the flag at all. Both are larger changes to the scope model.)

### Response shape

Codex round 1: returning stored state is necessary but insufficient — a
success-looking `created=false` still invites the caller to assume its requested
schema was ensured. Divergence must be machine-explicit:

```jsonc
{
  "kind": "table",
  "uri": "akb://myvault/coll/x/table/issues",
  "vault": "myvault",
  "name": "issues",
  "created": false,
  "outcome": "already_exists",
  "matches_request": false,
  "mismatches": ["collection", "columns"],
  "columns":     [...],   // the STORED schema, not the request's
  "unique_keys": [...],
  "indexes":     [...]
}
```

* Comparison is **exact canonical equality**, never "compatible" — anything
  looser reopens schema-diff policy, which is explicitly out of scope.
* Comparison covers `columns`, `unique_keys`, `indexes`, `collection` and
  `description`. Each mismatching field is named in `mismatches`.
* Stored JSONB is normalized through the repository parsing helpers, including
  legacy string rows (`table_registry_repo.py:222`), before comparison.
* `created` appears on `created=true` responses too, so its absence is never
  meaningful.
* The no-op canonicalizes the request through the **same** helper the real
  create uses (`_canonical_create_spec`: normalize columns, resolve inline and
  explicit keys/indexes, reject duplicate metadata names) and additionally runs
  `_validate_column_references`. Sharing it is what makes `matches_request`
  meaningful — two independently-derived canonical forms could report a
  mismatch the real create would never have produced, or a match it would have
  refused. It also means a malformed spec is rejected identically whether or
  not the table exists — "ensure a table matching this spec" is not satisfiable
  by an invalid spec. (A bad column/key/index spec is a `ValidationError` →
  422; a reference to a table missing from the vault is `AKBError(400)`.)
  The **strict** path deliberately keeps its original order (409 before column
  validation), so which ERROR a caller gets is unchanged — an existing table
  is still a 409 before any spec validation runs. Its success envelope does
  gain `created: true`; see Compatibility.
* **Comparison drops no-op spellings.** `required: false` and omitting
  `required` describe the same column, but normalization keeps whichever
  spelling arrived — so a stored row created with one form reported a
  spurious mismatch against a request using the other. `_comparable()`
  strips those before comparing, on BOTH sides and **never** on what is
  stored. The rule is per field: `False` counts as absent for
  `required`/`unique`/`index`, but for `default` only NULL does —
  `default: false` on a boolean column is a real `DEFAULT FALSE` and a
  genuine difference. Comparison is also **type-tagged**: Python says
  `False == 0` and `True == 1`, but JSONB and the generated DDL do not, so
  a plain `==` would hide that divergence — including nested inside a
  `check` spec. Explicit `check`/`enum`/`references`/`on_delete` nulls are
  stripped as absent, since REST accepts them and they generate the same
  DDL as omission. A false mismatch is worse than no signal: it sends
  the caller to alter a table that already matches.
* **Column normalization runs exactly once.** It is NOT idempotent: it
  synthesizes a `CHECK` for an enum column and then rejects a column that
  already carries one (*"Enum columns derive their CHECK constraint from
  `enum`; omit `check`"*). Sharing the canonicalizer naively therefore turned
  **every enum create into a 422** — a regression introduced while fixing
  `matches_request`, found by Codex round 4 and pinned by
  `test_real_create_with_enum_column_is_not_double_normalized`.
  `_canonical_create_spec(normalize_columns=False)` is how the real path opts
  out; the no-op keeps the default because it holds raw caller input. Codex
  round 5 verified both branches produce identical canonical tuples for enum
  plus inline and explicit key/index inputs.

### Events

No `table.create` on a no-op. Domain events represent committed changes, and
`tool_usage` already records the attempted command. This diverges from
`create_collection`, which emits unconditionally — that is an inconsistency in
the existing code, not a precedent to copy.

A no-op must additionally not auto-create the requested collection, must not
overwrite or re-index metadata from the losing request, and must emit at most
one `table.create` under concurrency.

## Compatibility

Requests are fully compatible: the default is `false`, so existing callers keep
the current behaviour including the 409, and the production catch-409 client
needs no change.

Responses are **additive, not identical** — every `create_table` response gains
`created` (and the no-op branch gains `outcome` / `matches_request` /
`mismatches`). Adding a field to a JSON response is conventionally safe: JSON
parsers ignore keys they do not know, and the generated TypeScript envelope
already carries an index signature. (An earlier draft justified this with
`extra="allow"` on the request models — that governs request PARSING and says
nothing about response compatibility. `CreateTableRequest` inherits `NFCModel`
in any case.) The earlier claim that behaviour is unchanged "bit for bit" was
an overstatement.

The MCP flag is **type-strict**: a non-boolean is rejected with
`invalid_argument` rather than coerced. Both lenient and strict casts mislead —
`bool("false")` is `True`, so a lenient cast grants idempotent behaviour the
caller never asked for, while a strict `is True` silently gives `"true"` the 409
it did not expect.

Contract surfaces updated in the same change:

* `backend/mcp_server/tools.py` — MCP tool schema and description
* `backend/app/openapi_contract.py` — static `AkbTableEnvelope`
* `backend/tests/test_tables_route_unit.py` — route forwarding assertion
* `backend/tests/test_vault_table_name_collision_unit.py`,
  `test_table_name_length_unit.py` — fakes now see the advisory lock and the
  `isolation` kwarg
* `backend/mcp_server/help.py` — the `akb_create_table` help topic
* `packages/akb-client/test/fixtures/openapi.core.json` +
  `src/core/schema.gen.ts` — `if_not_exists?: boolean` on `CreateTableRequest`
* `packages/akb-client/src/core/openapi-codegen.ts` — the hand-written
  `AkbTableEnvelope` generator gained `created`, `outcome`, `matches_request`
  and `mismatches`. Without this the response fields a caller needs to USE the
  feature were reachable only through the `[key: string]: unknown` catch-all.

Note the checked-in OpenAPI fixture has drifted from the live spec well beyond
this change (regenerating it wholesale pulls in 68 new schemas, 26 changed ones
and 2 new paths). Only the field this change introduces was added, so the diff
stays reviewable; refreshing the fixture is its own piece of work.

## Tests

`backend/tests/test_table_if_not_exists_unit.py` (40 cases), DB-free:

* Default and explicit `if_not_exists=false` on an existing table still raise 409.
* Absent → creates, `created=true`. Present → `created=false`, no DDL, no
  `table.create` event, no post-commit metadata indexing.
* Divergent `columns` / `unique_keys` / `indexes` / `collection` each appear in
  `mismatches`; `columns` reflect the STORED schema. Legacy JSON-string rows
  normalize rather than reporting a spurious mismatch.
* Malformed `unique_keys` and bad column references are 422 on the no-op path.
* **Cross-vault fusion with `if_not_exists=true` still raises 409, and the body
  carries no part of the other vault's schema, URI, or collection.**
* The advisory lock is taken before `find_by_name`, on both paths, with the
  `table-create:` prefix; the transaction pins `read_committed`.

`backend/tests/test_table_read_authority_unit.py` (17), both surfaces:

* An OAuth token holding only `akb:vault:write` is denied read authority; one
  holding `akb:vault:read` is granted it. Same for PAT `write` vs `read`.
* An unscoped JWT login is granted; a denied reader role is not.
* Non-boolean `if_not_exists` (`"true"`, `1`, `[]`, …) is rejected, not coerced.

`backend/tests/test_table_if_not_exists_e2e.py` (6), live PostgreSQL:

* Two concurrent `create_table(if_not_exists=True)` calls for the same
  (vault, table) both succeed with **exactly one** `created=true`, one registry
  row, one physical table and one `table.create` event.
* The advisory lock serializes same-key sessions, leaves different keys
  unblocked, and releases on rollback as well as commit.
* A re-read after a constraint violation raises
  `InFailedSQLTransactionError` — pinning the fact that made the original
  catch-and-reread design unimplementable.

Falsification (both verified): removing the advisory lock fails the concurrency
test 5/5 and passes again on restore; making the cross-vault branch return a
no-op fails both security tests and passes again on revert.

### Functional reproduction

Tests assert; this exercised the feature. A script drove the real service
against live PostgreSQL 16.13 and printed what a caller receives:

| # | Call | Result |
|---|---|---|
| 1 | absent, `if_not_exists=True` | `created=True`, table actually created (enum column included) |
| 2 | same spec again | `created=False`, `matches_request=True`, `mismatches=[]` |
| 3 | different spec | `created=False`, `matches_request=False`, `mismatches=['columns','description']`, returned columns are the STORED ones |
| 4 | `can_read_existing=False` | exactly `{kind, name, created, outcome}` |
| 5 | no flag | `ConflictError: Table already exists: issues` |

Database state after all five: **1** registry row, physical table present,
exactly **1** `table.create` event, and the actual columns are
`['id','title','state','created_by','created_at','updated_at']` — the
`headline` column requested in (3) was never created. Scenarios 2–4 are
no-ops and changed nothing; scenario 5 is a conflict, not a no-op.

## Priority

**Medium.** The original draft said "low, no user is blocked". Codex round 1
pushed back and is right: the data proves callers *recovered*, not that tasks
completed successfully or that nothing was slowed. It is systematic latency and
cost, and it destroys an operational signal outright — after nine hours of
production collection the count is **876 conflicts out of 897 calls (97.7%)**,
so the tool's failure rate cannot be read at all.

Still below the two live defects found in the same dataset and above backlog:

* `akb_search` — 75 calls, **mean 14.0s, max 35.5s**, zero failures. Root cause
  is now established independently: the vector working set (~23 GB) far exceeds
  `shared_buffers` (2 GB) on CephFS-backed storage, so cold blocks cost
  ~0.66–2.15 ms each and `track_io_timing` attributes 99.8% of a cold query to
  disk I/O wait. A latency problem that never fails is invisible to any error
  metric.
* A 5-minute scheduled job whose terminal `akb_sql` step fails every run against
  a missing table in another vault.

## Out of scope

Declarative reconciliation (`ensure_table`): if the table exists with different
columns this reports the divergence and alters nothing. Reconciliation needs a
destructive-divergence policy and cannot distinguish a rename from a
drop-plus-add, which is why `akb_alter_table` exposes `rename_column` as its own
operation.

## Status

**Implemented on `feat/create-table-if-not-exists`; Codex round 13 verdict is
"Ready to merge. No blockers remain." Awaiting PM sign-off.** Revised through thirteen Codex rounds (see `feedback/`), which found and
closed: an unimplementable transaction flow, an incorrect `matches_request`, a
REST gate that ignored OAuth scopes, a stale typed SDK, and a double-
normalization regression that broke every enum create.

Verification: 1059 unit tests, 6 live-PostgreSQL tests (also run against a
FRESH database, the CI condition), `ruff`, `mypy`, and the
`akb-client` `codegen:check` all pass. Both load-bearing guarantees are
falsified as well as asserted, and the feature was additionally exercised
end-to-end against live PostgreSQL 16.13 — see Functional reproduction.

Resolved since the first draft — the write-only-caller envelope is
`{kind, name, created, outcome}`, and request validation runs on the no-op path
while the strict path keeps validating *after* the existence check, so an
existing table is still a 409 there and not a 422.

Known gaps, deliberately not carried here: the OpenAPI fixture's pre-existing
drift, and REST's missing OAuth-scope enforcement in `get_current_user`.
