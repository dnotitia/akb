# Document write preconditions

## Upgrade requirement

Whole-body updates through REST PATCH, MCP `akb_update`, or the stdio proxy's
`file` parameter must provide `expected_commit` or `expected_content_hash`.
This changes previously accepted unpinned whole-body writes. Upgrade scripts,
agents and clients before rolling out the server. There is no force flag or
automatic conflict retry.

```python
original = akb_get(uri=uri)
draft = compose_changes(original.content)
akb_update(uri=uri, content=draft, expected_commit=original.current_commit)
```

The token must come from the read used to compose the draft. Fetching the
latest token immediately before submitting an old-derived draft defeats the
protection. On conflict, preserve the draft, read the latest document and
retain the original base. Compare those three versions, reapply the original
intent to the latest version while preserving concurrent changes, and save the
reviewed composed draft with that latest read's token. Reading latest and
resending the unchanged old draft with a new token is not a merge. Callers must
not automatically retry conflicts or use a global URI-to-revision cache to
substitute a newer condition.

## Contract

| Operation | Condition |
| --- | --- |
| Non-null body, including `""` | At least one original-read condition required |
| Omitted/null body | Metadata-only field patch; conditions optional |
| `expected_commit` | Full 40-character lowercase token; exact revision must match |
| `expected_content_hash` | Full 64-character lowercase SHA-256; body must match |
| Both supplied | Both must match, never OR |
| Exact-text edit | Current exact text must match; optional `base_commit` protects its revision context |

Null/omitted conditions are absent. Blank, whitespace, partial, malformed or
NUL-bearing conditions are invalid arguments (REST 422, MCP `invalid_argument`).
A valid but stale condition is a conflict (REST 409, MCP `conflict`). Historical
read selectors may allow abbreviations; write conditions require the full
token returned by the original read. Completed Legacy→Native mappings remain
accepted only while they still identify the current revision of that resource.

A hash does **not** protect metadata changes or prove that no intermediate
body changes occurred. After A→B→A, the original hash of A matches again;
the original revision does not. Prefer `expected_commit` for combined body
and metadata edits. A metadata-only patch can still replace a concurrently
changed field unless the caller supplies an exact revision condition.

Exact-text editing is not semantic merging. Without `base_commit`, a matching
string may be changed in a context that evolved since the caller read it.
On a revision conflict, retain the intended replacement, read latest, and
review its surrounding context before deriving a new exact-text edit pinned
to that read.

## Web editor recovery

While a document editor is active and the page is visible, it polls the
document head every 15 seconds. A changed revision shows a banner without
replacing the edit-start base, local draft, or its save condition. The check
uses reader-gated `GET /api/v1/document-head/{vault}/{doc_id:path}`. Its
`document_head` response contains only `kind`, `uri`, `vault`, `path`,
`current_commit`, and `updated_at`; it does not fetch the document body or
metadata fields for merging. This endpoint provides polling rather than an
event stream or CRDT collaboration.

Recovery reads the complete latest document and keeps that review snapshot
fixed. Review compares the original base, local draft, and latest snapshot.
Editable fields are resolved individually so independent changes can be
preserved; overlapping body changes require a manually composed resolution.
There is no automatic textual merge. Later head checks do not silently replace
the version under review or its token. Save the reviewed draft using the
reviewed snapshot's revision; a further conflict requires another latest read
and review while keeping the draft.

An authentication, access, or missing-document error disables applying and
saving without discarding the comparison's in-progress combined result. The
editor owns that result and its field choices across presentation remounts.
`Check access` retries only the lightweight head read; it does not reload the
comparison. A later network or service failure cannot clear known lost access.
Only a successful authorized head read, or a new editor identity/base, clears
that observation. Reloading a comparison stays disabled until access recovers.
After a user confirms replacing a composed result, its inputs and field choices
stay disabled during the comparison read. This prevents new edits from being
silently replaced by the arriving snapshot; cancellation still keeps the
original editor draft.

Draft ownership and read verification have different lifetimes. A foreground
access check cancels outdated reads, but does not discard the same user's
reviewed comparison. The Vault Guide comparison is hidden while ownership is
being verified and restored, unchanged, when the same owner is authorized.
Changing the account or Vault does not carry that comparison into the new
workspace.

A successful write is acknowledged against its document, account and auth
session, not an access-check counter. An intervening same-session access check
must not discard that success. Pre-save reads are canceled before reconciling
the receipt; private data is not inserted into an unverified read scope. A
subsequent authorized full read replaces the temporary saved display snapshot.

The Vault Guide is the reserved `overview/vault-skill.md` document. Owners can
read it with MCP `akb_get` and edit its body with `akb_update`, using the same
original-read precondition as other documents. Ordinary Writers can read it
but cannot edit it. Head reads require only Reader access: they do not prove
owner-only write capability. A Guide write rejected with 403 therefore requests
foreground identity and Vault-role revalidation; only a new proof confirming
owner capability clears that write denial. No write is retried automatically.

## Existing integrations

The web document editor and skill editor already send the edit-start revision.
Grep replacement already pins each generated body to its own read. Internal
exact edits recompute against the current body and pin each derived write.
Metadata-only reservation backfills remain unchanged. The proxy forwards
conditions unchanged when reading a local file and never fetches one itself.

No database migration, new configuration or proxy-runtime upgrade is required.
Existing clients can send the new required condition through their existing
transport. See the [SDK README](../../packages/akb-client/README.md#recovering-a-conflict)
for the typed update and reviewed recovery pattern.
