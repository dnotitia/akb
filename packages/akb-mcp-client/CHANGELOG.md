# Changelog

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
