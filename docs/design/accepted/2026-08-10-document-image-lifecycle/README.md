---
status: accepted
stage: applied
created: 2026-08-10
updated: 2026-08-10
---

# Document image lifecycle

## Decision

Inline Markdown images use a stable, storage-neutral reference:

```markdown
![Architecture](/api/assets/7bd193d8-4e19-4b26-a712-5c9b3ae93f19)
```

The object bytes live in the configured S3-compatible store. Markdown never
contains bucket names, object keys, presigned URLs, deployment hosts, or vault
names. Images are hidden attachments rather than standalone AKB Files.

The editor and MCP tool emit the canonical inline form above. Server-side
reachability is derived from CommonMark image nodes rather than source-text
regular expressions, so a valid image destination with a title or a
reference-style image resolves to the same asset without treating examples in
code spans/fences or ordinary links as authorization.

An attachment may be reused by multiple documents in the same vault. Its live
visibility is the union of those current document references; it has no
independent public toggle.

## Reference model

AKB records two different forms of reachability:

- `document_asset_refs` is the current authorization set. It has composite
  document/vault and asset/vault foreign keys and cascades with document
  deletion.
- `document_asset_revision_refs` is a bounded manifest keyed by vault,
  document path, Git commit, and asset. It deliberately has no document foreign
  key, so a recently deleted document's historical revision remains renderable
  until `retain_until`.

Every document create, update, edit, move, and external-Git import claims valid
same-vault references and synchronizes the current set after the Git commit and
document-row update, inside the same PostgreSQL transaction that advances
`documents.current_commit`. An unavailable or copied foreign URL remains an
unreadable placeholder instead of aborting an otherwise valid Markdown import.
A superseding write extends the prior HEAD's manifest before replacing the
current set. The document, collection, and external-Git deletion paths do the
same before their document row is removed.

## Authorization

Authenticated byte reads execute one vault-scoped query and require one of:

1. the caller created the still-unclaimed upload;
2. at least one current document in the vault references the asset; or
3. the request supplies a document path and Git commit prefix matching a
   non-expired revision manifest.

Failures return 404 before object storage is touched. This avoids exposing an
asset-ID existence oracle across vaults. Historical UI reads include their
document path and selected commit; ordinary current reads use the live set.
After a bounded HEAD validation, image bodies stream through the API rather
than being accumulated into one in-process buffer per image request.

Anonymous publication reads do not use these document-wide grants. They remain
authorized from the exact resolved publication commit and section-filtered
Markdown slice, so publishing one section cannot expose an image used only by a
different section.

The page-open response spends one publication view and mints a bounded view
grant (24 hours by default so browser lazy-loading remains usable). Every
embedded-image request requires that grant and is resolved with view counting
disabled while still rechecking the current publication and exact section
slice. Images are subordinate bytes of the counted page, so a document with N
images still consumes exactly one view-cap entry. For this asset route only,
the counted grant is sufficient proof that the page password gate was passed;
that narrow capability outlives the one-hour full-publication password token
but cannot fetch the document, raw File bytes, tables, or arbitrary attachment
UUIDs. The renderer consequently omits the broader password token from image
URLs and sends only the asset-scoped grant.

## Deletion and retention

| Operation | Live access | Object lifecycle |
|---|---|---|
| Remove one Markdown image link | Revoked unless another current document references it | Kept for matching revisions, then collected |
| Delete a document | Its current refs cascade immediately | Shared refs survive; otherwise revision window then collection |
| Delete a collection recursively | Same as deleting each contained document | Same bounded behavior; standalone Files retain their existing cascade |
| Delete a vault | Revoked with the vault | Rows are removed immediately; every object key is transactionally queued for retrying deletion |
| Abandon an upload before save | Uploader-only preview until expiry | Collected after the unclaimed-upload TTL |

The collector first removes an indexed, bounded batch of expired revision
manifests, then locks eligible attachment rows with `FOR UPDATE SKIP LOCKED`. A
document claim takes the same row lock, eliminating claim-versus-GC races.
Metadata deletion and insertion into `s3_delete_outbox` share one transaction;
the existing retrying S3 worker performs the remote delete. A conservative
claimed-object grace also protects rolling upgrades from an older writer that
did not yet publish reference rows.

Uploads use an explicit pending/confirmed state. This preserves pre-hash legacy
Files as confirmed while making newly initiated transfers unambiguous. After
server-side decoding succeeds, AKB commits an unreadable pending attachment row
before the S3 PUT, then marks it confirmed and hash-verified only after the PUT
completes. Normal failures enqueue the key for deletion immediately; a hard
process exit leaves the pending row for the same bounded collector. Image
decoding runs in a bounded worker-thread path so multi-frame validation cannot
block the API event loop.
Request cancellation shields and settles an in-flight boto3 PUT before cleanup
is enqueued, preventing a late thread completion from recreating an already
deleted, untracked object.

Upload/finalization also holds a key-share lock on the vault row. Whole-vault
deletion takes the conflicting lock before enumerating object keys, so it either
waits for the attachment to finalize and deletes it, or wins before the PUT is
allowed to start. The delete transaction writes those keys to the existing S3
outbox rather than waiting on irreversible object-store calls while holding the
lock; the worker retries physical deletion after access and metadata are gone.
Slow body reads use a separate bounded pool with a deadline; they cannot consume
the smaller decoder/object-store transfer pool forever.

Defaults:

- `document_asset_revision_retention_days: 30`
- `document_asset_unclaimed_ttl_hours: 24`
- `document_asset_gc_interval_secs: 300`
- `document_asset_upload_body_timeout_secs: 60`
- `publication_view_grant_ttl_secs: 86400`

These are operational retention controls, not public-link credentials. Lowering
them can intentionally make older Git revisions lose image rendering sooner.
