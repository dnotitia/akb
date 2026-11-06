// Resilience contract for the proxy's connection lifecycle.
//
// Covers the VPN-drop failure mode: the client-visible MCP server must
// survive backend unreachability. `initialize` is answered locally,
// `tools/list` degrades gracefully, and a background monitor restores the
// full toolset (via tools/list_changed) once connectivity returns — all
// without killing the proxy process. No real network calls — backend RPCs
// are stubbed.
//
// Run with: node packages/akb-mcp-client/test/reconnect.test.mjs

import assert from "node:assert/strict";
import { AKBProxy } from "../lib/proxy.mjs";
import { parseConfiguration } from "../lib/config.mjs";

let pass = 0;
let fail = 0;
const pending = [];
function itAsync(name, fn) {
  pending.push(
    fn().then(
      () => {
        pass++;
        console.log(`  ✓ ${name}`);
      },
      (e) => {
        fail++;
        console.log(`  ✗ ${name}: ${e.stack || e.message}`);
      },
    ),
  );
}

const tick = (ms = 10) => new Promise((r) => setTimeout(r, ms));
function newProxy() {
  return new AKBProxy(parseConfiguration(["--url", "http://127.0.0.1/mcp", "--pat", "test-pat"], {}));
}
const fileToolNames = [
  "akb_put_file",
  "akb_put_image",
  "akb_discard_image",
  "akb_get_file",
  "akb_update_file",
  "akb_delete_file",
];

// ── initialize is answered locally, never blocking on the backend ────

itAsync("initialize responds locally even when the backend is down", async () => {
  const proxy = newProxy();
  proxy._startBackendMonitor = () => {}; // don't spin a real monitor here
  proxy._ensureBackend = async () => false; // backend unreachable

  const res = await proxy._initialize(1, {
    protocolVersion: "2025-06-18",
    capabilities: {},
    clientInfo: { name: "c", version: "1" },
  });

  assert.equal(res.result.protocolVersion, "2025-06-18", "echoes client protocol version");
  assert.equal(res.result.capabilities.tools.listChanged, true, "advertises listChanged");
  assert.equal(res.result.serverInfo.name, "akb-mcp");
  assert.match(res.result.instructions, /akb_put_image/);
  assert.match(res.result.instructions, /targeted akb_edit/);
  assert.match(res.result.instructions, /replaces the entire document body/);
  assert.match(res.result.instructions, /akb_discard_image/);
  assert.equal(proxy._initialized, true);
});

itAsync("initialize falls back to a default protocol version when omitted", async () => {
  const proxy = newProxy();
  proxy._startBackendMonitor = () => {};
  const res = await proxy._initialize(1, { capabilities: {} });
  assert.match(res.result.protocolVersion, /^\d{4}-\d{2}-\d{2}$/);
});

itAsync("initialize negotiates down to the supported version instead of echoing an unsupported one", async () => {
  const proxy = newProxy();
  proxy._startBackendMonitor = () => {};
  const res = await proxy._initialize(1, {
    protocolVersion: "2099-01-01",
    capabilities: {},
  });

  // Spec-compliant negotiation: respond with OUR supported revision, never
  // echo a revision we do not implement, and never hard-error — a hard error
  // breaks any client offering a different revision (e.g. Claude Code's
  // 2025-11-25 initialize).
  assert.equal(res.error, undefined);
  assert.equal(res.result.protocolVersion, "2025-06-18");
  assert.notEqual(res.result.protocolVersion, "2099-01-01");
  assert.equal(proxy._initialized, true);
});

