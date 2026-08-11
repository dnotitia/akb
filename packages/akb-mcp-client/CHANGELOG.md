# Changelog

## 2.2.0 — upload images for inline Markdown documents

Adds two proxy-local tools for agent-authored document images:

- **`akb_put_image`** reads a local PNG, JPEG, GIF, or WebP (maximum
  10 MiB), uploads it through AKB's authenticated image endpoint, and returns
  both the stable `/api/assets/{uuid}` URL and a ready-to-paste Markdown image
  expression. Agents place that expression at the intended position with
  `akb_put`, `akb_update`, or a targeted `akb_edit`.
- **`akb_discard_image`** removes an upload that never reached a document
  commit. Repeating a completed cleanup is idempotent; an active or already
  claimed image reports that no deletion occurred, while the backend retains
  claimed bytes under the document lifecycle. AKB derives access from current
  document references and retains removed or deleted references for a bounded
  Git-revision window before GC.

Document images are deliberately separate from `akb_put_file`: they stay out
of File browse/search/publication surfaces, inherit the document/vault access
model, and are decoded and validated by the backend before becoming usable.
The proxy still has zero runtime dependencies and never receives object-store
credentials or a presigned URL for this path.

The proxy-local MCP `initialize` response advertises the inline-image workflow.
Backend guidance is capability-conditional, so direct MCP connections and
older proxies are not instructed to call tools absent from their tool list.

`akb_put_image` requires the corresponding backend image endpoint. During a
split backend/proxy rollout, deploy and verify the backend first, then publish
or install `akb-mcp` 2.2.0. Publishing the proxy first would expose the local
tool while an older backend can only reject its upload request. Existing MCP
processes must be restarted (or otherwise re-resolved) to load the new npm
version and tool list.

## 2.1.0 — survive backend/VPN outages without a session restart

Resilience fix for the failure mode where a long VPN/backend outage
silently drops the AKB server from a client's tool namespace for the rest
of the session.

**Root cause**: `initialize` used to round-trip to the backend. If the
backend was unreachable at handshake time (VPN down at startup), the
handshake failed and the MCP client dropped the whole server for the
session — so even after connectivity returned, `mcp__akb__*` tools stayed
gone until the client/session was restarted. The proxy process itself was
alive the whole time; the coupling of *client-visible liveness* to
*backend reachability* was the bug.

**What changed** — client-visible liveness is now decoupled from backend
reachability:

- **`initialize` is answered locally.** The server always registers, even
  with the backend down. It echoes the client's protocol version and
  advertises `tools.listChanged`.
- **`tools/list` degrades gracefully.** With the backend unreachable and
  nothing cached, it serves the local file tools only (a valid, if partial,
  response) instead of erroring. The backend tool list is cached on first
  success and served thereafter.
- **A background reconnect monitor** re-establishes the backend session
  with exponential backoff (1s → 30s cap), effectively forever while the
  client stays connected. On recovery from a degraded list it pushes
  `notifications/tools/list_changed`, so the full toolset reappears
  **without a session restart**.
- **Tool calls never kill the proxy.** A connection error marks the backend
  not-ready, kicks the monitor, and retries; a persistent outage surfaces a
  normal JSON-RPC error for that one call rather than tearing anything down.
- **A short connect-phase timeout** (`AKB_MCP_CONNECT_TIMEOUT_MS`, default
  10s) detects a blackholed connection quickly, separate from the generous
  5-min response timeout kept for legitimately slow operations
  (`akb_delete_vault` on large vaults).

No changes to tool behavior, auth, or the file-transfer path. Fully
backward compatible.

**Known limitation**: if the backend goes silent mid-session on an already
*established* keep-alive socket (no RST), the *first* in-flight tool call
can wait up to the response timeout before the retry/monitor kick in;
subsequent calls fail fast because the monitor has flipped the ready flag.

## 2.0.4 — MCP Registry: add `mcpName`

No code change. Adds `"mcpName": "io.github.dnotitia/akb"` to
`package.json`. The official MCP Registry verifies server ownership by
matching this field on the published npm package against the registry
entry's name — so the package must ship it before `akb-mcp` can be
listed (and cascade to downstream directories). Behaviour is unchanged.

## 2.0.3 — make MIT licensing explicit

No code change. The proxy has always declared `"license": "MIT"` in
`package.json`, but the only `LICENSE` file in the repo was the root
PolyForm NC (and now BSL 1.1) covering the AKB backend — leaving the
proxy's actual license ambiguous to anyone reading the source.

This release ships a package-local `LICENSE` file with the MIT text,
so the npm tarball is self-contained and the proxy is unambiguously
MIT regardless of how the repo at large is licensed.

**Why the proxy stays fully open while the backend moved to BSL 1.1**:
the proxy is a thin stdio ↔ HTTP forwarder meant to be embedded inside
arbitrary MCP-aware agent clients (Claude Code, Cursor, Windsurf,
custom agents, etc.). MIT removes any friction for those embedders.
The AKB backend — the actual knowledge base — is where the BSL
protection applies. See the root [LICENSE-CHANGE.md](../../LICENSE-CHANGE.md)
for the rationale on the backend transition.

