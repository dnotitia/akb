# AKB-104 Behavior Contract

## User-Visible Goal

An `@akb/client` consumer can query graph neighbors, overview, and health through typed `.graph` methods without manually assembling REST paths or query parameters, while retaining the existing raw graph request surface.

## Target

- Type: built npm package artifact
- Launch or access: install the package tarball produced by `pnpm pack` into a repository-external temporary TypeScript/Node consumer
- Allowed fixtures: a custom `fetch` boundary and synthetic success/error JSON responses; no credentials

## User Tasks

1. Call `graph.neighbors()` with two different canonical URIs and option sets, and once without options.
2. Call `graph.overview()` and `graph.health()` through both a scoped client and a root client with `defaultVault`, including omitted options.
3. Call overview and health on a root client without any vault.
4. Call all three typed methods through an `actingAs()` client with bearer authentication.
5. Exercise success, 4xx `{data,error}`, and `.throwOnError()` behavior with varying graph payloads.
6. Call raw `graph.request()` with a relative path and consume its public raw graph union type.
7. Compile a public TypeScript consumer that accepts hops 1..5, rejects hops 0/6 and `depth`, narrows leaf `kind`, and reads graph node/edge/overview/health fields.

## Expected Observable Behavior

- Neighbors sends exactly `GET /graph?uri=<encoded>` plus only provided `hops` and `limit` parameters.
- Overview sends exactly `GET /graph/overview?vault=<encoded>` plus only provided `top_k`.
- Health sends exactly `GET /graph/health?vault=<encoded>` plus only provided `hub_threshold` and `limit`.
- Omitted options do not serialize backend defaults.
- Missing vault throws a descriptive `TypeError` before `fetch` is called.
- The shared request path sends `Authorization: Bearer <token>` and `X-Akb-Claims` for `actingAs()`.
- Successful leaf envelopes retain literal kinds `graph_neighbors`, `graph_overview`, and `graph_health` with varying nodes, edges, hubs, orphans, and counts.
- Backend 4xx is returned as the existing `AkbResult` error and is thrown only by `.throwOnError()`; it is not hidden or reinterpreted.
- Raw relative requests retain the `/graph` namespace prefix and expose the public `AkbGraphEnvelope` union.
- The public TypeScript consumer compiles with the expected accepted and rejected option shapes.

## Anti-Cheat Probes

- Use at least two distinct URI/vault/option/payload sets and assert captured requests and returned values change with the inputs.
- Mix scoped and default vault clients and verify the encoded vault in each captured URL.
- Exercise omitted, invalid-server-range, and 4xx inputs; verify facade-side behavior differs only for the documented no-vault preflight.
- Count fetch calls around no-vault failures to prove no hidden network request.
- Capture method, URL, headers, response envelope, result error, thrown error, and raw request path from the built artifact.

## Evidence Required

- Reproducible shell commands for pack/install/build/run in a repository-external temporary directory.
- Redacted terminal observations summarizing captured URLs, methods, relevant header names/presence, literal kinds, varying payload markers, error status/code/message, throw behavior, raw path, and TypeScript compile result.
- Screenshot: not applicable because this is a non-visual SDK/API surface.

## Out Of Scope

- Backend/live deployment behavior, UI/browser behavior, graph visualization, relation writes, provenance facade, and package release/version/changelog.
