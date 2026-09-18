# Server-driven search filters

[Unified exact grep](../design/accepted/2026-09-16-unified-grep/README.md)
records the native grep implementation and local validation (2026-09-16).

Search refinement must happen before retrieval limits, not by filtering the
first page in the browser. Otherwise a matching document outside the first
25 results can never be discovered by selecting its type or tag.

## Request flow

```text
Search URL -> typed request options -> authorized server candidates
                                      -> hybrid retrieval or document grep
                                      -> bounded results and status feedback
```

The existing search engine and indexes remain unchanged. No schema migration,
new service, or additional deployment dependency is required.

Release the backend changes together with the frontend. An older backend can
silently ignore the new query parameters, so a frontend-only rollout does not
provide this filtering contract.

## REST contract

Both `/api/v1/search` and `/api/v1/grep` accept repeated `vault`, repeated
`doc_types`, repeated `tags`, `collection`, `include_archived`, `q`, and `limit`.

- Values within Vault, document-type and tag lists are OR conditions; different
  filter dimensions are intersected with each other and the caller's access.
- `collection` includes the named collection and descendants, not similarly
  named siblings. `%`, `_` and backslashes in names are literal, not SQL
  wildcards. When multiple Vaults are selected, the same path applies to each.
- Hybrid search additionally accepts `source_type=document|file|table`.
  Document metadata filters do not admit unrelated files or tables. Contradictory
  source/metadata constraints return no candidates, rather than broadening scope.
- The existing hybrid `type` parameter remains supported; `doc_types` intersects
  it when both are present.
- Grep additionally accepts `regex` and `case_sensitive`. Ordinary literal search
  means substring matching, not whole-document equality. Invalid regular
  expressions are validation errors, not successful empty responses.
- Hybrid search excludes archived documents by default. Grep retains its
  existing include-by-default behavior for older callers. The Search UI sends
  `include_archived=false` explicitly for both modes unless selected otherwise.

Native grep defaults to Document bodies. The UI can explicitly request
`include_text_files=true`; the deprecated `measurement_include_text_files` alias
remains accepted, and contradictory explicit values are rejected. REST/MCP grep
limit is 1–50. Document type/tag filters and archived-only scope exclude Files;
unarchived scope permits Files. Pending or stale File projections return a
readiness error, never a successful incomplete count. File grep replacement is
not supported.

## Candidate selection

Standard document queries bind type, tags, lifecycle, collection and ACL before
vector retrieval or grep scanning. File and table candidate queries apply source,
collection and Vault access; document-only metadata predicates exclude them.
Native documents apply the same metadata semantics to their verified heads before
search result counting and limiting. Native scope/resource/byte limits remain in
effect; narrowing metadata does not remove those safety boundaries.

The Vault-only vector optimization cannot express source or document lifecycle
filters. Searches requiring those constraints therefore use the existing source-ID
candidate path. This preserves filtering correctness at the cost of enumerating
authorized candidates. Large-corpus performance should be measured before rollout;
adding lifecycle metadata to vector drivers is a separate optimization, not part
of this change.

## Browser state

The Search route uses `q`, `v`, `mode`, `source`, repeated `doc_type`, repeated
`tag`, `collection`, `include_archived`, `regex`, `case_sensitive`, and
`include_text_files` query
parameters. Confirmed changes create history entries; only unsubmitted input
drafts stay component-local. Reload, browser Back, and document previews preserve
the search URL. The existing comma-separated `v` URL format remains supported.

Filters remain editable before results and after zero matches. Type/tag selection
selects document scope; selecting another resource kind clears document metadata
filters. Literal mode retains the Semantic resource choice in the URL but explicitly
searches Documents plus explicitly selected text Files. Tags can be entered independently of the loaded results;
result-derived tags are suggestions, not a complete facet inventory.

The global search panel also sends its selected resource kind to the server and
passes it into the advanced-search URL. Debouncing reports loading immediately,
so a pending request is not briefly displayed as a genuine empty response.

