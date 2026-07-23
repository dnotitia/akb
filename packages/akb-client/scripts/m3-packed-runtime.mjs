import assert from "node:assert/strict";

import { AkbError, createClient } from "@akb/client";
import { createClient as createLiteClient } from "@akb/client/lite";

const calls = [];
const claims = {
  sub: "packed-user",
  app_metadata: { org_id: "packed-org", role: "admin" },
};
const fetch = async (input, init = {}) => {
  const url = new URL(String(input));
  const method = init.method ?? "GET";
  const headers = Object.fromEntries(new Headers(init.headers));
  const body = init.body === undefined ? undefined : JSON.parse(String(init.body));
  calls.push({ url, method, headers, body });
  if (url.pathname.endsWith("/denied")) {
    return response({ message: "denied", code: "permission_denied" }, 403, "Forbidden");
  }
  return response(payload(url, method, body));
};

const root = createClient({
  baseUrl: "https://packed.invalid/api/v1",
  token: () => "packed-contract-token",
  fetch,
});
const client = root.vault("packed vault").actingAs(claims);
const lite = createLiteClient({
  baseUrl: "https://packed.invalid/api/v1",
  defaultVault: "packed vault",
  fetch,
});
assert.equal(typeof lite.graph.neighbors, "function");
assert.equal(typeof lite.activity.list, "function");
assert.equal(typeof lite.docs.history, "function");
assert.equal(typeof lite.docs.createCollection, "function");
assert.equal(typeof lite.tables.migrate, "function");

const results = [
  await client.graph.neighbors("akb://packed vault/doc/a", { hops: 2 }),
  await client.graph.overview({ topK: 7 }),
  await client.graph.health({ hubThreshold: 3 }),
  await client.graph.relations("akb://packed vault/doc/a"),
  await client.graph.link({
    source: "akb://packed vault/doc/a",
    target: "akb://packed vault/table/t",
    relation: "references",
  }),
  await client.graph.unlink({
    source: "akb://packed vault/doc/a",
    target: "akb://packed vault/table/t",
  }),
  await client.graph.provenance("akb://packed vault/doc/a"),
  await client.activity.list({ limit: 3 }),
  await client.activity.recent({ limit: 2 }),
  await client.docs.history("a.md", { limit: 2 }),
  await client.docs.diff("a.md", { commit: "abc123" }),
  await client.docs.createCollection({ path: "new/coll" }),
  await client.docs.deleteCollection("new/coll", { recursive: true }),
  await client.tables.list(),
  await client.tables.schema(),
  await client.tables.schema("incidents"),
  await client.tables.create({ name: "incidents", columns: [{ name: "state" }] }),
  await client.tables.alter("incidents", { drop_columns: ["legacy"] }),
  await client.tables.migrate(
    [{ op: "drop_column", table: "incidents", name: "legacy" }],
    { idempotencyKey: "packed-migration" },
  ),
  await client.tables.drop("incidents"),
];

assert.equal(results.length, 20);
assert.ok(results.every((result) => result.data !== null && result.error === null));
assert.ok(results.every((result) => result.throwOnError().data.kind));
assert.ok(calls.every((call) => call.headers.authorization === "Bearer packed-contract-token"));
assert.ok(calls.every((call) => call.headers["x-akb-claims"] === JSON.stringify(claims)));
assert.equal(calls[18].headers["idempotency-key"], "packed-migration");

const rawAt = calls.length;
await client.graph.request("/raw");
await client.activity.request("/raw");
await client.docs.request("/raw");
await client.tables.request("/raw");
assert.deepEqual(
  calls.slice(rawAt).map((call) => call.url.pathname),
  [
    "/api/v1/graph/raw",
    "/api/v1/activity/raw",
    "/api/v1/documents/raw",
    "/api/v1/tables/raw",
  ],
);

const denied = await client.tables.request("/denied");
assert.equal(denied.data, null);
assert.ok(denied.error instanceof AkbError);
assert.throws(() => denied.throwOnError(), AkbError);

