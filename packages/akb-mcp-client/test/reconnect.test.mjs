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
  return new AKBProxy({ url: "http://akb.test/mcp", pat: "test-pat" });
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

itAsync("initialize rejects an unsupported protocol version instead of echoing it", async () => {
  const proxy = newProxy();
  proxy._startBackendMonitor = () => {};
  const res = await proxy._initialize(1, {
    protocolVersion: "2099-01-01",
    capabilities: {},
  });

  assert.equal(res.error.code, -32602);
  assert.deepEqual(res.error.data.supported, ["2025-06-18"]);
  assert.equal(res.result, undefined);
  assert.equal(proxy._initialized, false);
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
    assert.equal(options.protocolMode, "modern");
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

itAsync("tools/list serves file tools only when backend is down and nothing cached", async () => {
  const proxy = newProxy();
  proxy._startBackendMonitor = () => {};
  proxy._ensureBackend = async () => false;

  const res = await proxy._toolsList(2, {});
  const names = res.result.tools.map((t) => t.name);

  assert.deepEqual(names.sort(), [...fileToolNames].sort(), "only file tools served");
  assert.equal(proxy._servedDegraded, true, "flags the degraded list for later re-list");
});

itAsync("tools/list serves the full decorated list from cache", async () => {
  const proxy = newProxy();
  proxy._startBackendMonitor = () => {};
  proxy._cachedTools = {
    tools: [
      { name: "akb_put", inputSchema: { type: "object", properties: {} } },
      { name: "akb_search", inputSchema: { type: "object", properties: {} } },
    ],
  };

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
  assert.equal(proxy._servedDegraded, false, "cached full list is not degraded");
  // The cache must not be mutated by decoration.
  assert.ok(
    !proxy._cachedTools.tools[0].inputSchema.properties.file,
    "cached tool schema left untouched",
  );
});

// ── background monitor recovers the toolset after an outage ──────────

itAsync("monitor emits tools/list_changed after backend recovers from a degraded list", async () => {
  const proxy = newProxy();
  proxy._servedDegraded = true; // client was previously handed file-tools-only
  proxy._ensureBackend = async () => {
    proxy._backendReady = true;
    return true;
  };
  proxy._syncTools = async () => ({ tools: [] });
  const notes = [];
  proxy._notify = (method) => notes.push(method);

  proxy._startBackendMonitor();
  await tick();

  assert.ok(notes.includes("notifications/tools/list_changed"), "list_changed pushed on recovery");
  assert.equal(proxy._servedDegraded, false, "degraded flag cleared");
  proxy._closed = true;
});

itAsync("monitor stays silent on recovery when the list was never degraded", async () => {
  const proxy = newProxy();
  proxy._servedDegraded = false;
  proxy._ensureBackend = async () => {
    proxy._backendReady = true;
    return true;
  };
  proxy._syncTools = async () => ({ tools: [] });
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

itAsync("_forward retries a connection error then surfaces it (process survives)", async () => {
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
  assert.ok(calls >= 3, "attempted the initial call plus retries");
});

itAsync("_forward recovers on a later attempt once the backend returns", async () => {
  const proxy = newProxy();
  proxy._startBackendMonitor = () => {};
  proxy._backendReady = true;
  let calls = 0;
  proxy._rpc = async () => {
    calls++;
    if (calls === 1) throw new Error("socket hang up");
    return { ok: true };
  };
  proxy._ensureBackend = async () => {
    proxy._backendReady = true;
    return true;
  };

  const res = await proxy._forward({ method: "tools/call", id: 7, params: {} });
  assert.equal(res.result.ok, true, "second attempt succeeds");
  assert.equal(calls, 2);
});

// ── Summary ──────────────────────────────────────────────────────────

await Promise.all(pending);
console.log("");
console.log(`  Passed: ${pass}   Failed: ${fail}`);
process.exit(fail > 0 ? 1 : 0);