## Result feedback

- Hybrid `returned` is the number of returned resources. `total_matches` is a
  candidate-pool count, **not** a corpus-wide total. The UI labels top results.
- `excluded` explains a page shorter than the requested limit: public cause name
  → how many candidates that filter removed while assembling this page, `{}`
  when none were. It is page-relative, not a pool or corpus figure, and holds
  only causes that are not faults — a component that failed belongs to
  `degraded` instead, and no cause is reported in both places.
- `recovered` is its fault counterpart: internal cause name → how many
  candidates a fault removed from this page before the refill loop replaced
  them, `{}` when the page needed no repair. It is populated only when the page
  is complete, so it never names a fault `degradation_reason` is already
  naming. It reports corpus health, not a problem with the response, and is not
  a reason to retry, warn, or withhold results.
- Grep distinguishes returned resources/lines from exact total matching
  resources/lines. Explicit File opt-in adds resource aggregates while keeping
  Document aggregates Document-only. Native results preserve type, revision,
  content hash and body-relative line numbers. File links bypass Document previews.
- Truncation copy is specific to each mode; narrowing scope is the recovery path.
- A degraded response is incomplete, whether it contains results or not. It must
  never be labelled a genuine zero-match. Retry is available without changing the
  query, and raw server diagnostic details are not displayed as user guidance.
  Degradation reports a component that failed, or a hit lost to a stale source
  row that left the page short of the requested limit. Two things are not
  degradation. A filter is not: excluding the documents the request asked to
  exclude — the default `unarchived` archive scope, for one — leaves the
  response unflagged and is reported in `excluded` rather than in a banner. Nor
  is a fault the search already made good: when the refill loop replaced the
  dropped candidate the page is complete, so the response is unflagged and the
  fault is reported in `recovered`.
- During a same-mode refresh the previous ledger is labelled busy; request
  generations prevent late responses from replacing newer results.

## Verification

Backend contract and service tests:

```sh
cd backend
uv run pytest tests/test_search_filter_contract_unit.py -q
```

Frontend gates:

```sh
cd frontend
pnpm run design:check
pnpm run typecheck
pnpm run lint
pnpm run test
pnpm run build
```

Real-browser contract scenarios (production build with intercepted API fixtures):

```sh
cd frontend
pnpm run preview --host 127.0.0.1 --port 4176
# In another terminal:
AKB_FRONTEND_URL=http://127.0.0.1:4176 pnpm exec playwright test search-contract.spec.ts
```

These browser tests exercise HTTP request serialization, filter selection, reload,
history, viewport overflow, and light/dark rendering. They do not prove real login,
database SQL execution, or indexing. Use the isolated runtime documented in
`backend/scripts/ci/README.md` for live-backend verification; never point fixture
tests at a production endpoint.

Live HTTP/DB scenario, against an isolated local runtime:

```sh
cd backend
AKB_SEARCH_TEST_URL=http://127.0.0.1:18080 uv run pytest tests/test_search_filters_http.py -q
```

This registers temporary users and creates its own Vault, documents, files and
tables. It checks results outside the original top 25, collection boundaries,
metadata, literal/regex/case options, archival status and private-Vault access.
It waits for asynchronous indexing and removes its Vault afterward. The runtime
owns the remaining temporary accounts and dependencies. Use its deterministic
embedding stub: this checks retrieval/filter wiring, not model relevance quality.

For the non-mocked Chromium scenario, serve the production frontend build with
`/api` proxied to that same isolated backend, then run:

```sh
cd frontend
AKB_SEARCH_LIVE=1 AKB_FRONTEND_URL=http://127.0.0.1:4177 \
  pnpm exec playwright test search-live.spec.ts
```

This signs in through the actual local-login form and exercises search filters,
reload, archive inclusion, regex and Semantic/Literal switching using real HTTP
responses. Both live tests are opt-in and reject non-local target hosts. Do not
configure their local proxy to forward to a production backend.
