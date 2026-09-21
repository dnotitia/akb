---
status: accepted
stage: applied
created: 2026-09-16
updated: 2026-09-16
---

# Native Document publications

Native documents could be read normally but could not be published because
publication creation and anonymous reads still depended on legacy document
rows and Git. Native public links now bind to the authoritative Resource.

- `native_document_id` binds a publication to `(vault, document surface,
  Resource ID)` through a composite foreign key. It is exclusive with the
  legacy `document_id`; paths are only derived locations.
- Creation resolves once, then rechecks the same live Resource under a shared
  row lock inside the publication transaction. Concurrent move fails clearly;
  concurrent deletion cannot leave a usable orphan link.
- Database triggers update the derived URI on move and revoke publications
  atomically on Native soft deletion. Reusing the old path does not transfer
  a link to the replacement document.
- Live reads use the verified current Native revision and its metadata/body.
  A section filter restricts both body and image manifest. A missing section
  returns an empty body/manifest and `section_not_found`, never all sections.
- Existing password, expiry, view limits, author attribution, view grants and
  vault-scoped image authorization remain in force. Native identity is not
  exposed through the public response.
- Completed cutovers transfer existing links only through committed authority,
  verified Vault cutover and completed immutable document-ID mapping. Current
  Head may have advanced. Unmapped old links fail closed, without path lookup
  or stale legacy content fallback. The same transfer runs in the authority
  transaction of future cutovers.

Migration 106 adds the binding and lifecycle triggers and transfers verified
existing bindings. There is no shadow legacy document or second content copy.
Legacy publication behavior and File/table publication contracts remain intact.

Validation includes real PostgreSQL body/update/section/asset, move/path reuse,
soft deletion, cross-vault/surface constraints, create/move race, and cutover
mapping tests, plus existing publication and authority regressions. Merged-image
runtime validation is recorded separately from these local tests.
