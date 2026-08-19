---
status: proposal
stage: design
created: 2026-08-19
updated: 2026-08-19
---

# Vault-skill system collection: reservation, auto-injection, and a single editing surface

## What this is

Successor to the accepted `2026-05-14-akb-help-skill-bootstrap` design. That
design deliberately made the vault-skill "a normal AKB document" at the
convention-only path `overview/vault-skill.md`. A five-agent audit
(`feedback/2026-08-19-separation-audit.md`) confirmed the consequence: the
skill/overview separation is enforced nowhere, skill identity is judged two
different ways (path-keyed vs type-keyed) across surfaces, and the UI actively
invites mixing ordinary documents into `overview/`.

This design keeps the original storage premise — the skill IS a document
(PostgreSQL row + git file, versioned, editable with regular tools) — and adds
the three things v1 left out:

1. **Reserved system namespace.** `overview` becomes a two-way-enforced system
   collection: only the vault-skill may live there, and `doc_type="skill"` may
   exist only there (one per vault).
2. **Auto-injection.** The skill body is attached automatically to MCP tool
   responses on first touch of a vault per session, and re-attached when the
   skill changes — the agent no longer has to remember to call `akb_help`.
3. **One editing surface.** The web UI treats the skill as vault
   configuration: a dedicated section in vault settings, a visually distinct
   locked collection in the tree, and no scattered banner/reset affordances.

## Decisions (PM-confirmed 2026-08-19)

| # | Decision | Choice |
|---|---|---|
| 1 | Injection mechanism | Tool-response injection, tracked per (session, vault, skill-version); version change re-injects |
| 2 | Enforcement direction | Two-way: overview is skill-only AND skill type is canonical-path-only |
| 3 | UI surface | System collection in tree + dedicated editor in vault settings |
| 4 | Survival invariant | Canonical doc cannot be deleted; reset-to-template only |
| 5 | Existing violations | Automatic migration (service-layer backfill), prod count taken first |

Decision log with alternatives: `rounds/2026-08-19-brainstorm-decisions.md`.

## Non-goals

- De-documenting the skill (moving it to a `vaults` column / dedicated table)
  — rejected: loses git history, `akb_edit`, diff/history tooling, and
  contradicts the shared-storage premise. See rejected approach B.
- A first-class `collections.is_system` DB concept — rejected as YAGNI (one
  system collection exists; the deferred v1.1 collection-skill idea lives
  inside each collection, not in new system collections). See rejected
  approach C.
- Collection-level skills (`{collection}/_skill.md`) — still deferred, as in
  the 2026-05-14 design.
- Client-context-loss heuristics (periodic TTL re-injection) — v1 injects on
  version change only.

## Architecture

### Policy module (approach A)

New module `backend/app/services/skill_policy.py` owns:

- `VAULT_SKILL_PATH = "overview/vault-skill.md"` (moved from
  `mcp_server/help.py:1871`; help.py imports it),
  `SKILL_COLLECTION = "overview"`, `SKILL_DOC_TYPE = "skill"`.
- Guard functions raising the canonical `ForbiddenError` (existing error
  envelope, code `FORBIDDEN`, message naming the reserved namespace and the
  supported alternative).
- An internal-bypass parameter used only by the seed, the reset operation, and
  the migration backfill.

The rule binds to the **path namespace** (`overview` exact + `overview/`
subtree), not to the collections table — the native-ledger backend creates no
collections row for the seed, so a path rule is the only formulation that
applies identically to both backends.

### Enforcement matrix

| Action | Rule | Enforcement point |
|---|---|---|
| Create doc in `overview/` | Blocked (internal seed/reset/migration only) | `document_service.put`, `native_document_service` put |
| Create/retype `skill` outside canonical path | Blocked | put + update (closes the REST PATCH retype hole, `documents.py:150`) |
| Retype canonical doc | Blocked — `type=skill` pinned; other frontmatter free | `document_service.update` (~:1008) |
| Move canonical doc, or any move in/out of `overview/` | Blocked | `document_service.move` + native |
| Delete canonical doc | Blocked — reset only | delete paths, both backends |
| Delete `overview` collection (incl. cascade) | Blocked | `collection_service.delete` |
| Import records into `overview/` or with `type=skill` | Per-record skip + warning (extends existing ConflictError-skip pattern) | `knowledge_io` |
| Files/tables under `overview/` | Blocked (namespace covers non-document resources) | file/table create paths |
| LLM auto-typing as `skill` | Removed from `metadata_worker._DOC_TYPES` | `metadata_worker.py:38` |

Reset = overwrite canonical body with the seed template through the normal
update pipeline (git history records it; no delete/recreate).

Mirror (external-git) vaults keep the status quo: no seed, no skill, no
injection; the missing fallback text no longer instructs an impossible
`akb_put`.

### Auto-injection flow

Injection lives at the dispatch chokepoint (`mcp_server/server.py
call_tool()`, next to audit/tool-usage recording). After a successful
`_dispatch`, the server:

1. Resolves the single vault the call touched by promoting
   `tool_usage._vault_of()` to a shared helper (it already handles URI args
   and multi-vault `akb_sql`; calls with no single target inject nothing).
   Reads and writes both qualify — first touch is usually browse/search.
2. Consults an in-process map `{(session_id, vault): injected_version}`.
   Version = skill content hash. On miss or mismatch, attaches to the result
   dict:

```json
"vault_skill": {
  "vault": "<name>",
  "version": "<hash8>",
  "reason": "first_touch | updated",
  "body": "<markdown, bounded>",
  "truncated": false
}
```

