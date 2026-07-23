import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "vitest";

import { AkbError, createClient } from "../dist/index.js";
import { createClient as createLiteClient } from "../dist/lite.js";

const contract = JSON.parse(
  await readFile(new URL("../scripts/sdk-surface-contract.json", import.meta.url), "utf8"),
);
const CONTRACT_PATH_PARAMETERS = {
  activityList: { vault: "contract vault" },
  documentsHistory: { vault: "contract vault", doc_id: "notes/a b.md" },
  documentsDiff: { vault: "contract vault", doc_id: "notes/a b.md" },
  collectionsCreateCollection: { vault: "contract vault" },
  collectionsDeleteCollection: { vault: "contract vault", path: "notes/new" },
  tablesGetVault: { vault: "contract vault" },
  tablesGetVaultSchema: { vault: "contract vault" },
  tablesGetTableSchema: { vault: "contract vault", table: "incident/type" },
  tablesPostVault: { vault: "contract vault" },
  tablesAlterTable: { vault: "contract vault", table_name: "incidents" },
  tablesApplyMigration: { vault: "contract vault" },
  tablesDeleteTableName: { vault: "contract vault", table_name: "incidents" },
};

test("SDK surface matrix connects every contract facade through one scoped client", async () => {
  const calls = [];
  const claims = {
    sub: "contract-user",
    app_metadata: { org_id: "contract-org", role: "admin" },
  };
  const fetch = async (input, init = {}) => {
    const url = new URL(String(input));
    const method = init.method ?? "GET";
    const headers = Object.fromEntries(new Headers(init.headers));
    const body = init.body === undefined ? undefined : JSON.parse(String(init.body));
    calls.push({ url, method, headers, body });
    if (url.pathname.endsWith("/denied")) {
      return json({ message: "denied", code: "permission_denied" }, 403, "Forbidden");
    }
    return json(responseFor(url, method, body));
  };
  const root = createClient({
    baseUrl: "https://contract.invalid/api/v1",
    token: () => "contract-token",
    fetch,
  });
  const client = root.vault("contract vault").actingAs(claims);
  const lite = createLiteClient({ baseUrl: "https://contract.invalid/api/v1", fetch });

  for (const item of contract.operations) {
    assert.equal(typeof client[item.facade.namespace][item.facade.method], "function", item.operationId);
    assert.equal(typeof lite[item.facade.namespace][item.facade.method], "function", item.operationId);
  }

  const results = [
    await client.graph.neighbors("akb://contract vault/doc/a b", { hops: 2, limit: 9 }),
    await client.graph.overview({ topK: 12 }),
    await client.graph.health({ hubThreshold: 4, limit: 8 }),
    await client.graph.relations("akb://contract vault/doc/a b", { direction: "both", type: "references" }),
    await client.graph.link({
      source: "akb://contract vault/doc/a b",
      target: "akb://contract vault/table/t",
      relation: "references",
      metadata: { confidence: 1 },
    }),
    await client.graph.unlink({
      source: "akb://contract vault/doc/a b",
      target: "akb://contract vault/table/t",
      relation: "references",
    }),
    await client.graph.provenance("akb://contract vault/doc/a b"),
    await client.activity.list({ collection: "notes", author: "contract-user", limit: 6 }),
    await client.activity.recent({ limit: 5 }),
    await client.docs.history("notes/a b.md", { limit: 4 }),
    await client.docs.diff("notes/a b.md", { commit: "abc?123" }),
    await client.docs.createCollection({ path: "notes/new", summary: null }),
    await client.docs.deleteCollection("notes/new", { recursive: true }),
    await client.tables.list(),
    await client.tables.schema(),
    await client.tables.schema("incident/type"),
    await client.tables.create({ name: "incidents", columns: [{ name: "state" }] }),
    await client.tables.alter("incidents", { drop_columns: ["legacy"] }),
    await client.tables.migrate(
      [{ op: "drop_column", table: "incidents", name: "legacy" }],
      { idempotencyKey: "contract-idempotency" },
    ),
    await client.tables.drop("incidents"),
  ];

  assert.equal(results.length, contract.operations.length);
  assert.ok(results.every((result) => result.data !== null && result.error === null));
  assert.ok(results.every((result) => result.throwOnError().data.kind));
  for (const [index, item] of contract.operations.entries()) {
    assert.equal(calls[index].method, item.method.toUpperCase(), `${item.operationId} method`);
    assert.equal(calls[index].url.pathname, contractPath(item), `${item.operationId} path`);
  }
  assert.ok(calls.every((call) => call.headers.authorization === "Bearer contract-token"));
  assert.ok(calls.every((call) => call.headers["x-akb-claims"] === JSON.stringify(claims)));
  assert.equal(calls[4].headers["content-type"], "application/json");
  assert.deepEqual(calls[4].body, {
    source: "akb://contract vault/doc/a b",
    target: "akb://contract vault/table/t",
    relation: "references",
    metadata: { confidence: 1 },
  });
  assert.deepEqual(calls[11].body, { path: "notes/new", summary: null });
  assert.deepEqual(calls[16].body, { name: "incidents", columns: [{ name: "state" }] });
  assert.deepEqual(calls[17].body, { drop_columns: ["legacy"] });
  assert.equal(calls[18].headers["idempotency-key"], "contract-idempotency");

  const rawStart = calls.length;
  await client.graph.request("/raw");
  await client.activity.request("/raw");
  await client.docs.request("/raw");
  await client.tables.request("/raw");
  assert.deepEqual(
    calls.slice(rawStart).map((call) => call.url.pathname),
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

  let missingVaultCalls = 0;
  const unscoped = createClient({
    baseUrl: "https://contract.invalid/api/v1",
    fetch: async () => {
      missingVaultCalls += 1;
      return json({});
    },
  });
  assert.throws(() => unscoped.graph.overview(), /Select a vault/);
  assert.throws(() => unscoped.activity.list(), /Select a vault/);
  assert.throws(() => unscoped.docs.history("a.md"), /Select a vault/);
  assert.throws(() => unscoped.docs.createCollection({ path: "a" }), /Select a vault/);
  assert.throws(() => unscoped.tables.list(), /Select a vault/);
  assert.equal(missingVaultCalls, 0);
});

function responseFor(url, method, body) {
  if (url.pathname.endsWith("/graph")) {
    return { kind: "graph_neighbors", nodes: [], edges: [] };
  }
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
      doc_id: "d-contract",
      title: "Contract",
      path: "a.md",
      vault: "contract vault",
      uri: url.searchParams.get("uri"),
      created_by: null,
      created_at: null,
      updated_at: null,
      current_commit: null,
      relations: [],
    };
  }
  if (url.pathname.includes("/activity/")) {
    return { kind: "activity", vault: "contract vault", total: 0, activity: [] };
  }
  if (url.pathname.endsWith("/recent")) return { kind: "recent_changes", changes: [] };
  if (url.pathname.includes("/history/")) {
    return { kind: "document_history", uri: "akb://contract vault/doc/a.md", history: [] };
  }
  if (url.pathname.includes("/diff/")) {
    return { kind: "document_diff", file: "a.md", commit: "abc", type: "modified", diff: "" };
  }
  if (url.pathname.includes("/collections/") && method === "POST") {
    return {
      kind: "collection_create",
      ok: true,
      created: true,
      collection: { path: body.path, name: "new", summary: body.summary, doc_count: 0 },
    };
  }
  if (url.pathname.includes("/collections/")) {
    return {
      kind: "collection_delete",
      ok: true,
      collection: "notes/new",
      deleted_docs: 0,
      deleted_files: 0,
      deleted_sub_collections: 0,
      deleted_tables: 0,
    };
  }
  if (url.pathname.endsWith("/migrations")) {
    return {
      kind: "table_migration",
      vault: "contract vault",
      idempotency_key: "contract-idempotency",
      checksum: "fixture",
      applied: true,
      operations: 1,
      results: [],
    };
  }
  if (url.pathname.endsWith("/schema")) {
    const table = url.pathname.split("/").at(-2);
    return table === "contract%20vault"
      ? { kind: "vault_table_schema", vault: "contract vault", tables: [], total: 0 }
      : { kind: "table_schema", vault: "contract vault", name: table, columns: [], pg_types: {}, drift: {} };
  }
  return { kind: "table" };
}

function json(body, status = 200, statusText = "OK") {
  return new Response(JSON.stringify(body), {
    status,
    statusText,
    headers: { "content-type": "application/json" },
  });
}

function contractPath(item) {
  const parameters = CONTRACT_PATH_PARAMETERS[item.operationId] ?? {};
  return item.path.replace(/\{([^}]+)\}/g, (_, name) => {
    const value = parameters[name];
    assert.equal(typeof value, "string", `${item.operationId} ${name} path parameter`);
    return name === "doc_id" || name === "path"
      ? value.split("/").map(encodeURIComponent).join("/")
      : encodeURIComponent(value);
  });
}
