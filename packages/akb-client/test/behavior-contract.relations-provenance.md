# AKB-105 Relation and Provenance Behavior Contract

## User-Visible Goal

An `@akb/client` consumer can read, create, remove, and trace AKB relations through typed `.graph` methods without assembling REST paths, request bodies, or relation vocabularies by hand.

## Target

- Type: built npm package artifact
- Launch or access: install the tarball produced by `pnpm pack` into a repository-external, permission-restricted TypeScript/Node consumer
- Allowed fixtures and credential source: a custom `fetch` boundary and synthetic success/error JSON responses; no credentials and no repository source, diff, history, tests, or implementation notes

## User Tasks

1. Call `graph.relations()` with two canonical URIs and varied direction/type options, and once without options.
2. Call `graph.link()` with two source/target/relation sets, once with metadata and once without it.
3. Call named and relation-omitted `graph.unlink()`, including repeated omitted-relation deletion of the same endpoint pair.
4. Call `graph.provenance()` with two document URIs and consume the flat payload.
5. Exercise all four methods through an `actingAs()` client with bearer authentication.
6. Exercise invalid-URI and reader-write 4xx responses as `{data,error}` and through `.throwOnError()`.
7. Compile a public TypeScript consumer that accepts `links_to` for read filters and returned rows but rejects it for `AkbWritableRelationType`, `link()`, and named `unlink()`.

## Expected Observable Behavior

- Relations sends exactly `GET /relations?uri=<encoded>` plus only provided `direction` and `type`; omitted options serialize neither backend defaults nor an empty filter.
- Link sends exactly `POST /relations` with a JSON body containing `source`, `target`, `relation`, and only provided `metadata`.
- Unlink sends exactly `DELETE /relations?source=<encoded>&target=<encoded>` plus only a provided `relation`, with no request body.
- Repeating relation-omitted unlink preserves both successful `unlinked: 1` and idempotent `unlinked: 0` envelopes.
- Provenance sends exactly `GET /provenance?uri=<encoded>` and returns the backend's flat document payload without a wrapper.
- The shared request path sends `Authorization: Bearer <redacted>` and `X-Akb-Claims` on every request.
- Success envelopes retain literal kinds `relations`, `relation_link`, `relation_unlink`, and `provenance` while URI, direction, relation, metadata, and response values vary with the fixtures.
- Backend 4xx remains the existing `{data: null, error}` result and is thrown only by `.throwOnError()`; the facade does not reinterpret it.
- Public types expose all four leaf operation responses and nullable provenance fields without `unknown` or a generic success fallback.

## Anti-Cheat Probes

- Use at least two distinct URI, direction, relation, metadata, and response sets and assert captured requests and returned values change accordingly.
- Verify omitted direction, type, metadata, and unlink relation are absent rather than serialized as empty or undefined values.
- Inspect DELETE request initialization and assert no body property is present.
- Repeat relation-omitted unlink and verify the observed count changes from 1 to 0.
- Exercise at least two distinct 4xx classes and verify both result and throw behavior.
- Compile both accepted read vocabulary and `@ts-expect-error` write vocabulary probes from the installed package.

## Evidence Required

- Reproducible redacted shell commands for pack, external install, typecheck, and runtime execution.
- Redacted terminal observations covering exact URLs, methods, POST body, DELETE body absence, omitted options, auth/claims presence, literal kinds, flat provenance fields, `unlinked: 1 -> 0`, 4xx status/code, throw behavior, and TypeScript compile result.
- Screenshot: not applicable because this is a non-visual SDK/API surface.

## Out Of Scope

- Backend service, route, RBAC, URI validation, cross-vault policy, new relation kinds, entity provenance, existing graph query behavior, UI/browser behavior, and package release/version/changelog.