itAsync("backend modern discovery carries vault-guide preflight without dropping client capabilities", async () => {
  const proxy = newProxy();
  const original = {
    protocolVersion: "2025-06-18",
    capabilities: {
      roots: { listChanged: true },
      experimental: { "example.test/feature": { version: 2 } },
    },
    clientInfo: { name: "client", version: "1" },
  };
  proxy._clientCapabilities = original.capabilities;
  proxy._clientInfo = original.clientInfo;
  let forwarded;
  proxy._rpc = async (method, params, options) => {
    assert.equal(method, "server/discover");
    assert.equal(options.timeoutMs, proxy._probeTimeoutMs());
    forwarded = proxy._modernizeParams(params);
    return { supportedVersions: ["2026-07-28"] };
  };

  assert.equal(await proxy._ensureBackend(), true);
  assert.deepEqual(
    forwarded._meta["io.modelcontextprotocol/clientCapabilities"].roots,
    { listChanged: true },
  );
  assert.deepEqual(
    forwarded._meta["io.modelcontextprotocol/clientCapabilities"].experimental[
      "example.test/feature"
    ],
    { version: 2 },
  );
  assert.deepEqual(
    forwarded._meta["io.modelcontextprotocol/clientCapabilities"].experimental[
      "io.dnotitia.akb/vault-skill-preflight"
    ],
    { version: 2 },
  );
  assert.equal(
    original.capabilities.experimental["io.dnotitia.akb/vault-skill-preflight"],
    undefined,
    "client initialize params are not mutated",
  );
});

// ── tools/list degrades to file tools when the backend is unreachable ─

itAsync("tools/list serves the complete generated catalog without backend access", async () => {
  const proxy = newProxy();
  proxy._startBackendMonitor = () => {};
  proxy._ensureBackend = async () => false;

  const res = await proxy._toolsList(2, {});
  const names = res.result.tools.map((t) => t.name);

  for (const name of [...fileToolNames, "akb_search", "akb_put", "akb_connection"]) assert.ok(names.includes(name));
  assert.ok(names.length > fileToolNames.length + 1);
  assert.equal(new Set(names).size, names.length);
});

itAsync("tools/list decorates the generated catalog without changing its source", async () => {
  const proxy = newProxy();
  proxy._startBackendMonitor = () => {};
  const source = structuredClone(proxy.catalog.canonical);

  const res = await proxy._toolsList(3, {});
  const names = res.result.tools.map((t) => t.name);

  for (const n of ["akb_put", "akb_search", ...fileToolNames]) {
    assert.ok(names.includes(n), `expected ${n} in tools`);
  }
  const put = res.result.tools.find((t) => t.name === "akb_put");
  assert.ok(put.inputSchema.properties.file, "file param injected into akb_put");
  const image = res.result.tools.find((t) => t.name === "akb_put_image");
  assert.match(image.description, /maximum 10 MiB/);
  assert.match(image.description, /targeted akb_edit/);
  assert.match(image.description, /replaces the entire body/);
  assert.ok(image.inputSchema.properties._vault_skill_ack);
  assert.deepEqual(proxy.catalog.canonical, source, "canonical schemas remain unchanged");
});

itAsync("modern tools/list cache metadata covers degraded and cached catalogs", async () => {
  const proxy = newProxy();
  proxy._clientGeneration = "modern";
  proxy._startBackendMonitor = () => {};
  proxy._ensureBackend = async () => false;

  const degraded = await proxy._toolsList(4, {});
  assert.equal(degraded.result.ttlMs, 0, "degraded catalog expires immediately");
  assert.equal(degraded.result.cacheScope, "private", "degraded catalog is private");

  const cached = await proxy._toolsList(5, {});
  assert.equal(cached.result.ttlMs, 0, "cached catalog expires immediately");
  assert.equal(cached.result.cacheScope, "private", "cached catalog is private");
});

// ── background monitor recovers the toolset after an outage ──────────