## 2.0.2 — bump default request timeout (30s → 5min)

Bug fix: the proxy's per-request timeout was hardcoded to 30s, which
aborted any operation slower than that on the client side — most
visibly `akb_delete_vault` against a large vault (7K+ docs), where the
backend cascade (chunks delete + vector outbox + git cleanup) easily
runs past 30s. The operator would see `Request timeout (30s)` even
though the backend kept processing and eventually completed; this
produced the misleading impression that the delete had failed when in
fact it had succeeded after the client gave up.

The default is now 5 minutes (300_000 ms). For very large vaults or
slow links, set `AKB_MCP_REQUEST_TIMEOUT_MS` to override. S3
upload/download paths remain at 10 min (unchanged).

Longer-term fix (separate backend PR): make `akb_delete_vault` an
async background job that returns immediately and exposes a status
endpoint, so client timeout becomes irrelevant.

## 2.0.1 — keep-alive proxy connections

Performance fix: the stdio ↔ HTTP proxy now reuses TCP+TLS connections to
the AKB backend via module-level `http.Agent` / `https.Agent` with
`keepAlive: true`. Each MCP tool call previously paid a fresh handshake
because Node's default agent ships with keep-alive off; a typical agent
session chains 5–15 calls, so this saves one round-trip per call
(~40–100 ms on a nearby cloud backend, more across regions).

No contract change. S3 presigned-URL methods (`_uploadToS3`,
`_downloadFromS3`) are intentionally unaffected — they target arbitrary
upload hosts, not the AKB backend.

Thanks to @MackDing for the contribution (#65).

## 2.0.0 — URI-canonical hard cutover (BREAKING)

The backend MCP contract is now URI-canonical: every resource handle
collapses onto a single `uri` of the form `akb://{vault}/<type>/<id>`.
Tool inputs no longer accept the legacy `(vault, doc_id)` / `(vault,
file_id)` pairs, and responses no longer surface internal UUIDs.

### File tools — input shape

- `akb_get_file` and `akb_delete_file` take `{ uri, save_to? }` instead of
  `{ vault, file_id, save_to? }`. Pass the URI from akb_browse or
  `akb_put_file`'s response.
- `akb_put_file` is unchanged from the caller's perspective (still
  `{ vault, file_path, collection?, ... }`) — but the response now carries
  the canonical `uri`.

### Backend response envelope

`akb_put`, `akb_get`, `akb_update`, `akb_edit`, `akb_create_table`,
`akb_put_file` (and friends) all return `uri` as the sole identifier.
`id` / `doc_id` / `file_id` / `source_id` / `vault_id` have been removed
from MCP payloads. The corresponding REST endpoints used by the proxy
internally are unaffected — only what reaches the MCP client changed.

### Compatibility

This is a hard cutover with no opt-out. A 1.x proxy talking to a 2.0
backend (or vice versa) will fail at the tools/list contract — schemas
no longer line up. Pair akb-mcp 2.x with a backend built from
`feat/uri-canonical-hard-cutover` (or its merge into main) onwards.

## 1.0.0

Backend contract refresh — first stable major. Pair with backend
that includes the `feat/crud-events-refactor` series (Phase 0:
clean CRUD infrastructure + table/file events).

### Backend response envelope (BREAKING for direct consumers)

All table and file REST responses now use a flat envelope with
consistent keys:

- Single resource: `{ kind, id, vault, ...resource-specific }`
- List endpoint:   `{ kind, vault, items, total }`
- Bulk action:     `{ kind, parent_kind, parent_id, count, ... }`
- Delete:          `{ kind, id, vault, deleted: true }`

Old keys removed / renamed:
- `table_id` / `file_id` / `row_id` → `id`
- list responses' `tables` / `files` → `items`
- SELECT result `rows` → `items`
- `dropped: true` → `deleted: true`
- delete returning `bool` → full envelope dict

The proxy itself doesn't destructure these renamed keys directly
(it only reads `id`, `upload_url`, `name`, `download_url`, and
`size_bytes` — all preserved), so the file-tool flows
(`akb_put_file`, `akb_get_file`, `akb_delete_file`) keep working
without any client-side change. The bump signals the wire-level
backend contract has changed and the recommended baseline is now a
matched 1.0 backend.

### New events (additive)

Backend now emits these in the `akb:events` Redis stream:

- `table.create` — payload `{vault, table_name, columns_count, description}`
- `table.drop`   — payload `{vault, table_name}`
- `file.put`     — payload `{vault, collection, name, mime_type, size_bytes}`
- `file.delete`  — payload `{vault, collection, name, s3_key, size_bytes}`

No proxy-side change required.

### Other

- `repository.url` corrected to `https://github.com/dnotitia/akb.git`.
- `CHANGELOG.md` is now part of the npm package.
- Releases are published manually with `npm publish --access public`
  from `packages/akb-mcp-client/` after a backend cutover lands on
  main.

## 0.6.0 and earlier

Pre-1.0 development series. See git history for details.
