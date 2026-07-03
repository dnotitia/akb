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
