# Document archive discovery and recovery (AKB-222)

## Purpose

An archived document remains the same resource. Archive changes default discovery;
it does not delete content, revoke access, disable published links, or create a
second archive store. Users must be able to find and restore it without knowing
its technical URI.

## API contract

REST browse, semantic search, and literal grep accept the optional
`archive_scope` parameter:

| Value | Documents | Files and tables |
| --- | --- | --- |
| `unarchived` | Draft and active | Included where supported |
| `archived` | Archived only | Excluded |
| `all` | All states | Included where supported |

An explicit scope overrides `include_archived`. Without it, existing defaults
remain unchanged: browse/search exclude archives, grep includes them. Existing
MCP callers retain their boolean contract. Responses echo the effective scope,
including empty responses, so a client can distinguish a supported empty result
from a server that silently ignores a new parameter. Document search/grep rows
also expose status; non-document resources have no document status.

Both legacy and Native storage paths apply scope before search candidates or
grep counts/limits. Browse retains Collections for navigation and filters the
document inventory before returning it. Semantic counts still describe the
bounded retrieval candidate window, not an exhaustive corpus count.

Restore reuses the existing document PATCH with `status: active` and the loaded
`expected_commit` when available. It preserves identity, path, content, history,
and links. No new table, endpoint, or archive index is required. Restoring a
previous draft explicitly produces an active document; the system does not
invent a remembered prior state.

## Interaction

- Collections and advanced Search expose Current / Archived / All documents.
  Current includes drafts. Search keeps the scope in its URL, including support
  for old `include_archived` links.
- The reader overflow offers Archive or Restore. An archived reader also has a
  compact restore notice. Both full-page and Search-preview readers use the same
  operation. Confirmation explains discovery versus access and the restore
  destination. Search preview remains open and its background results refresh.
- Archive/restore requires Writer or higher; changing the reserved Vault guide
  requires Owner. Historical/diff views and read-only Vaults cannot mutate state.
  Unavailable actions show the reason instead of disappearing.
- A PATCH alone is not considered proof of state change: the reader reloads the
  current document and verifies its status before notifying other views. Errors
  remain in the confirmation dialog without closing it or claiming success.
- Unsupported explicit filters suppress untrusted results and offer a return to
  current documents. No misleading zero-archive count is shown.
- The archived-only tree does not expose recursive Collection deletion, because
  that filtered inventory does not show all resources that deletion would affect.

## Boundaries

Native search uses a derived index and may lag a restored Head briefly. Browse
reads authoritative document state and provides a recovery path during that lag.
Existing links and public access remain governed by their existing policies.
The sidebar still uses the existing full subtree browse; DOM progressive
disclosure is not server-side pagination.

## Verification

Tests cover scope precedence and legacy defaults; legacy/Native parity;
document-only archives; filtering before counts/limits; empty response echoes;
permission and concurrency guards; older-server handling; stale UI responses;
and reader archive/restore with unchanged path and preview state. The browser
contract uses isolated HTTP fixtures and is distinct from a live-server E2E.
