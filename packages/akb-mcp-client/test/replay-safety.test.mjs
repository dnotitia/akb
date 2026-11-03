import assert from "node:assert/strict";
import { createServer } from "node:http";
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { AKBProxy } from "../lib/proxy.mjs";

const modernMeta = {
  "io.modelcontextprotocol/protocolVersion": "2026-07-28",
  "io.modelcontextprotocol/clientCapabilities": {},
  "io.modelcontextprotocol/clientInfo": { name: "replay-test", version: "1" },
};
function call(name, args = {}, generation = "legacy") {
  return {
    jsonrpc: "2.0", id: 42, method: "tools/call",
    params: { name, arguments: args, ...(generation === "modern" ? { _meta: modernMeta } : {}) },
  };
}
function bodyOf(response) {
  assert.equal(response.id, 42);
  assert.equal(response.result.isError, true);
  return JSON.parse(response.result.content[0].text);
}
function reply(res, body, result = { content: [{ type: "text", text: "saved" }] }) {
  res.writeHead(200, { "content-type": "application/json" });
  res.end(JSON.stringify({ jsonrpc: "2.0", id: body.id, result }));
}

// Real TCP/HTTP: the server records the effect BEFORE losing the response.
// Handshake/monitor behavior is independently covered by reconnect/protocol
// tests; here recovery is deterministic so only actual tool dispatches count.
async function backend(t, generation, handler) {
  const requests = [];
  const server = createServer(async (req, res) => {
    let raw = "";
    for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw);
    requests.push(body);
    handler(res, body, requests.length);
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(async () => {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  });
  const proxy = new AKBProxy({ url: `http://127.0.0.1:${server.address().port}/mcp`, pat: "test" });
  const connect = async () => {
    proxy._backendReady = true;
    proxy._backendMode = generation;
    return true;
  };
  await connect();
  proxy._ensureBackend = connect;
  let monitorStarts = 0;
  proxy._startBackendMonitor = () => { monitorStarts += 1; };
  return { proxy, requests, monitorStarts: () => monitorStarts };
}

for (const generation of ["legacy", "modern"]) {
  test(`${generation}: a committed mutation is never replayed after response loss`, async (t) => {
    let effects = 0;
    const fx = await backend(t, generation, (res, body) => {
      if (body.params.name === "akb_get") return reply(res, body, { content: [{ type: "text", text: String(effects) }] });
      effects += 1;
      res.destroy();
    });
    const args = { uri: "akb://v/doc/a.md", content: "private-draft", tags: [],
      expected_commit: "a".repeat(40), expected_content_hash: "b".repeat(64), _vault_skill_ack: "caller-ack" };
    let response;
    let failure;
    try { response = await fx.proxy._handle(call("akb_update", args, generation)); }
    catch (error) { failure = error; }
    assert.equal(effects, 1, "the committed effect must not be repeated");
    assert.ifError(failure);
    assert.equal(bodyOf(response).code, "operation_unknown");
    assert.equal(bodyOf(response).details.operation_outcome, "unknown");
    assert.equal(JSON.stringify(response).includes(args.content), false);
    assert.equal(effects, 1);
    assert.equal(fx.requests.length, 1);
    assert.deepEqual(fx.requests[0].params.arguments, args, "OCC/metadata/ack are unchanged");
    assert.equal(fx.monitorStarts(), 1, "connection recovery is still requested");
    const read = await fx.proxy._handle(call("akb_get", { uri: args.uri }, generation));
    assert.equal(read.result.content[0].text, "1", "a later caller can inspect the committed state");
  });

  test(`${generation}: reviewed reads and grep without replacement still recover`, async (t) => {
    const fx = await backend(t, generation, (res, body, count) => {
      if (count % 2) res.destroy();
      else reply(res, body);
    });
    for (const [name, args] of [
      ["akb_search", { query: "hello" }], ["akb_get", { uri: "akb://v/doc/a.md" }],
      ["akb_browse", {}], ["akb_grep", { pattern: "TODO" }],
      ["akb_grep", { pattern: "TODO", replace: null }],
    ]) {
      const before = fx.requests.length;
      const result = await fx.proxy._handle(call(name, args, generation));
      assert.equal(result.result.content[0].text, "saved");
      assert.equal(fx.requests.length - before, 2);
    }
  });

  test(`${generation}: SQL, replacement, metadata writes and unknown tools fail closed`, async (t) => {
    const fx = await backend(t, generation, (res) => res.destroy());
    fx.proxy._cachedTools = { tools: [{ name: "custom_read", annotations: { readOnlyHint: true } }] };
    for (const [name, args] of [
      ["akb_put", { vault: "v", title: "title", content: "body" }],
      ["akb_update", { uri: "akb://v/doc/a.md", title: "metadata only" }],
      ["akb_delete", { uri: "akb://v/doc/a.md" }],
      ["akb_grep", { pattern: "TODO", replace: "" }],
      ["akb_grep", { pattern: "TODO", replace: "done" }],
      ["akb_grep", { pattern: "TODO", replace: false }],
      ["akb_sql", { sql: "SELECT 1" }],
      ["akb_sql", { sql: "WITH changed AS (UPDATE counters SET value=value+1 RETURNING *) SELECT * FROM changed" }],
      ["akb_sql", { sql: "SELECT nextval('counter_seq')" }],
      ["custom_read", {}],
    ]) {
      const before = fx.requests.length;
      assert.equal(bodyOf(await fx.proxy._handle(call(name, args, generation))).code, "operation_unknown");
      assert.equal(fx.requests.length - before, 1, name);
    }
  });

  test(`${generation}: only failed preflight proves not_started`, async () => {
    const proxy = new AKBProxy({ url: "http://127.0.0.1:1", pat: "test" });
    proxy._ensureBackend = async () => false;
    proxy._startBackendMonitor = () => {};
    proxy._rpc = async () => assert.fail("tool dispatch must not start");
    const error = bodyOf(await proxy._handle(call("akb_put", { content: "body" }, generation)));
    assert.equal(error.code, "backend_unavailable");
    assert.equal(error.details.operation_outcome, "not_started");
  });

  test(`${generation}: partial responses, timeouts, invalid JSON and server errors are unknown`, async (t) => {
    for (const mode of ["partial", "timeout", "invalid-json", "missing-result", "null-result", "invalid-error", "server-error"]) {
      const fx = await backend(t, generation, (res, body) => {
        if (mode === "timeout") return; // the write happened, but no response arrives
        if (mode === "partial") {
          res.writeHead(200, { "content-type": "application/json" });
          res.write('{"jsonrpc":"2.0","result":');
          setImmediate(() => res.destroy());
          return;
        }
        res.writeHead(["server-error", "invalid-error"].includes(mode) ? 503 : 200, { "content-type": "application/json" });
        const payloads = {
          "missing-result": { id: body.id }, "null-result": { id: body.id, result: null },
          "invalid-error": { error: "private-upstream-body" },
        };
        res.end(payloads[mode] ? JSON.stringify(payloads[mode]) : "private-upstream-body");
      });
      const http = fx.proxy._http.bind(fx.proxy);
      fx.proxy._http = (method, path, body, headers, options) => http(method, path, body, headers, { ...options, timeoutMs: 250 });
      const response = await fx.proxy._handle(call("akb_put", { content: "body" }, generation));
      assert.equal(bodyOf(response).details.operation_outcome, "unknown", mode);
      assert.equal(fx.requests.length, 1, mode);
      assert.equal(JSON.stringify(response).includes("private-upstream-body"), false);
    }
  });

  test(`${generation}: known tool and protocol errors retain their meaning`, async (t) => {
    const failure = { content: [{ type: "text", text: JSON.stringify({ code: "conflict", error: "stale read" }) }], isError: true };
    const fx = await backend(t, generation, (res, body) => reply(res, body, failure));
    const result = await fx.proxy._handle(call("akb_update", { content: "body" }, generation));
    assert.equal(bodyOf(result).code, "conflict");
    assert.equal(fx.requests.length, 1);
    for (const status of [200, 403]) {
      const protocol = await backend(t, generation, (res, body) => {
        res.writeHead(status, { "content-type": "application/json" });
        res.end(JSON.stringify({ jsonrpc: "2.0", id: body.id, error: { code: -32602, message: "timeout is not a valid argument", data: { field: "timeout" } } }));
      });
      await assert.rejects(() => protocol.proxy._handle(call("akb_search", {}, generation)), (err) => err.code === -32602 && err.data.field === "timeout");
      assert.equal(protocol.requests.length, 1, "protocol errors must not be retried because their message says timeout");
    }
    const denied = await backend(t, generation, (res) => {
      res.writeHead(403);
      res.end("timeout setting is forbidden");
    });
    await assert.rejects(() => denied.proxy._handle(call("akb_search", {}, generation)), (err) => err.statusCode === 403);
    assert.equal(denied.requests.length, 1);
  });

  test(`${generation}: stdio survives response loss without replaying the committed write`, async (t) => {
    let effects = 0;
    const fx = await backend(t, generation, (res, body) => {
      if (body.method === "server/discover") return reply(res, body, { supportedVersions: ["2026-07-28"] });
      if (body.method === "tools/list") return reply(res, body, { tools: [] });
      if (body.params.name === "akb_get") return reply(res, body, { content: [{ type: "text", text: String(effects) }] });
      effects += 1;
      res.destroy();
    });
    const child = spawn(process.execPath, [fileURLToPath(new URL("../bin/akb-mcp.mjs", import.meta.url))], {
      env: { ...process.env, AKB_MCP_URL: fx.proxy.url.href, AKB_PAT: "test", AKB_MCP_USAGE_LOG: "" },
      stdio: ["pipe", "pipe", "pipe"],
    });
    child.stderr.resume();
    const lines = createInterface({ input: child.stdout });
    t.after(async () => {
      lines.close();
      if (child.exitCode === null && child.signalCode === null) {
        const closed = new Promise((resolve) => child.once("exit", resolve));
        child.kill();
        await closed;
      }
    });
    const pending = new Map();
    lines.on("line", (line) => {
      const response = JSON.parse(line);
      pending.get(response.id)?.(response);
    });
    const rpc = (msg) => new Promise((resolve, reject) => {
      const timer = setTimeout(() => { pending.delete(msg.id); reject(new Error(`stdio response missing for ${msg.id}`)); }, 5000);
      pending.set(msg.id, (response) => { clearTimeout(timer); pending.delete(msg.id); resolve(response); });
      child.stdin.write(JSON.stringify(msg) + "\n");
    });
    await rpc({ jsonrpc: "2.0", id: 1,
      method: generation === "modern" ? "server/discover" : "initialize",
      params: generation === "modern" ? { _meta: modernMeta } : {
        protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "replay-test", version: "1" },
      },
    });
    const result = await rpc(call("akb_put", { vault: "v", title: "title", content: "body" }, generation));
    assert.equal(bodyOf(result).code, "operation_unknown");
    assert.equal(effects, 1);
    const read = await rpc(call("akb_get", { uri: "akb://v/doc/a.md" }, generation));
    assert.equal(read.result.content[0].text, "1");
    assert.equal(fx.requests.filter((req) => req.method === "tools/call" && req.params.name === "akb_put").length, 1);
  });
}

