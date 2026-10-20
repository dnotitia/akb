# @akb/client

Reference AKB REST client boundary for the hybrid response contract.

## Safe document updates

Replacing a whole body (including `content: ""`) requires a condition from
the read used to compose it. Metadata-only patches, including `content: null`,
do not require one. This is a breaking server contract for previously unpinned
whole-body writers; the SDK input type makes the requirement explicit.

```ts
const docs = akb.vault("example").docs;
const original = (await docs.get("guides/readme.md")).throwOnError().data;
const originalCommit = original.current_commit;
if (!originalCommit || typeof original.content !== "string") {
  throw new Error("The original read needs a complete body and revision token.");
}
const draft = `${original.content}\nReviewed additions`;
const saved = await docs.update("guides/readme.md", {
  content: draft,
  expectedCommit: originalCommit,
});
```

Use the document API accessor configured for your Vault (as in the examples
below). `expectedContentHash` is an alternative that protects only body
equality, not metadata or intermediate A→B→A changes. Both supplied conditions
must match. On conflict, re-read and deliberately merge/recompose the draft;
never fetch a newer revision just to submit an older-derived body. The client
does not automatically fetch versions, retry conflicts or force an overwrite.

### Recovering a conflict

Keep `original` and `draft` when `saved.error?.code === "conflict"`, then read
the complete latest document with `docs.get`. Compare all three versions:
the original base, your draft, and the latest stored document. A latest read
does not merge the draft or authorize resending it with a newer token. The
SDK has no global URI-to-revision cache; each write uses the condition passed
by its caller.

For a concrete example, suppose your intent was to change `Owner: TBD` to
`Owner: Mina`. Your original base and retained draft are:

```ts
const original = (await docs.get("guides/readme.md")).throwOnError().data;
const originalCommit = original.current_commit;
if (!originalCommit || typeof original.content !== "string") {
  throw new Error("The original read needs a complete body and revision token.");
}
const oldText = "Owner: TBD";
const newText = "Owner: Mina";
if (original.content.split(oldText).length !== 2) {
  throw new Error("Review the intended owner change before composing a draft.");
}
const draft = original.content.replace(oldText, newText);
const saved = await docs.update("guides/readme.md", {
  content: draft,
  expectedCommit: originalCommit,
});
```

If that save conflicts, retain those values and fetch the recovery snapshot:

```ts
const latest = (await docs.get("guides/readme.md")).throwOnError().data;
if (typeof latest.content !== "string") throw new Error("Read the complete latest body.");
// Present these three versions for review; do not save again yet.
console.log({ base: original.content, draft, latest: latest.content });
```

Suppose review confirms that another writer added a notes section while the
owner line and its meaning stayed unchanged. As a separate action after that
review, reapply only the intended owner change to `latest.content`. This keeps
the notes section. If the owner line or its context changed, resolve that
overlap manually before composing the replacement body.

```ts
const reviewedCommit = latest.current_commit;
if (!reviewedCommit || latest.content.split(oldText).length !== 2) {
  throw new Error("Review the latest owner line and revision before saving.");
}
const composedDraft = latest.content.replace(oldText, newText);
const resolved = await docs.update("guides/readme.md", {
  content: composedDraft,
  expectedCommit: reviewedCommit,
});
resolved.throwOnError();
```

Send only metadata fields you intend to change. If the final save conflicts
again, keep this composed draft and repeat review against a new latest read;
do not loop automatically or replace only `expectedCommit`.

AKB HTTP success responses keep the backend envelope:

```json
{ "kind": "table_query", "columns": ["id"], "items": [{ "id": "r1" }], "total": 1 }
```

HTTP errors use the canonical `AkbError` shape:

```json
{ "message": "permission denied", "code": "permission_denied", "details": { "pg_sqlstate": "42501" } }
```

The client unwraps that boundary into the Supabase-style tuple:

```js
import { createClient } from "@akb/client";

const akb = createClient("https://akb.example.com/api/v1", {
  apiKey: process.env.AKB_SERVICE_KEY,
});

const tenant = akb.vault("reef").actingAs({
  sub: "end-user-1",
  app_metadata: { org_id: "org-1", role: "member" },
});

const { data, error } = await tenant.request("/tables/reef/sql", {
  method: "POST",
  body: JSON.stringify({ sql: "SELECT * FROM incidents" }),
});

if (error) console.error(error.code, error.details);
else console.log(data.kind, data.items);
```

For call sites that prefer exceptions:

```js
const result = await akb.request("/tables/reef");
const { data } = result.throwOnError();
```

## Distribution and operational failures

The future official distribution channel for `@akb/client` is npm; this package
does not publish during its build. For an optional development installation,
use an immutable Git revision. The `prepare` lifecycle builds the required
`dist/` entrypoints:

```json
{
  "dependencies": {
    "@akb/client": "git+https://github.com/dnotitia/akb.git#<full-40-character-sha>&path:/packages/akb-client"
  }
}
```

With pnpm 11.10.0, approve only the resolved Git artifact in the consumer's
`pnpm-workspace.yaml`:

```yaml
allowBuilds:
  "@akb/client@https://codeload.github.com/dnotitia/akb/tar.gz/<full-40-character-sha>#path:/packages/akb-client": true
```

All HTTP transport, cancellation, response-read, and JSON-parse failures are
returned as `{ data: null, error: AkbError }`. SDK-local errors use these
stable codes: `transport_error`, `request_aborted`, `response_read_error`, and
`invalid_json`. `error.status` and `error.response` preserve an HTTP response
when one exists; transport failures use `null` rather than a fabricated status.
The SDK does not copy a credential, request URL/body, or signal reason into
these errors. Injected fetch rejections are covered by this result boundary;
invalid input/configuration and token-supplier or user-callback failures keep
their native exception boundary.

Pass a request-scoped signal to control-plane operations through their final
options object (list options also accept `signal`), or to storage operations
through their options:

```ts
const signal = new AbortController().signal;
await admin.apps.get("app-id", { signal });
await app.inventory.list({ limit: 10, signal });
await exchangeAppCredential({ baseUrl, credential, signal });
```

Vault-scoped table administration is available through the immutable `.tables`
facade. Request bodies use the generated public types, and migrations require
an explicit idempotency key:

```ts
const tables = akb.vault("reef").tables;

const created = await tables.create({
  name: "incidents",
  columns: [{ name: "state", type: "text" }],
});

const schema = await tables.schema("incidents");
const migrated = await tables.migrate(
  [{ op: "add_column", table: "incidents", name: "owner", type: "text" }],
  { idempotencyKey: crypto.randomUUID() },
);

created.throwOnError();
schema.throwOnError();
migrated.throwOnError();
```

The backend and MCP surfaces are not rewrapped. `kind` remains the HTTP success discriminator and `{ data, error }` exists only at the SDK boundary.

## Runtime Table Types

AKB user tables are created at runtime, so table row types come from the live schema introspection endpoint rather than static OpenAPI:

```bash
akb gen types --vault eng --url https://akb.example.com > akb.types.ts
```

The generated file exports Supabase-style `Database` and `AkbSchema` types:

```ts
import { createClient } from "@akb/client";
import type { AkbSchema } from "./akb.types";

const akb = createClient<AkbSchema>({
  baseUrl: "https://akb.example.com/api/v1",
  token: process.env.AKB_TOKEN,
});
```

For CI drift checks, regenerate and compare the committed file:

```bash
akb gen types --vault eng --url "$AKB_URL" --check akb.types.ts
```

## Internal OpenAPI Core

`src/core/schema.gen.ts` is the generated low-level REST operation map used
by `createTypedFetch` (compiled to `dist/core/schema.gen.{js,d.ts}`). It stays
separate from runtime table types: OpenAPI types describe AKB endpoints, while
`akb gen types` describes user tables inside a vault.