let missingVaultFetches = 0;
const unscoped = createClient({
  baseUrl: "https://packed.invalid/api/v1",
  fetch: async () => {
    missingVaultFetches += 1;
    return response({});
  },
});
assert.throws(() => unscoped.graph.overview(), /Select a vault/);
assert.throws(() => unscoped.activity.list(), /Select a vault/);
assert.throws(() => unscoped.docs.history("a.md"), /Select a vault/);
assert.throws(() => unscoped.docs.createCollection({ path: "a" }), /Select a vault/);
assert.throws(() => unscoped.tables.list(), /Select a vault/);
assert.equal(missingVaultFetches, 0);

console.log("M3 packed runtime proof passed: 20 operations, main/lite, raw/error/preflight.");

function payload(url, method, body) {
  if (url.pathname.endsWith("/graph")) return { kind: "graph_neighbors", nodes: [], edges: [] };
  if (url.pathname.endsWith("/graph/overview")) {
    return {
      kind: "graph_overview",
      nodes: [],
      edges: [],
      nodes_total: 0,
      edges_total: 0,
      returned: 0,
      truncated: false,
      orphans_returned: 0,
      orphans_truncated: false,
    };
  }
  if (url.pathname.endsWith("/graph/health")) {
    return { kind: "graph_health", hubs: [], orphans: { count: 0, sample: [] } };
  }
  if (url.pathname.endsWith("/relations") && method === "POST") {
    return { kind: "relation_link", linked: true, ...body };
  }
  if (url.pathname.endsWith("/relations") && method === "DELETE") {
    return {
      kind: "relation_unlink",
      unlinked: 1,
      source: url.searchParams.get("source"),
      target: url.searchParams.get("target"),
    };
  }
  if (url.pathname.endsWith("/relations")) {
    return { kind: "relations", uri: url.searchParams.get("uri"), relations: [] };
  }
  if (url.pathname.endsWith("/provenance")) {
    return {
      kind: "provenance",
      doc_id: "d-packed",
      title: "Packed",
      path: "a.md",
      vault: "packed vault",
      uri: url.searchParams.get("uri"),
      created_by: null,
      created_at: null,
      updated_at: null,
      current_commit: null,
      relations: [],
    };
  }
  if (url.pathname.includes("/activity/")) {
    return { kind: "activity", vault: "packed vault", total: 0, activity: [] };
  }
  if (url.pathname.endsWith("/recent")) return { kind: "recent_changes", changes: [] };
  if (url.pathname.includes("/history/")) {
    return { kind: "document_history", uri: "akb://packed vault/doc/a.md", history: [] };
  }
  if (url.pathname.includes("/diff/")) {
    return { kind: "document_diff", file: "a.md", commit: "abc123", type: "modified", diff: "" };
  }
  if (url.pathname.includes("/collections/") && method === "POST") {
    return {
      kind: "collection_create",
      ok: true,
      created: true,
      collection: { path: body.path, name: "coll", summary: null, doc_count: 0 },
    };
  }
  if (url.pathname.includes("/collections/")) {
    return {
      kind: "collection_delete",
      ok: true,
      collection: "new/coll",
      deleted_docs: 0,
      deleted_files: 0,
      deleted_sub_collections: 0,
      deleted_tables: 0,
    };
  }
  if (url.pathname.endsWith("/migrations")) {
    return {
      kind: "table_migration",
      vault: "packed vault",
      idempotency_key: "packed-migration",
      checksum: "fixture",
      applied: true,
      operations: 1,
      results: [],
    };
  }
  if (url.pathname.endsWith("/schema")) {
    return url.pathname.split("/").at(-2) === "packed%20vault"
      ? { kind: "vault_table_schema", vault: "packed vault", tables: [], total: 0 }
      : { kind: "table_schema", vault: "packed vault", name: "incidents", columns: [], pg_types: {}, drift: {} };
  }
  return { kind: "table" };
}

function response(body, status = 200, statusText = "OK") {
  return new Response(JSON.stringify(body), {
    status,
    statusText,
    headers: { "content-type": "application/json" },
  });
}
