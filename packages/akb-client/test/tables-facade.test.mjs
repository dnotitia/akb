import assert from "node:assert/strict";
import { test } from "vitest";

import { AkbError, createClient } from "../src/index.js";

const fixtureToken = ["tables", "token"].join("-");
const claims = {
  sub: "tables-user",
  app_metadata: { org_id: "tables-org", role: "admin" },
};

function responseFor(url, method) {
  const pathname = new URL(url).pathname;
  if (pathname.endsWith("/migrations")) {
    return { kind: "table_migration", vault: "ignored", applied: [], total: 0 };
  }
  if (pathname.endsWith("/schema")) {
    const segments = pathname.split("/");
    return segments.at(-2) === "schema" || segments.at(-3) === "tables"
      ? { kind: "vault_table_schema", vault: "ignored", tables: [], total: 0 }
      : { kind: "table_schema", vault: "ignored", name: "ignored", columns: [] };
  }
  return { kind: "table", vault: "ignored", tables: method === "GET" ? [] : undefined };
}

test("tables facade maps every typed operation with exact encoded paths, bodies, and headers", async () => {
  const calls = [];
  const root = createClient({
    baseUrl: "https://akb.test/api/v1",
    token: fixtureToken,
    fetch: async (input, init = {}) => {
      const method = init.method ?? "GET";
      calls.push({
        url: String(input),
        method,
        headers: Object.fromEntries(new Headers(init.headers)),
        body: init.body === undefined ? undefined : JSON.parse(String(init.body)),
      });
      return new Response(JSON.stringify(responseFor(String(input), method)), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    },
  }).actingAs(claims);

  const first = root.vault("vault one");
  const second = root.vault("한글/공간");
  const createInput = {
    name: "incidents",
    description: null,
    columns: [{ name: "state", type: "text", nullable: false }],
  };
  const alterInput = {
    add_columns: [{ name: "owner", type: "text" }],
    rename_columns: { state: "status" },
  };
  const operations = [
    { op: "add_column", table: "incidents", name: "priority", type: "text" },
    { op: "drop_index", table: "incidents", name: "old_idx" },
  ];

  assert.equal((await first.tables.list()).throwOnError().data.kind, "table");
  assert.equal((await first.tables.schema()).throwOnError().data.kind, "vault_table_schema");
  assert.equal(
    (await first.tables.schema("incident/type")).throwOnError().data.kind,
    "table_schema",
  );
  assert.equal((await first.tables.create(createInput)).throwOnError().data.kind, "table");
  assert.equal(
    (await first.tables.alter("incident/type", alterInput)).throwOnError().data.kind,
    "table",
  );
  assert.equal(
    (
      await first.tables.migrate(operations, {
        idempotencyKey: "migration-key-one",
      })
    ).throwOnError().data.kind,
    "table_migration",
  );
  assert.equal((await first.tables.drop("incident/type")).throwOnError().data.kind, "table");
  await second.tables.schema("테이블 name");
  await second.tables.migrate([{ op: "drop_column", table: "events", name: "legacy" }], {
    idempotencyKey: "migration-key-two",
  });

  assert.deepEqual(
    calls.map(({ url, method }) => ({ url, method })),
    [
      { url: "https://akb.test/api/v1/tables/vault%20one", method: "GET" },
      { url: "https://akb.test/api/v1/tables/vault%20one/schema", method: "GET" },
      {
        url: "https://akb.test/api/v1/tables/vault%20one/incident%2Ftype/schema",
        method: "GET",
      },
      { url: "https://akb.test/api/v1/tables/vault%20one", method: "POST" },
      {
        url: "https://akb.test/api/v1/tables/vault%20one/incident%2Ftype",
        method: "PATCH",
      },
      { url: "https://akb.test/api/v1/tables/vault%20one/migrations", method: "POST" },
      {
        url: "https://akb.test/api/v1/tables/vault%20one/incident%2Ftype",
        method: "DELETE",
      },
      {
        url: "https://akb.test/api/v1/tables/%ED%95%9C%EA%B8%80%2F%EA%B3%B5%EA%B0%84/%ED%85%8C%EC%9D%B4%EB%B8%94%20name/schema",
        method: "GET",
      },
      {
        url: "https://akb.test/api/v1/tables/%ED%95%9C%EA%B8%80%2F%EA%B3%B5%EA%B0%84/migrations",
        method: "POST",
      },
    ],
  );
  assert.deepEqual(calls[3].body, createInput);
  assert.deepEqual(calls[4].body, alterInput);
  assert.deepEqual(calls[5].body, operations);
  assert.equal(calls[5].headers["idempotency-key"], "migration-key-one");
  assert.equal(calls[8].headers["idempotency-key"], "migration-key-two");
  for (const call of calls) {
    assert.equal(call.headers.authorization, `Bearer ${fixtureToken}`);
    assert.deepEqual(JSON.parse(call.headers["x-akb-claims"]), claims);
  }
  assert.equal(calls[6].body, undefined);
  assert.equal(Object.isFrozen(first), true);
  assert.equal(Object.isFrozen(first.tables), true);
});

test("tables facade supports default vault and keeps the raw /tables request prefix", async () => {
  const calls = [];
  const client = createClient({
    baseUrl: "https://akb.test/api/v1",
    defaultVault: "default/vault",
    fetch: async (input, init = {}) => {
      calls.push({ url: String(input), method: init.method ?? "GET" });
      return new Response(JSON.stringify({ kind: "table", tables: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    },
  });

  await client.tables.list();
  await client.tables.request("/raw/path?fresh=true");

  assert.deepEqual(calls, [
    { url: "https://akb.test/api/v1/tables/default%2Fvault", method: "GET" },
    { url: "https://akb.test/api/v1/tables/raw/path?fresh=true", method: "GET" },
  ]);
});

test("tables facade rejects every typed method before fetch when no vault is selected", async () => {
  let calls = 0;
  const client = createClient({
    baseUrl: "https://akb.test/api/v1",
    fetch: async () => {
      calls += 1;
      return new Response("{}");
    },
  });
  const attempts = [
    () => client.tables.list(),
    () => client.tables.schema(),
    () => client.tables.schema("incidents"),
    () => client.tables.create({ name: "incidents", columns: [] }),
    () => client.tables.alter("incidents", {}),
    () => client.tables.migrate([], { idempotencyKey: "key" }),
    () => client.tables.drop("incidents"),
  ];

  for (const attempt of attempts) {
    assert.throws(attempt, TypeError);
    assert.throws(attempt, /Select a vault before using table administration/);
  }
  assert.equal(calls, 0);
});

test("tables facade preserves permission and validation errors plus throwOnError", async () => {
  const statuses = [403, 403, 403, 422, 400];
  const codes = [
    "reader_required",
    "writer_required",
    "admin_required",
    "validation_error",
    "bad_migration",
  ];
  let call = 0;
  const client = createClient({
    baseUrl: "https://akb.test/api/v1",
    fetch: async () => {
      const index = call++;
      return new Response(JSON.stringify({ message: codes[index], code: codes[index] }), {
        status: statuses[index],
        statusText: "Rejected",
        headers: { "content-type": "application/json" },
      });
    },
  }).vault("reef");

  const results = [
    await client.tables.list(),
    await client.tables.create({ name: "incidents", columns: [] }),
    await client.tables.drop("incidents"),
    await client.tables.alter("incidents", {}),
    await client.tables.migrate([], { idempotencyKey: "bad" }),
  ];

  for (const [index, result] of results.entries()) {
    assert.equal(result.data, null);
    assert.ok(result.error instanceof AkbError);
    assert.equal(result.error.code, codes[index]);
    assert.throws(() => result.throwOnError(), AkbError);
  }
});
