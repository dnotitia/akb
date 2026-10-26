# Browsing large Vaults and narrowing literal search

AKB supports bounded navigation without increasing scan limits, timeouts, or
application memory. The existing full-list API remains available for deliberate
exports; a small `limit` applied to an already-built list is not a bounded read.

## Start with one page

REST example (supply your normal authentication):

```text
GET /api/v1/browse/team?depth=0&page_size=100
GET /api/v1/browse/team?collection=guides&depth=0&page_size=100
```

MCP equivalent:

```text
akb_browse(vault="team", depth=0, page_size=100)
akb_browse(uri="akb://team/coll/guides", depth=0, page_size=100)
```

`page_size` accepts 1–200 identities. Follow `next_cursor` using the same Vault,
Collection, depth, resource kind and archive scope until `complete` is `true`.
Do not combine MCP `page_size`/`cursor` with the legacy `limit`/`offset` pair.
`content_type=collections` is a bounded Collection directory; `documents`,
`tables`, and `files` narrow resource kinds before selecting the page.

The TypeScript SDK exposes the same options as `docs.browse({ collection:
"guides", depth: 0, pageSize: 100, contentType: "documents", archiveScope:
"all", cursor })`. Omit `cursor` for the first page.

| Field | Meaning |
| --- | --- |
| `items` | Visible resources selected on this page |
| `scanned` | Identities selected for hydration on this page, at most `page_size`; not database rows visited by the query planner |
| `next_cursor` | Continuation, or `null` when this traversal is exhausted |
| `complete` | No more identities remain after this page for this scope |

Archive filtering, and MCP's page-local name filter, can produce an **empty page
with a continuation**. That is not an empty Vault or proof of no matches.
An absent/null completeness field from an older server is unknown, not `true`.
No exact total is computed. An omitted table row count is unknown, not zero.

In paged mode, `depth=0` includes direct child Collections and resources only;
`depth=N` descends N additional Collection levels; `-1` removes the depth bound
but keeps the page bound. The historical unpaged API's Collection-navigation
rows retain their original depth-independent behavior.

## What is bounded

The database selects identities before Native payloads are read. Only that page
is hydrated through the existing verified BodyStore; neither the frozen legacy
Document projection nor a derived search index becomes Native authority.
Native canonical-body reads also have a 16 MiB page budget and may end a page
early. Normal Document admission is already capped at 10 MiB per Document.
A historical Document exceeding the page body budget is rejected explicitly,
not silently omitted. Body sizes and subsequent Head reads use one read-only
repeatable-read transaction, so a concurrent replacement cannot bypass the
budget. Legacy hash reads use their pinned Git commit without persisting a hash
repair during this read-only request.

This is not a fixed JSON-byte ceiling. A verbose summary or table schema can
still produce a large response; request fewer identities and omit optional
hashes/summaries when a client has a smaller response budget. The proxy does
not impose the 8 MiB budget used by one QA client.

Pages use kind, bytewise path, then identity as a unique deterministic order.
The cursor is a position, not a credential or a database snapshot shared across
requests. Every page checks current access. Concurrent inserts/moves before the
cursor require a refresh; deletion, moves and grant changes are not frozen for
the duration of an entire traversal. Do not treat page completion as a
transactionally consistent export of a changing Vault.

The web explorer loads 100 identities initially and automatically requests the
next page near the end of its scroll pane. The main Collection index browses
direct children; the expandable sidebar retains its bounded recursive hierarchy.
Name search includes descendants of the selected Vault/Collection. Existing rows
remain during loading and retry; focus stays on the current control. A failed
append stops automatic retries and exposes Retry. Keyboard users also retain a
quiet next-page action. Vault directory rendering similarly advances on scroll
from its complete accessible directory, so its name search covers every Vault.
Overview Collections and resources have independent cursors and rendering batches.
There is no permanent partial-load explanation or visible Load more workflow.
If all visible Collections are closed, stop automatic resource requests after
passing the Collection catalog. Expanding a branch or searching resumes demand;
keyboard users can still request the next page explicitly. The catalog itself
continues on scroll, including Vaults with more than 100 Collections.

