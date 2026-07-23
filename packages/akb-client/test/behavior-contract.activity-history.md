# AKB-108 Activity and History Facade Behavior Contract

## User-visible goal

An `@akb/client` consumer can use `.docs.history()`, `.docs.diff()`, `.activity.list()`, and `.activity.recent()` through the public packed package without raw fetches, while retaining the shared request/result contracts.

## Observable behavior

- History and diff encode the selected vault and every document path segment exactly once; diff requires and serializes `commit`, while history serializes `limit` only when supplied.
- Activity list encodes its selected vault and omits `collection`, `author`, `since`, and `limit` when each value is `null` or `undefined`.
- Activity recent uses an explicit vault before a scoped/default vault. A root client with no vault omits the `vault` query to preserve cross-vault semantics.
- History, diff, and list throw a clear `TypeError` before fetch when no explicit/scoped/default vault exists.
- All four methods use `GET`, shared bearer/`X-Akb-Claims` handling, `{data,error}`, and `.throwOnError()`.
- `.activity.request()` remains available under the `/activity` namespace.
- Generated response mappings and `AkbSuccessEnvelope` expose the literal kinds `document_history`, `document_diff`, `activity`, and `recent_changes`, including required nested fields and the backend's optional/nullable distinctions.
- TypeScript rejects diff calls without `commit` and options not supported by the selected method.

## Source-blind proof

Produce a tarball with `pnpm pack`, install only that tarball in a repository-external directory with mode `700`, run the runtime consumer against a mock HTTP boundary, and compile the strict TypeScript consumer with TypeScript 5.9.3 and NodeNext settings. Do not import repository source files, use workspace links, or retain credentials and absolute user paths in the transcript.

## Out of scope

Backend/frontend changes, package version or changelog updates, release/deploy work, realtime notifications, and history writes.
