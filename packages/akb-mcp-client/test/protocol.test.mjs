// Source-neutral protocol matrix smoke test through the installed stdio
// process. The fake backend only records the wire contract; it does not call
// proxy internals.

import assert from "node:assert/strict";
import { createServer } from "node:http";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { Client } from "@modelcontextprotocol/client";
import { StdioClientTransport } from "@modelcontextprotocol/client/stdio";
import { AKBProxy } from "../lib/proxy.mjs";
import { parseConfiguration } from "../lib/config.mjs";
import canonical from "../lib/generated-tools.json" with { type: "json" };

const packageRoot = join(dirname(fileURLToPath(import.meta.url)), "..");
const proxyEntrypoint = join(packageRoot, "bin", "akb-mcp.mjs");

const MODERN_META = {
  "io.modelcontextprotocol/protocolVersion": "2026-07-28",
  "io.modelcontextprotocol/clientCapabilities": {},
  "io.modelcontextprotocol/clientInfo": { name: "modern-test", version: "1" },
};
const FILE_TOOL_NAMES = [
  "akb_put_file",
  "akb_put_image",
  "akb_discard_image",
  "akb_get_file",
  "akb_update_file",
  "akb_delete_file",
];
const DEFAULT_BACKEND_TOOLS = structuredClone(canonical);
const RICH_BACKEND_TOOLS = structuredClone(canonical);
RICH_BACKEND_TOOLS[0].description += " Live description.";

function waitForLine(lines, id, timeoutMs = 3000) {
  const existing = lines.find((line) => line?.id === id);
  if (existing) return Promise.resolve(existing);
  return new Promise((resolve, reject) => {
    const deadline = setTimeout(() => {
      const error = new Error(`Timed out waiting for stdio response ${id}`);
      reject(error);
    }, timeoutMs);
    lines.waiters ??= [];
    lines.waiters.push({ id, resolve: (value) => { clearTimeout(deadline); resolve(value); } });
  });
}

async function fakeBackend({ legacyOnly = false, tools = DEFAULT_BACKEND_TOOLS } = {}) {
  const requests = [];
  const server = createServer(async (req, res) => {
    let raw = "";
    for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw);
    requests.push({ headers: req.headers, body });

    if (legacyOnly && body.method === "server/discover") {
      res.writeHead(400, { "content-type": "application/json" });
      res.end(JSON.stringify({
        jsonrpc: "2.0",
        id: body.id,
        error: { code: -32600, message: "Bad Request: Missing session ID" },
      }));
      return;
    }

    let result;
    if (body.method === "initialize") {
      assert.equal(body.params.protocolVersion, "2025-06-18");
      result = {
        protocolVersion: "2025-06-18",
        capabilities: { tools: { listChanged: true } },
        serverInfo: { name: "legacy-backend", version: "1" },
      };
    } else if (body.method === "server/discover") {
      result = {
        supportedVersions: ["2026-07-28"],
        capabilities: { tools: { listChanged: true } },
      };
    } else if (body.method === "tools/list") {
      result = { tools };
    } else {
      result = { content: [{ type: "text", text: "{}" }], isError: false };
    }
    const headers = { "content-type": "application/json" };
    if (legacyOnly) headers["mcp-session-id"] = "legacy-session";
    res.writeHead(200, headers);
    res.end(JSON.stringify({ jsonrpc: "2.0", id: body.id, result }));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  return {
    requests,
    url: `http://127.0.0.1:${address.port}/mcp/`,
    close: () => new Promise((resolve) => server.close(resolve)),
  };
}

async function runProxy(url, messages) {
  const child = spawn(process.execPath, [proxyEntrypoint, "--url", url, "--pat", "akb_test"], {
    cwd: packageRoot,
    stdio: ["pipe", "pipe", "pipe"],
  });
  const lines = [];
  const rl = createInterface({ input: child.stdout });
  rl.on("line", (line) => {
    try {
      const value = JSON.parse(line);
      lines.push(value);
      for (const waiter of lines.waiters || []) {
        if (waiter.id === value.id) waiter.resolve(value);
      }
      lines.waiters = (lines.waiters || []).filter((waiter) => waiter.id !== value.id);
    } catch {
      // A malformed stdout line is surfaced by a missing response below.
    }
  });

  try {
    const responses = [];
    for (const message of messages) {
      child.stdin.write(`${JSON.stringify(message)}\n`);
      responses.push(await waitForLine(lines, message.id));
    }
    return { child, responses };
  } catch (error) {
    child.kill("SIGKILL");
    throw error;
  } finally {
    child.stdin.end();
    await new Promise((resolve) => {
      const timer = setTimeout(() => {
        child.kill("SIGKILL");
        resolve();
      }, 1500);
      child.once("exit", () => {
        clearTimeout(timer);
        resolve();
      });
    });
    rl.close();
  }
}

async function withExternalModernClient(url, fn, { connectTimeoutMs = 50 } = {}) {
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [proxyEntrypoint, "--url", url, "--pat", "akb_test"],
    cwd: packageRoot,
    env: {
      PATH: process.env.PATH || "",
      AKB_MCP_CONNECT_TIMEOUT_MS: String(connectTimeoutMs),
    },
    stderr: "pipe",
  });
  transport.stderr?.resume();
  const client = new Client(
    { name: "akb-235-external-parser", version: "1" },
    { versionNegotiation: { mode: { pin: "2026-07-28" } } },
  );

  try {
    await client.connect(transport, { timeout: 3000 });
    return await fn(client);
  } finally {
    await client.close().catch(() => {});
  }
}

