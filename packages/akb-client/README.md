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

const akb = createClient({ baseUrl: "https://akb.example.com/api/v1", token: process.env.AKB_TOKEN });

const { data, error } = await akb.request("/tables/reef/sql", {
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
