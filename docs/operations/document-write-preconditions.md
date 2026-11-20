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
review/merge changes deliberately before submitting a newly based draft.

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
Web draft recovery does not automatically merge either: review the three
versions before applying a draft to a newer base.

## Existing integrations

The web document editor and skill editor already send the edit-start revision.
Grep replacement already pins each generated body to its own read. Internal
exact edits recompute against the current body and pin each derived write.
Metadata-only reservation backfills remain unchanged. The proxy forwards
conditions unchanged when reading a local file and never fetches one itself.

### MCP content and local file input

For `akb_put`, supply exactly one of `content` (a string, including `""`) or
`file` (a non-empty local path). `file` is available only through the stdio
proxy; HTTP clients send `content`. The proxy reads the file and forwards its
text as `content`, preserving the caller's version/hash conditions.

For `akb_update`, omit both fields to update metadata only. With no `file`,
`content: null` also retains the body; `content: ""` clears it and therefore
requires an original-read condition. A file update has the same precondition
requirement, including when the local file is empty.

Passing both keys is an `invalid_argument`, even if `content` is `null` or
`""`. Clients that used placeholder content with a file must **omit the content
key entirely**. Invalid body types and empty/non-string file paths are rejected
with `details.field` before any file read or document mutation. A local file
that cannot be read remains a file I/O failure, not a version conflict.

No database migration or new configuration is required. Existing clients can
send original-read conditions through their existing transport. The corrected
file-input schema and proxy validation require updating and restarting the
proxy; the backend body validation requires the matching server update.
See the SDK README for the typed update pattern.