test("a failed read retry cannot erase evidence of an earlier dispatch", async () => {
  const proxy = new AKBProxy({ url: "http://127.0.0.1:1", pat: "test" });
  proxy._backendReady = true;
  proxy._startBackendMonitor = () => {};
  proxy._rpc = async () => { throw new Error("ECONNRESET"); };
  proxy._ensureBackend = async () => false;
  assert.equal(bodyOf(await proxy._handle(call("akb_get"))).details.operation_outcome, "unknown");
});

test("legacy session loss never replays a mutation", async (t) => {
  const fx = await backend(t, "legacy", (res, body) => {
    res.writeHead(404, { "content-type": "application/json" });
    res.end(JSON.stringify({ id: body.id, error: { code: -32000, message: "Session not found" } }));
  });
  assert.equal(bodyOf(await fx.proxy._handle(call("akb_put", { content: "body" }))).code, "operation_unknown");
  assert.equal(fx.requests.length, 1);
  assert.equal(fx.monitorStarts(), 1);
});

test("concurrent calls keep dispatch evidence separate", async () => {
  const proxy = new AKBProxy({ url: "http://127.0.0.1:1", pat: "test" });
  let connections = 0;
  proxy._ensureBackend = async () => ++connections !== 1;
  proxy._startBackendMonitor = () => {};
  proxy._rpc = async () => { throw new Error("ECONNRESET"); };
  const responses = await Promise.all([proxy._forward(call("custom_write")), proxy._forward(call("custom_write"))]);
  assert.deepEqual(responses.map((response) => bodyOf(response).details.operation_outcome), ["not_started", "unknown"]);
});
