import assert from "node:assert/strict";

import { AkbError, createClient } from "@akb/client";

const calls = [];
const claims = {
  sub: "artifact-user",
  app_metadata: { org_id: "artifact-org", role: "admin" },
};
const root = createClient({
  baseUrl: "https://public.example/api/v1",
  token: () => ["artifact", "token"].join("-"),
  fetch: async (input, init = {}) => {
    const method = init.method ?? "GET";
    const headers = Object.fromEntries(new Headers(init.headers));
    const body = init.body === undefined ? undefined : JSON.parse(String(init.body));
    calls.push({ url: String(input), method, headers, body });
    if (String(input).endsWith("/denied")) {
      return new Response(JSON.stringify({ message: "denied", code: "permission_denied" }), {
        status: 403,
        headers: { "content-type": "application/json" },
      });
    }
    const pathname = new URL(String(input)).pathname;
    const kind = pathname.endsWith("/migrations")
      ? "table_migration"
      : pathname.endsWith("/schema") && pathname.split("/").at(-3) !== "tables"
        ? "table_schema"
        : pathname.endsWith("/schema")
          ? "vault_table_schema"
          : "table";
    return new Response(JSON.stringify({ kind, tables: [], total: 0 }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  },
}).actingAs(claims);

const alpha = root.vault("vault one");
const beta = root.vault("한글/둘");
const createInput = {
  name: "incidents",
  description: null,
  columns: [{ name: "state", type: "text" }],
};
const alterInput = {
  add_columns: [{ name: "owner", type: "text" }],
  drop_columns: ["legacy"],
};
const operations = [
  { op: "add_column", table: "incidents", name: "priority", type: "text" },
  { op: "drop_index", table: "incidents", name: "old_idx" },
];

assert.equal((await alpha.tables.list()).throwOnError().data.kind, "table");
assert.equal((await alpha.tables.schema()).throwOnError().data.kind, "vault_table_schema");
assert.equal((await alpha.tables.schema("incident/type")).throwOnError().data.kind, "table_schema");
assert.equal((await alpha.tables.create(createInput)).throwOnError().data.kind, "table");
assert.equal((await alpha.tables.alter("incident/type", alterInput)).throwOnError().data.kind, "table");
assert.equal(
  (
    await alpha.tables.migrate(operations, {
      idempotencyKey: "artifact-key-one",
    })
  ).throwOnError().data.kind,
  "table_migration",
);
assert.equal((await alpha.tables.drop("incident/type")).throwOnError().data.kind, "table");
await beta.tables.schema("테이블 name");
await beta.tables.migrate([{ op: "drop_column", table: "events", name: "legacy" }], {
  idempotencyKey: "artifact-key-two",
});
await alpha.tables.request("/denied");

assert.deepEqual(
  calls.slice(0, 9).map(({ url, method }) => ({ url, method })),
  [
    { url: "https://public.example/api/v1/tables/vault%20one", method: "GET" },
    { url: "https://public.example/api/v1/tables/vault%20one/schema", method: "GET" },
    {
      url: "https://public.example/api/v1/tables/vault%20one/incident%2Ftype/schema",
      method: "GET",
    },
    { url: "https://public.example/api/v1/tables/vault%20one", method: "POST" },
    {
      url: "https://public.example/api/v1/tables/vault%20one/incident%2Ftype",
      method: "PATCH",
    },
    { url: "https://public.example/api/v1/tables/vault%20one/migrations", method: "POST" },
    {
      url: "https://public.example/api/v1/tables/vault%20one/incident%2Ftype",
      method: "DELETE",
    },
    {
      url: "https://public.example/api/v1/tables/%ED%95%9C%EA%B8%80%2F%EB%91%98/%ED%85%8C%EC%9D%B4%EB%B8%94%20name/schema",
      method: "GET",
    },
    {
      url: "https://public.example/api/v1/tables/%ED%95%9C%EA%B8%80%2F%EB%91%98/migrations",
      method: "POST",
    },
  ],
);
assert.deepEqual(calls[3].body, createInput);
assert.deepEqual(calls[4].body, alterInput);
assert.deepEqual(calls[5].body, operations);
assert.equal(calls[5].headers["idempotency-key"], "artifact-key-one");
assert.equal(calls[8].headers["idempotency-key"], "artifact-key-two");
for (const call of calls) {
  assert.equal(call.headers.authorization, "Bearer artifact-token");
  assert.deepEqual(JSON.parse(call.headers["x-akb-claims"]), claims);
}

const denied = await alpha.tables.request("/denied");
assert.equal(denied.data, null);
assert.ok(denied.error instanceof AkbError);
assert.throws(() => denied.throwOnError(), AkbError);

let missingVaultCalls = 0;
const missingVault = createClient({
  baseUrl: "https://public.example/api/v1",
  fetch: async () => {
    missingVaultCalls += 1;
    return new Response("{}");
  },
});
assert.throws(() => missingVault.tables.list(), /Select a vault before using table administration/);
assert.equal(missingVaultCalls, 0);
assert.equal(Object.isFrozen(alpha), true);
assert.equal(Object.isFrozen(alpha.tables), true);

console.log("tables facade packed runtime proof passed");
