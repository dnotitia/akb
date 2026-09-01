# @akb/client

Reference AKB REST client boundary for the hybrid response contract.

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
