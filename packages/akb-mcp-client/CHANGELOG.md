# Changelog

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
- Releases are now published by the
  `.github/workflows/publish-akb-mcp.yml` workflow on a
  matching `akb-mcp-v*` git tag (with `--provenance`). Manual
  `npm publish` from a workstation still works as a fallback.

## 0.6.0 and earlier

Pre-1.0 development series. See git history for details.