Keep ordinary navigation/list semantics: automatic paging alone does not make
a resource directory an ARIA feed. The
[WAI-ARIA feed pattern](https://www.w3.org/WAI/ARIA/apg/patterns/feed/) explains
why dynamic loading and assistive-reader focus need explicit coordination.
The directory therefore retains its keyboard action, existing focus and retry
control. Follow the [TanStack infinite-query guidance](https://tanstack.com/query/latest/docs/framework/react/guides/infinite-queries)
to avoid overlapping page fetches and retain the same cursor on retry.

The REST `name_query` parameter searches literal names and paths over the whole
selected scope, including items not previously loaded in the browser. It is at
most 200 characters, trimmed, case-insensitive, and part of the cursor binding.
`%`, `_`, and backslash are literal characters. Use `content_type=resources` to
page Documents/Tables/Files without Collection navigation rows. These two REST
additions do not change MCP's legacy page-local `filter` argument.

```text
GET /api/v1/browse/team?depth=-1&page_size=100&name_query=runbook
GET /api/v1/browse/team?collection=guides&depth=-1&content_type=resources&page_size=100&name_query=runbook
```

Catalog names are filtered in SQL before page selection. Native title metadata
lives in verified current revision frontmatter; frozen legacy titles, event
summaries and asynchronous search chunks cannot replace it. Native name search
therefore traverses bounded identity/body pages and returns only verified name
matches. An empty page may have a cursor: continue until a matching page or
`complete=true`, including for renamed or empty-body Documents. The frontend
continues such pages automatically and shows Searching until it can establish an
empty result. This avoids downloading all resources into the browser or making
an unbounded backend request, but a rare/absent Native title can still require
traversing the full selected corpus. Narrow the Collection or resource kind for
large scopes. No title index, scan-budget increase or read-side metadata mutation
is introduced. PostgreSQL `ILIKE` and Unicode case folding may differ for unusual
non-ASCII case mappings; ordinary ASCII name matching is aligned.

Search requests are debounced by 250ms and obsolete reads are cancelled. Search
is scoped name/path discovery; use Search for document-body searching.
Counts in a partial or filtered view say **Loaded**, and recursive deletion
never treats unknown contents as an empty Collection. Breadcrumbs fetch only
the authorized Collection ancestry rather than downloading the whole Vault.
Kinds not requested in a single-kind view are unknown, not empty. The current
authorized Document, File or Table can reveal its own row and Collection even
when it is outside the first page; this does not fetch every preceding page.
Move dialogs load the Collection directory only. The move endpoint remains
authoritative for name conflicts and preserves the chosen destination on failure.

## Grep result limits are not scan limits

Native literal grep checks the authorized candidate scope **before** reading
canonical bodies. Its protective ceilings remain 10,000 resources and 128 MiB.
`limit=1` or `limit=25` reduces returned previews, not the candidates searched.
`count_only` and `files_with_matches` do not bypass those scan protections.

```text
akb_grep(pattern="release", vault="team", collection="guides", limit=25)
GET /api/v1/grep?q=release&vault=team&collection=guides&limit=25
```

`collection` includes descendants and excludes sibling prefixes. `path` is not
a supported grep scope argument. To search a large hierarchy deliberately,
browse its Collections and search suitably bounded subtrees rather than
retrying an unchanged oversized request. Each successful response's existing
total/returned/truncation fields still distinguish all matches in that scope
from the previews that fit the result budget.

## Distinguish these outcomes

| Observation | Interpretation | Recovery |
| --- | --- | --- |
| HTTP 200, but a client rejects response bytes | Successful server response exceeded that client's budget | Use bounded pages, fewer items or narrower scope; inspect the client error |
| HTTP 422 / `grep_scope_too_large` | Protective candidate-count or byte rejection | Narrow `vault`/`collection`; lowering preview `limit` is insufficient |
| HTTP 200 with `complete=false` | A successful partial navigation page | Follow `next_cursor`, including after an empty page |
| HTTP 200 with grep truncation | Search succeeded; returned previews are capped | Inspect totals and truncation; use count-only or a narrower scope |
| HTTP 5xx | Backend/dependency failure, not a protective scope rejection | Inspect the error and service diagnostics; do not report zero matches |

The Search UI labels a protective rejection **Narrow the search scope**, opens
and focuses the Collection filter, and does not offer an unchanged-scope Retry
as its recovery. It does not silently broaden permissions or remove scan limits.

## Verification checklist

Compare an unchanged small Vault and a synthetic large Vault. Record response
bytes, request count, per-request latency and total traversal latency separately;
more bounded requests need not make a complete export faster. Compare the final
URI/hash set, including duplicate filenames, root/nested resources, archives,
confirmed Files and Tables. Check Reader success, outsider denial, malformed and
cross-scope cursors, an empty filtered page, next-page retry, and concurrent Head
replacement between selection and hydration. Keep production data untouched.

Design references: PostgreSQL documents the need for a unique ordering with
[LIMIT/OFFSET](https://www.postgresql.org/docs/16/queries-limit.html) and the
per-transaction snapshot supplied by
[repeatable read](https://www.postgresql.org/docs/16/transaction-iso.html#XACT-REPEATABLE-READ).
