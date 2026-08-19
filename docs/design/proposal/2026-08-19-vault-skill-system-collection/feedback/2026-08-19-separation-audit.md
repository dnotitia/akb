# Separation audit — why this design exists (2026-08-19)

Five-agent workflow audit (4 parallel explorers + 1 verifying critic; the six
most load-bearing claims re-checked against code, all VERIFIED). Question:
what actually enforces "the overview collection is for the vault-skill only"?
Answer: nothing, at any layer — by explicit v1 design ("convention only",
`2026-05-14-akb-help-skill-bootstrap/README.md:59`; the item's `rounds/` and
`feedback/` are empty, so reservation was never discussed).

## Backend write paths — zero guards

- `DocumentService.put()` validates only `status`
  (`document_service.py:558-561`); any type lands in any collection;
  `doc_type` has no enum and no DB CHECK (`init.sql:401`). The MCP `akb_put`
  schema documents `type` as "Free-form — any string is accepted"
  (`tools.py:137-146`), so tightening is an agent-facing contract change.
- `akb_import` routes through the same unguarded `put()`
  (`knowledge_io.py:134-157`).
- Canonical doc is movable and deletable like any doc (no path branch in
  `move()`/`delete()`); `overview` is cascade-deletable
  (`collection_service.py` contains no mention of "overview").
- REST PATCH can retype the skill away (`documents.py:150-152` →
  `document_service.py:1008-1009`); MCP `akb_update` cannot — only because
  its schema omits `type` (asymmetry, not protection).
- `VAULT_SKILL_PATH` is referenced only by help rendering (3 uses in
  `help.py`); the literal path otherwise appears only in the create-rollback
  emptiness query (`document_service.py:2265`) and comments.

## Dual identity keying — the felt "unclear separation"

- Path-keyed: `akb_help` (`help.py:1945`), vault page chip/About
  (`vault.tsx:163-173`), settings (`vault-settings.tsx:109-116`).
- Type-keyed: document viewer (`document-view.tsx:106`) — ANY
  `doc.type === "skill"` at any path gets the banner claiming "Agents writing
  into this vault read this first", an AGENT tab that renders the CANONICAL
  doc's preview (not the viewed doc), and a Reset button that overwrites the
  VIEWED doc's body with the seed template
  (`skill-banner.tsx:37-51`).
- Divergence scenarios confirmed both ways (retyped canonical: chip says
  defined, viewer drops skill UI; stray skill-typed doc: full skill chrome on
  the wrong doc).

## UI invites mixing

- Explorer offers "new doc" on the overview row
  (`vault-explorer.tsx:286,437-451`); create form defaults to `note`, offers
  `skill` anywhere (`document-new.tsx:53-56`, `doc-constants.ts`).
- Delete button on the canonical doc is gated only on write role
  (`document.tsx:689-693`); overview is cascade-deletable from the UI.

## Notable edges

- After a move, aliases keep the old path resolving — until any writer claims
  the vacated canonical path, which severs the alias
  (`document_service.py:639-641`) and silently swaps what `akb_help` serves.
- Both help fetch shims swallow all exceptions into None — DB outage renders
  as "vault has no skill" (`server.py:383-386`, `help.py` REST twin).
- Mirror vaults: no seed, write-blocked, yet the missing fallback instructs
  `akb_put` (unfollowable).
- `metadata_worker` can auto-type mirror docs as `skill`
  (`metadata_worker.py:38`).

## Tests

- Zero coverage of the reservation question in either direction.
- `test_skill_e2e.sh:133-134,153` actively depends on the canonical doc being
  deletable/editable with plain tools — any guard breaks the suite as
  written (redesign scoped in the README).

## Open questions carried into the design

Rule scope (exact vs prefix — design chose prefix), non-document resources
(design: covered), mirror ingestion path, verb coverage (design: full
matrix), recovery flow (design: delete forbidden, reset only), import
contract (design: per-record skip), native enforcement point (design: path
namespace, not collections table), rule direction (design: two-way).
Prod violation prevalence remains unknowable from the repo — counted before
backfill, recorded privately.
