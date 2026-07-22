# AKB-110 Collection Lifecycle Behavior Contract

## User-Visible Goal

An `@akb/client` consumer can create and delete collections through the vault-scoped `.docs` facade with typed success envelopes, exact REST requests, and unchanged shared error handling.

## Target

- Type: built npm package artifact and live REST API
- Launch or access: install the tarball produced by `pnpm pack` into a repository-external, permission-restricted TypeScript/Node consumer; exercise the live backend through its documented HTTP endpoints
- Allowed fixtures and credential source: a custom `fetch` boundary with synthetic success/error JSON for artifact validation, and ephemeral live-test credentials created by the lifecycle script; no repository source, workspace symlink, source fixture, diff, history, implementation note, or credential value

## User Tasks

1. Create collections in two distinct vault/path/summary combinations, including omitted `summary`, explicit `null`, and a repeated idempotent create.
2. Delete empty and non-empty collections with omitted options, `recursive: false`, and `recursive: true`.
3. Delete nested paths containing spaces, Korean text, and reserved characters while preserving `/` hierarchy separators.
4. Refuse destructive delete paths containing `.` or `..` segments before any request is sent.
5. Exercise all typed methods through an `actingAs()` vault-scoped client with bearer authentication.
6. Exercise backend 400, 401, 403, 404, 409, and 422 responses as `{data:null,error}` and through `.throwOnError()`.
7. Compile a public TypeScript consumer that uses collection input/options/summary/create/delete types, operation response mappings, literal `kind` narrowing, and the `AkbSuccessEnvelope` union without `unknown` or a generic JSON fallback.
8. Exercise live create, idempotent create, non-empty 409, recursive delete, and missing 404 through REST.

## Expected Observable Behavior

- Create sends exactly `POST /collections/<encoded-vault>` with JSON `{path}` or `{path,summary}`; omitted `summary` is absent and explicit `null` is preserved.
- Create returns the flat backend payload with required literal `kind: "collection_create"`, `ok`, `created`, and the exact nested collection summary including null and zero values.
- Delete sends exactly `DELETE /collections/<encoded-vault>/<encoded-path-segments>`; every vault/path segment is encoded once and `/` hierarchy separators remain separators.
- Delete paths containing an exact `.` or `..` segment throw before `fetch`, preventing URL normalization from retargeting a destructive request.
- Delete serializes `?recursive=true` only for `recursive: true`; omitted options and `recursive: false` serialize no query.
- Delete returns the flat backend payload with required literal `kind: "collection_delete"`, exact collection path, and all four numeric deleted counts including zero values.
- The shared request path sends `Authorization: Bearer <redacted>`, `X-Akb-Claims`, and JSON content type when a body exists.
- Backend errors remain the existing `AkbResult` error and are thrown only by `.throwOnError()`; 409 exposes all four non-empty counts under `AkbError.details` while legacy `detail` remains observable.
- Existing `.docs.browse()`, document CRUD, and raw `.docs.request()` behavior remains available.
- Public generated types map both operations directly to their leaf envelopes and narrow the success union by the two literal kinds.
- Live REST responses match the OpenAPI leaf contract and preserve idempotent and lifecycle semantics.

## Anti-Cheat Probes

- Use at least two distinct vault, path, summary, and count sets and assert captured requests and returned values change with the inputs.
- Compare omitted `summary` with explicit `null`, and omitted options with explicit `recursive: false`.
- Use spaces, Korean text, `%`, `?`, and `#` in nested path segments and assert each is encoded exactly once.
- Probe leading, trailing, and nested `.`/`..` segments and assert the fetch boundary is not called.
- Capture method, URL, headers, body presence/content, response kinds/counts, result errors, thrown errors, and public TypeScript compile output from the installed tarball.
- Install from the tarball only in a repository-external directory with restrictive permissions; reject repository path imports, workspace links, and source fixture access.
- Repeat live creation and deletion observations to prove persisted backend state rather than static success output.

## Evidence Required

- Reproducible redacted shell commands and exit statuses for pack, external install, typecheck, runtime execution, and live lifecycle execution.
- Redacted terminal observations covering exact URLs/methods/bodies, omitted fields/options, auth/claims presence, literal kinds, varied payload/count markers, 409 `details`, 404, `.throwOnError()`, and TypeScript narrowing.
- A structured behavior report in the external validator directory with every clause marked pass, fail, blocked, or out of scope.
- Screenshot: not applicable because this is a non-visual SDK/API surface.

## Out Of Scope

- Collection service/repository algorithms, MCP response changes, broader client-side path validation beyond destructive URL dot-segment safety, browse reimplementation, move/rename/preview, frontend/UI, package version/changelog/release, deployment, and merge.