async function testModernDegradedProcessWithExternalParser() {
  await withExternalModernClient("http://127.0.0.1:1/mcp/", async (client) => {
    const result = await client.listTools();

    assert.equal(result.ttlMs, 0, "degraded modern tools/list is immediately stale");
    assert.equal(result.cacheScope, "private", "degraded modern tools/list is private");
    assert.deepEqual(
      result.tools.map((tool) => tool.name),
      [...canonical.map((tool) => tool.name), ...FILE_TOOL_NAMES, "akb_connection"],
      "the complete catalog is available before any backend response",
    );
  });
}

async function testModernReachableProcessWithExternalParser() {
  const backend = await fakeBackend({ tools: RICH_BACKEND_TOOLS });
  try {
    await withExternalModernClient(backend.url, async (client) => {
      const result = await client.listTools();

      assert.equal(result.ttlMs, 0, "reachable modern tools/list is immediately stale");
      assert.equal(result.cacheScope, "private", "reachable modern tools/list is private");
      assert.deepEqual(result.tools.map((tool) => tool.name), [...canonical.map((tool) => tool.name), ...FILE_TOOL_NAMES, "akb_connection"]);
      const deadline = Date.now() + 2000;
      while (!backend.requests.some((request) => request.body.method === "tools/list") && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 10));
      let refined;
      do {
        refined = await client.listTools();
        if (refined.tools[0].description === RICH_BACKEND_TOOLS[0].description) break;
        await new Promise((resolve) => setTimeout(resolve, 10));
      } while (Date.now() < deadline);
      assert.deepEqual(refined.tools[0], RICH_BACKEND_TOOLS[0], JSON.stringify(await client.callTool({ name: "akb_connection", arguments: {} })));
      for (const request of backend.requests) {
        assert.equal(request.headers.authorization, "Bearer akb_test");
      }

      const toolResult = await client.callTool({ name: "akb_search", arguments: {} });
      assert.equal(toolResult.ttlMs, undefined, "cache metadata stays on tools/list only");
      assert.equal(toolResult.cacheScope, undefined, "cache metadata stays on tools/list only");
    }, { connectTimeoutMs: 1000 });
  } finally {
    await backend.close();
  }
}

async function testModernProcess() {
  const backend = await fakeBackend();
  try {
    const { responses } = await runProxy(backend.url, [
      { jsonrpc: "2.0", id: 1, method: "server/discover", params: { _meta: MODERN_META } },
      { jsonrpc: "2.0", id: 2, method: "tools/list", params: { _meta: MODERN_META } },
      { jsonrpc: "2.0", id: 20, method: "tools/call", params: { name: "akb_list_vaults", arguments: {}, _meta: MODERN_META } },
    ]);
    assert.equal(responses[0].result.supportedVersions[0], "2026-07-28");
    assert.equal(responses[0].result.resultType, "complete");
    assert.equal(responses[0].result.ttlMs, undefined);
    assert.equal(responses[0].result.cacheScope, undefined);
    assert.equal(responses[1].result.resultType, "complete");
    assert.equal(responses[1].result.ttlMs, 0);
    assert.equal(responses[1].result.cacheScope, "private");
    assert.ok(responses[1].result.tools.some((tool) => tool.name === "akb_put_file"));
    assert.ok(responses[1].result._meta["io.modelcontextprotocol/serverInfo"]);

    assert.ok(backend.requests.some((request) => request.body.method === "server/discover"));
    assert.ok(backend.requests.some((request) => request.body.method === "tools/list"));
    for (const request of backend.requests) {
      assert.equal(request.headers["mcp-protocol-version"], "2026-07-28");
      assert.equal(request.headers["mcp-method"], request.body.method);
      assert.equal(request.headers["mcp-session-id"], undefined);
      assert.equal(
        request.body.params._meta["io.modelcontextprotocol/protocolVersion"],
        "2026-07-28",
      );
    }
  } finally {
    await backend.close();
  }
}

