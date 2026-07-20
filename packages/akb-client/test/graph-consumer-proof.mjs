import assert from "node:assert/strict";

import { AkbError, createClient } from "@akb/client";

const calls = [];
const token = ["proof", "token"].join("-");
const claims = { sub: "proof-user", app_metadata: { org_id: "proof-org", role: "reader" } };

const fetchBoundary = async (input, init) => {
  const url = new URL(String(input));
  const headers = Object.fromEntries(new Headers(init?.headers));
  calls.push({ url: url.href, method: init?.method ?? "GET", headers });

  if (url.pathname.endsWith("/graph/health") && url.searchParams.get("limit") === "999") {
    return json({ detail: "limit must be less than or equal to 200", code: "validation_error" }, 422, "Unprocessable Entity");
  }
  if (url.pathname.endsWith("/graph/health")) {
    const vault = url.searchParams.get("vault");
    return json({
      kind: "graph_health",
      hubs: [{ uri: `akb://${vault}/doc/hub`, name: `hub-${vault}`, resource_type: "doc", degree: Number(url.searchParams.get("hub_threshold") ?? 5) }],
      orphans: { count: vault === "scope/two" ? 2 : 1, sample: [{ uri: `akb://${vault}/doc/orphan`, name: "orphan", resource_type: "doc" }] },
    });
  }
  if (url.pathname.endsWith("/graph/overview") || (url.pathname.endsWith("/graph/") && url.searchParams.has("vault"))) {
    const vault = url.searchParams.get("vault");
    return json({
      kind: "graph_overview",
      nodes: [{ uri: `akb://${vault}/doc/top`, name: `top-${vault}`, resource_type: "doc", degree: Number(url.searchParams.get("top_k") ?? 200) }],
      edges: [],
      nodes_total: vault === "default one" ? 11 : 22,
      edges_total: 3,
      returned: 1,
      truncated: true,
      orphans_returned: 1,
      orphans_truncated: false,
    });
  }
  if (url.pathname.endsWith("/graph")) {
    const uri = url.searchParams.get("uri");
    return json({
      kind: "graph_neighbors",
      nodes: [{ uri, name: `node-${url.searchParams.get("hops") ?? "default"}`, resource_type: "doc", depth: 0 }],
      edges: [{ source: uri, target: `${uri}#target`, relation: "links_to", kind: "explicit" }],
    });
  }
  throw new Error(`unexpected request ${url.href}`);
};

const client = createClient({
  baseUrl: "https://proof.invalid/api/v1",
  token: () => token,
  defaultVault: "default one",
  fetch: fetchBoundary,
}).actingAs(claims);

const neighborsA = await client.graph.neighbors("akb://default one/doc/a b", { hops: 3, limit: 17 });
const overviewA = await client.graph.overview({ topK: 37 });
const healthA = await client.vault("scope/two").graph.health({ hubThreshold: 7, limit: 11 });
const neighborsB = await client.vault("scope/two").graph.neighbors("akb://scope/two/table/b");
const overviewB = await client.vault("scope/two").graph.overview();
const healthError = await client.vault("scope/two").graph.health({ limit: 999 });
const raw = await client.vault("scope/two").graph.request("?vault=scope%2Ftwo");

assert.equal(calls[0].url, "https://proof.invalid/api/v1/graph?uri=akb%3A%2F%2Fdefault+one%2Fdoc%2Fa+b&hops=3&limit=17");
assert.equal(calls[1].url, "https://proof.invalid/api/v1/graph/overview?vault=default+one&top_k=37");
assert.equal(calls[2].url, "https://proof.invalid/api/v1/graph/health?vault=scope%2Ftwo&hub_threshold=7&limit=11");
assert.equal(calls[3].url, "https://proof.invalid/api/v1/graph?uri=akb%3A%2F%2Fscope%2Ftwo%2Ftable%2Fb");
assert.equal(calls[4].url, "https://proof.invalid/api/v1/graph/overview?vault=scope%2Ftwo");
assert.equal(calls[5].url, "https://proof.invalid/api/v1/graph/health?vault=scope%2Ftwo&limit=999");
assert.equal(calls[6].url, "https://proof.invalid/api/v1/graph/?vault=scope%2Ftwo");
assert.ok(calls.every((call) => call.method === "GET"));
assert.ok(calls.every((call) => call.headers.authorization === `Bearer ${token}`));
assert.ok(calls.every((call) => call.headers["x-akb-claims"] === JSON.stringify(claims)));
assert.equal(neighborsA.throwOnError().data.nodes[0].name, "node-3");
assert.equal(neighborsB.throwOnError().data.nodes[0].name, "node-default");
assert.equal(overviewA.throwOnError().data.nodes_total, 11);
assert.equal(overviewB.throwOnError().data.nodes_total, 22);
assert.equal(healthA.throwOnError().data.hubs[0].degree, 7);
assert.equal(healthA.throwOnError().data.orphans.count, 2);
assert.equal(healthError.data, null);
assert.equal(healthError.error?.status, 422);
assert.equal(healthError.error?.code, "validation_error");
assert.throws(() => healthError.throwOnError(), AkbError);
assert.equal(raw.throwOnError().data.kind, "graph_overview");

let noVaultCalls = 0;
const root = createClient({
  baseUrl: "https://proof.invalid/api/v1",
  fetch: async () => {
    noVaultCalls += 1;
    return json({});
  },
});
assert.throws(() => root.graph.overview(), TypeError);
assert.throws(() => root.graph.health(), TypeError);
assert.equal(noVaultCalls, 0);

console.log(JSON.stringify({
  overall_behavior: "satisfies_contract",
  calls: calls.map(({ url, method, headers }) => ({
    url,
    method,
    authorization: headers.authorization?.startsWith("Bearer ") ? "Bearer <redacted>" : null,
    claims: headers["x-akb-claims"] ? "present" : null,
  })),
  kinds: [neighborsA.data?.kind, overviewA.data?.kind, healthA.data?.kind, raw.data?.kind],
  varying_payloads: [neighborsA.data?.nodes[0].name, neighborsB.data?.nodes[0].name, overviewA.data?.nodes_total, overviewB.data?.nodes_total],
  error: { status: healthError.error?.status, code: healthError.error?.code, throwOnError: "AkbError" },
  no_vault: { fetch_calls: noVaultCalls, error: "TypeError" },
  screenshot: "not_applicable_non_visual_sdk",
}, null, 2));

function json(body, status = 200, statusText = "OK") {
  return new Response(JSON.stringify(body), {
    status,
    statusText,
    headers: { "content-type": "application/json" },
  });
}