Version/body lookups go through a per-vault cache (60s TTL) with write-through
invalidation when an update commits to the canonical path in the same process
(API replicas=1 today; TTL is the safety net for future multi-replica).

Safety contract (mirrors `tool_usage.record`):

- Injection must never fail or slow-fail the tool call — lookup errors are
  logged and skipped.
- The session map is bounded; eviction's only cost is one harmless
  re-injection.
- `body` is clipped (16KB) with `truncated: true` and a pointer to `akb_help`
  for the full text.

Surrounding changes:

- `initialize.instructions` is rewritten from "call akb_help before writing"
  to "a `vault_skill` payload is auto-attached on first touch; apply it, and
  call `akb_help(topic="vault-skill")` for the full text".
- `akb_help(topic="vault-skill")` stays as the explicit full-text fetch. With
  the survival invariant, its missing branch applies only to mirror vaults.
- The fetch shims that swallow every exception into "no skill"
  (`server.py:383-386`, `api/routes/help.py:69-71`) are split so absence and
  error are distinguishable.

### UI/UX

Single judgment rule (path == canonical) and single editing surface:

- **Tree**: `overview` renders as a locked system collection (icon + tone,
  pinned top); its per-row "new doc" and delete buttons are removed. Not
  hidden — what `akb_browse`/git show must match the UI.
- **Forms**: `overview` leaves the collection picker and is rejected by the
  path validator with a clear message; `skill` leaves `DOC_TYPES` in the
  create form and frontmatter dialog (it becomes a system-assigned type).
  The search type filter keeps `skill`.
- **Vault settings**: the skill section becomes the editor — body preview +
  AGENT preview tabs (reusing `/help/vault-skill-preview`), markdown editing,
  reset-to-template with a "recorded in git history" confirm, and a link to
  the document history view. Mirror vaults show an explanatory note instead.
- **Cleanup**: opening the canonical doc by its document URL redirects to the
  settings editor (reusing the `/vault/:name/skill` redirect direction);
  SkillBanner's own Edit/Reset buttons and the viewer's AGENT tab are removed
  — the misdirected-reset failure class disappears structurally.
- **Status chip**: with the survival invariant, defined/missing is
  meaningless for normal vaults; the chip becomes "template vs customized"
  (seed-template hash comparison).
- `vault.tsx` empty-state scaffold discount stays but no longer needs the
  existence probe.

### Migration

Service-layer idempotent backfill (BackfillRunner pattern, per-vault git
lock, internal bypass) — never raw SQL, because doc moves must keep git + PG +
aliases + indexes consistent:

| Violation class | Treatment |
|---|---|
| Ordinary docs under `overview/` | `move()` to the path minus the `overview/` prefix — `resource_aliases` keep old URIs resolving |
| `skill`-typed docs elsewhere | retype to `note` (frontmatter rewrite via update pipeline) |
| Vaults missing the canonical doc | reseed via `build_vault_skill_seed_request` |
| Canonical doc retyped away | restore `type=skill` |
| Mirror vaults | excluded |

A prod violation count runs before the backfill to size the blast radius.
Counts are recorded in the private ops vault only — never in this public
repo.

### Back-compat

- `vault_skill` response key is additive; the stdio proxy passes it through
  unchanged.
- `akb_put`'s `type` stays free-form with `skill` documented as reserved
  (`tools.py` description update — an agent-facing contract change).
- The only breaking change: writes into the reserved namespace and
  retype/move/delete of the canonical doc return `FORBIDDEN` with guidance.
- Export keeps including the skill doc; import skips reserved records
  per-record.
- Rollback-safe: guards lift with a code rollback; migrated docs keep working
  via aliases.

### Testing

- **Unit**: full guard matrix (verb × legacy/native), reset, internal bypass.
- **Injection unit**: first-touch, version-change re-injection, multi-vault
  skip, lookup-failure skip, bounded-map eviction, truncation.
- **e2e**: `test_skill_e2e.sh` redesigned — it currently deletes the canonical
  doc to produce the missing case (T4) and edits it with plain tools, both of
  which the guards now forbid. Replace with five guard-rejection asserts
  (put/retype/move/delete/collection-delete), injection payload scenarios,
  and unchanged `akb_help` behavior; the missing case moves to a mirror-vault
  fixture.
- **Migration**: fixtures per violation class + double-run idempotency.
- **Frontend**: settings editor edit/reset flows, create-form reservation
  rejection.

### Release order

1. Prod violation count (recorded privately).
2. Deploy code (guards + injection + UI).
3. Run backfill.
4. Verify + e2e green.

## Rejected approaches

- **B — de-document the skill** (vault column / dedicated table): cleanest
  "config, not content" story, but loses git versioning, `akb_edit`, history
  tooling, and contradicts the design premise that the skill shares the
  document storage system.
- **C — first-class system-collection concept** (`collections.is_system`):
  generalizes for future system docs, but only one exists, the native backend
  has no collections rows to flag, and it adds a schema migration for no
  current consumer.

## Open implementation questions

- Key name `vault_skill` must be checked against every tool's existing
  response keys before freezing (no collision found by inspection; verify in
  the plan).
- Whether a root-level (collection-less) path is valid for migrated docs, or
  whether prefix-stripping needs a fallback collection.
- Behavior for sessions without a usable `_session_id()` (stateless
  connections): fall back to per-connection tracking rather than per-call
  injection.
