# PM feedback

## 2026-08-31 — Open questions closed after the first implementation pass

Six questions were raised out of the implementation and answered:

- **`distilled_doc_count` stays omitted.** The vault-label derivation is not to
  be used — a retroactively mutable free-text label cannot support a metric
  anyone has to defend. The per-document marker is the accepted direction, and
  it is cross-repo work outside this track. Omission is the correct encoding
  meanwhile; "absent is not zero" exists for exactly this case.
- **`vector_chunk_count` stays indexed-only.** The customer-facing meaning is
  "what is in the vector store"; the indexing backlog is `/health`'s
  responsibility and that separation is intended.
- **`file_bytes` stays all-or-nothing.** Withholding the sum when any confirmed
  file has no recorded size is the point of the contract, since a partial sum
  reads as a total. If operations shows legacy NULL rows suppressing the field
  often in practice, revisit with a `unsized_count` alongside the sum — not
  before.
- **`file_count` keeps counting attachments.** The field means stored objects.
- **Long-outage gaps need no change here.** The platform loader already
  distinguishes them: past `first_observed`, a window with no row by D+2 is a
  gap, not pre-collection. Not reconstructing unobserved windows is correct.
- **`/health` keeps the field in both places.** Top-level as the contract term,
  and inside `vector_store.backfill.upsert` where an operator reading the
  backlog expects it.

Also requested: this design item, recording the listener's shape, the contract,
and the timestamp-format note.