itAsync("catalog refinement notifies only when the effective contract changes", async () => {
  const proxy = newProxy();
  const initial = structuredClone(proxy.catalog.canonical);
  const notes = [];
  proxy._notify = (method) => notes.push(method);
  proxy._rpc = async () => ({ tools: initial });
  await proxy._syncTools();
  assert.equal(notes.length, 0);
  initial[0].description += " Refined description.";
  await proxy._syncTools();
  assert.deepEqual(notes, ["notifications/tools/list_changed"]);
  await proxy._syncTools();
  assert.equal(notes.length, 1);
  initial.reverse();
  initial[0].inputSchema.properties = Object.fromEntries(Object.entries(initial[0].inputSchema.properties).reverse());
  await proxy._syncTools();
  assert.equal(notes.length, 1, "object and tool ordering do not change the effective catalog");
});

itAsync("current Native creation options refine the complete catalog once", async () => {
  const proxy = newProxy();
  const tools = structuredClone(proxy.catalog.canonical);
  const create = tools.find((tool) => tool.name === "akb_create_vault");
  delete create.inputSchema.properties.template;
  delete create.inputSchema.properties.external_git;
  create.description = "This deployment supports empty vault creation.";
  const notes = [];
  proxy._notify = (method) => notes.push(method);
  proxy._rpc = async () => ({ tools });
  await proxy._syncTools();
  const refined = (await proxy._toolsList(1, {})).result.tools;
  assert.deepEqual(refined.find((tool) => tool.name === create.name), create);
  assert.equal(refined.length, proxy.catalog.canonical.length + 7);
  assert.deepEqual(notes, ["notifications/tools/list_changed"]);
  await proxy._syncTools();
  assert.equal(notes.length, 1);
});

itAsync("catalog refinement refuses empty and incomplete backend grammars", async () => {
  for (const tools of [[], structuredClone(newProxy().catalog.canonical).slice(1)]) {
    const proxy = newProxy();
    const before = structuredClone(proxy.catalog.initialCatalog());
    assert.throws(() => proxy.catalog.refine({ tools }), /incompatible/);
    assert.deepEqual(proxy.catalog.initialCatalog(), before);
  }
});

itAsync("catalog accepts current creation refinements and rejects other schema or name changes", async () => {
  for (const removed of [["template"], ["external_git"], ["template", "external_git"]]) {
    const proxy = newProxy();
    const tools = structuredClone(proxy.catalog.canonical);
    const create = tools.find((tool) => tool.name === "akb_create_vault");
    for (const argument of removed) delete create.inputSchema.properties[argument];
    assert.equal(proxy.catalog.refine({ tools }), true);
    assert.deepEqual(proxy.catalog.initialCatalog().tools.find((tool) => tool.name === create.name), create);
    assert.equal(proxy.catalog.refine({ tools }), false);
  }
  for (const mutate of [
    (tools) => { tools[tools.length - 1] = tools[0]; },
    (tools) => { tools[0].name = "akb_unexpected"; },
    (tools) => { tools.push(tools[0]); },
    (tools) => { tools[0].outputSchema = { type: "object" }; },
    (tools) => { tools[0].inputSchema.additionalProperties = false; },
    (tools) => { delete tools.find((tool) => tool.name === "akb_create_vault").inputSchema.properties.description; },
  ]) {
    const proxy = newProxy();
    const tools = structuredClone(proxy.catalog.canonical);
    mutate(tools);
    const before = structuredClone(proxy.catalog.initialCatalog());
    assert.throws(() => proxy.catalog.refine({ tools }), /incompatible/);
    assert.deepEqual(proxy.catalog.initialCatalog(), before);
  }
});

itAsync("monitor stays silent on recovery when the list was never degraded", async () => {
  const proxy = newProxy();
  proxy._ensureBackend = async () => {
    proxy._backendReady = true;
    return true;
  };
  proxy._syncTools = async () => ({ tools: proxy.catalog.canonical });
  const notes = [];
  proxy._notify = (method) => notes.push(method);

  proxy._startBackendMonitor();
  await tick();

  assert.equal(notes.length, 0, "no spurious list_changed when nothing was degraded");
  proxy._closed = true;
});

