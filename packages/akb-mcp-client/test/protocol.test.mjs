// Source-neutral protocol matrix smoke test through the installed stdio
// process. The fake backend only records the wire contract; it does not call
// proxy internals.

import assert from "node:assert/strict";
import { createServer } from "node:http";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { AKBProxy } from "../lib/proxy.mjs";

const packageRoot = join(dirname(fileURLToPath(import.meta.url)), "..");
const proxyEntrypoint = join(packageRoot, "bin", "akb-mcp.mjs");

const MODERN_META = {
  "io.modelcontextprotocol/protocolVersion": "2026-07-28",
  "io.modelcontextprotocol/clientCapabilities": {},
  "io.modelcontextprotocol/clientInfo": { name: "modern-test", version: "1" },
};

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

async function fakeBackend() {
  const requests = [];
  const server = createServer(async (req, res) => {
    let raw = "";
    for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw);
    requests.push({ headers: req.headers, body });

    let result;
    if (body.method === "server/discover") {
      result = {
        supportedVersions: ["2026-07-28"],
        capabilities: { tools: { listChanged: true } },
      };
    } else if (body.method === "tools/list") {
      result = {
        tools: [{
          name: "akb_search",
          description: "search",
          inputSchema: { type: "object", properties: {} },
        }],
      };
    } else {
      result = { content: [{ type: "text", text: "{}" }], isError: false };
    }
    res.writeHead(200, { "content-type": "application/json" });
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

async function testModernProcess() {
  const backend = await fakeBackend();
  try {
    const { responses } = await runProxy(backend.url, [
      { jsonrpc: "2.0", id: 1, method: "server/discover", params: { _meta: MODERN_META } },
      { jsonrpc: "2.0", id: 2, method: "tools/list", params: { _meta: MODERN_META } },
    ]);
    assert.equal(responses[0].result.supportedVersions[0], "2026-07-28");
    assert.equal(responses[0].result.resultType, "complete");
    assert.equal(responses[1].result.resultType, "complete");
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

async function testGenerationMixingIsFailClosed() {
  const legacy = new AKBProxy({
    url: "http://127.0.0.1/mcp/",
    pat: "akb_test",
  });
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

  const modern = new AKBProxy({
    url: "http://127.0.0.1/mcp/",
    pat: "akb_test",
  });
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

await testModernProcess();
await testLegacyProcess();
await testGenerationMixingIsFailClosed();
console.log("  ✓ stdio modern and legacy processes preserve their client surfaces");