async function testLegacyProcess() {
  const backend = await fakeBackend();
  try {
    const { responses } = await runProxy(backend.url, [
      {
        jsonrpc: "2.0",
        id: 3,
        method: "initialize",
        params: {
          protocolVersion: "2025-06-18",
          capabilities: { roots: { listChanged: true } },
          clientInfo: { name: "legacy-test", version: "1" },
        },
      },
      { jsonrpc: "2.0", id: 4, method: "tools/list", params: {} },
    ]);
    assert.equal(responses[0].result.protocolVersion, "2025-06-18");
    assert.equal(responses[1].result.resultType, undefined);
    assert.equal(responses[1].result._meta, undefined);
    assert.equal(responses[1].result.ttlMs, undefined);
    assert.equal(responses[1].result.cacheScope, undefined);
    assert.ok(responses[1].result.tools.some((tool) => tool.name === "akb_search"));

    for (const request of backend.requests) {
      assert.equal(request.headers["mcp-protocol-version"], "2026-07-28");
      assert.equal(request.headers["mcp-method"], request.body.method);
      assert.equal(request.headers["mcp-session-id"], undefined);
    }
  } finally {
    await backend.close();
  }
}

async function testLegacyBackendMismatch() {
  const backend = await fakeBackend({ legacyOnly: true });
  try {
    await withExternalModernClient(backend.url, async (client) => {
      const deadline = Date.now() + 2000;
      let status;
      do {
        const result = await client.callTool({ name: "akb_connection", arguments: {} });
        status = JSON.parse(result.content[0].text);
        if (status.contract === "incompatible") break;
        await new Promise((resolve) => setTimeout(resolve, 10));
      } while (Date.now() < deadline);
      assert.equal(status.contract, "incompatible");
      const rejected = await client.callTool({ name: "akb_list_vaults", arguments: {} });
      assert.equal(JSON.parse(rejected.content[0].text).execution, "not_started");
      assert.equal(backend.requests.filter((request) => request.body.method === "initialize").length, 0);
      assert.equal(backend.requests.filter((request) => request.body.method === "tools/call").length, 0);
    });
  } finally { await backend.close(); }
}

async function testGenerationMixingIsFailClosed() {
  const legacy = new AKBProxy(parseConfiguration(["--url", "http://127.0.0.1/mcp/", "--pat", "akb_test"], {}));
  legacy._startBackendMonitor = () => {};
  let forwarded = false;
  legacy._forward = async () => {
    forwarded = true;
    return { jsonrpc: "2.0", id: 2, result: {} };
  };
  await legacy._handle({
    jsonrpc: "2.0",
    id: 1,
    method: "initialize",
    params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "l", version: "1" } },
  });
  const mixed = await legacy._handle({
    jsonrpc: "2.0",
    id: 2,
    method: "tools/call",
    params: { name: "akb_put_file", arguments: { vault: "v", file_path: "/tmp/nope" }, _meta: MODERN_META },
  });
  assert.equal(mixed.error.code, -32600);
  assert.equal(forwarded, false);

  const modern = new AKBProxy(parseConfiguration(["--url", "http://127.0.0.1/mcp/", "--pat", "akb_test"], {}));
  modern._startBackendMonitor = () => {};
  const discover = await modern._handle({
    jsonrpc: "2.0",
    id: 3,
    method: "server/discover",
    params: { _meta: MODERN_META },
  });
  assert.equal(discover.result.supportedVersions[0], "2026-07-28");
  const legacyHandshake = await modern._handle({
    jsonrpc: "2.0",
    id: 4,
    method: "initialize",
    params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "l", version: "1" } },
  });
  assert.equal(legacyHandshake.error.code, -32022);
}

async function testLocalToolRefusalsAreFlaggedAsErrors() {
  // The proxy answers its file tools itself. Their refusals carry the same
  // {"error", ...} body the backend sends, and the same isError flag with it.
  const legacyInit = {
    jsonrpc: "2.0",
    id: 1,
    method: "initialize",
    params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "l", version: "1" } },
  };
  const putFile = (id) => ({
    jsonrpc: "2.0",
    id,
    method: "tools/call",
    params: { name: "akb_put_file", arguments: { vault: "v", file_path: "/nonexistent/akb-proxy-test.bin" } },
  });

  const failing = new AKBProxy(parseConfiguration(["--url", "http://127.0.0.1/mcp/", "--pat", "akb_test"], {}));
  failing._startBackendMonitor = () => {};
  failing._fileToolSkillPreflight = async () => null;
  await failing._handle(legacyInit);
  const failed = await failing._handle(putFile(2));
  assert.equal(failed.result.isError, true);
  assert.match(JSON.parse(failed.result.content[0].text).error, /not found/i);

  const guided = new AKBProxy(parseConfiguration(["--url", "http://127.0.0.1/mcp/", "--pat", "akb_test"], {}));
  guided._startBackendMonitor = () => {};
  guided._fileToolSkillPreflight = async () => ({ ack_token: "challenge", body: "guide" });
  await guided._handle(legacyInit);
  const required = await guided._handle(putFile(3));
  assert.equal(required.result.isError, true);
  assert.equal(JSON.parse(required.result.content[0].text).code, "vault_skill_required");
}

await testModernDegradedProcessWithExternalParser();
await testModernReachableProcessWithExternalParser();
await testModernProcess();
await testLegacyProcess();
await testLegacyBackendMismatch();
await testGenerationMixingIsFailClosed();
await testLocalToolRefusalsAreFlaggedAsErrors();
console.log("  ✓ stdio modern and legacy processes preserve their client surfaces");