itAsync("vault-guide acknowledgement is attached only to the exact backend retry", async () => {
  const proxy = newProxy();
  const forwarded = [];
  const challenge = {
    error: "Apply the vault instructions",
    code: "vault_skill_required",
    retryable: true,
    vault_skill: {
      vault: "v1",
      version: "guide-v1",
      body: "# Guide",
      ack_token: "opaque-ack",
    },
  };
  proxy._forward = async (msg) => {
    forwarded.push(msg);
    const body = forwarded.length === 1 ? challenge : { updated: true };
    return {
      jsonrpc: "2.0",
      id: msg.id,
      result: { content: [{ type: "text", text: JSON.stringify(body) }] },
    };
  };
  const params = {
    name: "akb_update",
    arguments: { content: "new", uri: "akb://v1/doc/a.md" },
  };

  await proxy._handle({ jsonrpc: "2.0", id: 1, method: "tools/call", params });
  await proxy._handle({
    jsonrpc: "2.0",
    id: 2,
    method: "tools/call",
    // Reordered keys still identify the unchanged logical operation.
    params: {
      name: "akb_update",
      arguments: { uri: "akb://v1/doc/a.md", content: "new" },
    },
  });

  assert.equal(forwarded[0].params.arguments._vault_skill_ack, undefined);
  assert.equal(forwarded[1].params.arguments._vault_skill_ack, "opaque-ack");
});

itAsync("vault-guide acknowledgement never authorizes an unrelated queued write", async () => {
  const proxy = newProxy();
  const forwarded = [];
  proxy._forward = async (msg) => {
    forwarded.push(msg);
    return {
      jsonrpc: "2.0",
      id: msg.id,
      result: {
        content: [{
          type: "text",
          text: JSON.stringify({
            code: "vault_skill_required",
            vault_skill: { ack_token: "opaque-ack", version: "v1" },
          }),
        }],
      },
    };
  };

  await proxy._handle({
    jsonrpc: "2.0", id: 1, method: "tools/call",
    params: { name: "akb_put", arguments: { vault: "v1", title: "A", content: "a" } },
  });
  await proxy._handle({
    jsonrpc: "2.0", id: 2, method: "tools/call",
    params: { name: "akb_put", arguments: { vault: "v1", title: "B", content: "b" } },
  });

  assert.equal(forwarded[1].params.arguments._vault_skill_ack, undefined);
});

// ── _forward never kills the process; it recovers or surfaces an error ─

itAsync("_forward submits once after a connection error and keeps the process alive", async () => {
  const proxy = newProxy();
  proxy._startBackendMonitor = () => {}; // observe restart without spinning a loop
  let restarts = 0;
  const origNoop = proxy._startBackendMonitor;
  proxy._startBackendMonitor = () => {
    restarts++;
    origNoop();
  };
  proxy._backendReady = true;
  let calls = 0;
  proxy._rpc = async () => {
    calls++;
    throw new Error("ECONNRESET");
  };

  await assert.rejects(() => proxy._forward({ method: "tools/call", id: 5, params: {} }));
  assert.equal(proxy._backendReady, false, "marks backend not-ready");
  assert.ok(restarts >= 1, "kicks the reconnect monitor");
  assert.equal(calls, 1, "caller operation is never replayed");
});

itAsync("a new explicit call can succeed after a lost first response", async () => {
  const proxy = newProxy();
  proxy._startBackendMonitor = () => {};
  let calls = 0;
  proxy._rpc = async () => { calls++; if (calls === 1) throw new Error("socket hang up"); return { ok: true }; };
  await assert.rejects(() => proxy._forward({ method: "tools/call", id: 7, params: {} }));
  assert.equal(calls, 1);
  const response = await proxy._forward({ method: "tools/call", id: 8, params: {} });
  assert.equal(response.result.ok, true);
  assert.equal(calls, 2);
});

// ── Summary ──────────────────────────────────────────────────────────

await Promise.all(pending);
console.log("");
console.log(`  Passed: ${pass}   Failed: ${fail}`);
process.exit(fail > 0 ? 1 : 0);
