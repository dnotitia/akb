---
status: proposal
stage: design
created: 2026-08-04
updated: 2026-08-04
---

# Publications resolve by path — bind cleanup and resolution to the vault

## What this is

Publications addressed their target document by a path-shaped `resource_uri`
rather than by a document identity — a consequence of migration 022 collapsing
the old `document_id` / `file_id` foreign keys into a single canonical URI. With
`documents` keyed `UNIQUE(vault_id, path)`, a path can be reused, so publication
cleanup on document delete has to be explicit and complete. It was not: cleanup
lived in several inline copies and two delete paths omitted it.

This item makes document-publication cleanup a single implementation reached from
every delete path, and binds publication resolution to the publication's own
vault.

## What changed (PR 1)

1. **One cleanup implementation.** The by-`resource_uri` cleanup existed in four
   places; it is now one pair of helpers, called from every delete path with the
   caller's transaction threaded through so cleanup and row-delete commit
   together.

2. **One place deletes a document.** `DocumentRepository.delete_with_publications`
   replaces the plain row delete: it requires a caller-managed transaction, takes
   `FOR UPDATE` on the row, reads the path back from the locked row, cleans
   publications, then deletes. A static test allowlists every remaining
   `DELETE FROM documents` so a new site is caught in review. The row lock also
   serializes a publish that would otherwise race the delete.

3. **Writes onto a claimed path are refused.** Create, move, and external-git
   import reject a write to a path that a publication still claims but no document
   owns — with an error naming the publication rather than deleting it.

4. **Resolution is bound to the vault.** `resolve_document_publication` and the
   oEmbed title lookup key on the publication's own `vault_id`, with the URI's
   vault name demoted to a fail-closed cross-check — the binding the file path
   already carried.

5. **A file was publishable before its upload confirmed**, and the discard paths
   left the publication behind; both now clean up.

## Tests

- `backend/tests/test_publication_resolution_e2e.sh` — new end-to-end suite covering
  collection delete, move onto a claimed path, file publications under collection
  delete, and the upload-discard paths. Each case first asserts the ordinary
  behaviour so a later pass cannot come from a setup that silently no-ops.
- `backend/tests/test_publications_e2e.sh` (907 lines) was not running in CI; the
  e2e job now brings up MinIO and gates it.
- `tests/concurrency/test_invariants_unit.py` was running in neither CI job; its
  fixture now applies migrations and it is registered in the DB-backed job.

## Direction

Addressing a publication by a path rather than by an identity is what makes
this area need care in the first place, and restoring an identity anchor —
validated when a publication is created and again when it is resolved — is
where this should end up. That is a schema change with its own rollout, so it
is a separate piece of work rather than something folded in here.

A few smaller consolidations follow from the same direction: giving the file
cleanup helper the same URI-only signature the document one now has, and two
CI-integrity improvements to how suite results are counted.

Design detail, the reasoning behind each choice, and the review record are held
in the team's internal notes rather than in this repository.
