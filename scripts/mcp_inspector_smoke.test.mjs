import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { mkdtemp, readFile, rm, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawn } from "node:child_process";
import test from "node:test";

import {
  createConfigFifo,
  FAILURE_CLASSES,
  MODERN_PROTOCOL_VERSION,
  buildInspectorConfig,
  classifyInspectorFailure,
  configDigest,
  inspectorArguments,
  inspectInstallation,
  nodeVersionMeetsFloor,
  parseArguments,
  redactValue,
  runInspectorInvocation,
  runSmoke,
  stableStringify,
  validateDescriptor,
  validateRuntimeDiscovery,
  writeConfigToFifo,
} from "./mcp_inspector_smoke.mjs";

const marker = "synthetic-secret-marker";

function descriptor() {
  return {
    schema_version: 2,
    status: "ready",
    scenario: "empty",
    services: {
      app: {
        origin: "http://127.0.0.1:8000",
        health: { method: "GET", url: "http://127.0.0.1:8000/readyz" },
        discovery: { method: "GET", url: "http://127.0.0.1:8000/openapi.json" },
      },
      fixture: {
        origin: "http://127.0.0.1:8889",
        health: { method: "GET", url: "http://127.0.0.1:8889/health" },
        reset: {
          method: "POST",
          url: "http://127.0.0.1:8889/reset",
          body: { scenario: "empty" },
        },
        discovery: { method: "GET", url: "http://127.0.0.1:8889/discover" },
      },
      stdio: {
        transport: "stdio",
        package: "akb-mcp",
        executable: "akb-mcp",
        consumer_root: "/tmp/akb-consumer",
        environment: {
          AKB_MCP_URL: "http://127.0.0.1:8000/mcp/",
          AKB_PAT: "AKB_E2E_PAT",
        },
      },
    },
    credentials: {
      username_env: "AKB_E2E_USERNAME",
      password_env: "AKB_E2E_PASSWORD", // pragma: allowlist secret
      pat_env: "AKB_E2E_PAT",
      login_path: "/api/v1/auth/login",
    },
    profile: "transport-proxy",
    evidence: { source_revision: "a".repeat(40), proxy_artifact_version: "2.3.0" },
  };
}

function discovery() {
  return {
    status: "ready",
    scenario: "empty",
    access: {
      login: {
        service: "app",
        method: "POST",
        path: "/api/v1/auth/login",
        fields: ["username", "password"],
      },
    },
    runtime: {
      source_revision: "a".repeat(40),
      profile: "transport-proxy",
      credential_env: {
        username: "AKB_E2E_USERNAME",
        password: "AKB_E2E_PASSWORD", // pragma: allowlist secret
        pat: "AKB_E2E_PAT",
      },
      pat: {
        mint: {
          service: "app",
          method: "POST",
          path: "/api/v1/auth/tokens",
          body_fields: ["name"],
          auth: "login_session",
        },
      },
      fixture: {
        generation: "fixture-generation",
        reset: {
          method: "POST",
          url: "http://127.0.0.1:8889/reset",
          body: { scenario: "empty" },
        },
      },
      consumer_smoke: {
        protocol_era: "modern",
        protocol_version: MODERN_PROTOCOL_VERSION,
        http: { service: "app", method: "POST", path: "/mcp/" },
        required_tools: ["akb_list_vaults", "akb_put", "akb_delete"],
        shared_tools: ["akb_list_vaults", "akb_put", "akb_delete"],
        representative: {
          tool: "akb_list_vaults",
          read_only: true,
          arguments: {},
          observable: {
            content_type: "json",
            result_type: "object",
            required_keys: ["vaults", "total", "returned"],
            items_key: "vaults",
            count_rule: "total>=returned==items.length",
            is_error: false,
          },
        },
        proxy_local: {
          tools: ["akb_put_file"],
          input_properties: { akb_put: ["file"] },
        },
      },
    },
  };
}

test("Node floor and explicit target/intent parsing are fail-closed", () => {
  assert.equal(nodeVersionMeetsFloor("22.19.0"), true);
  assert.equal(nodeVersionMeetsFloor("22.18.9"), false);
  assert.deepEqual(parseArguments(["--intent", "smoke", "--target", "both", "--descriptor", "descriptor.json"]), {
    intent: "smoke",
    target: "both",
    descriptor: "descriptor.json",
    help: false,
  });
  assert.throws(() => parseArguments(["--intent", "smoke", "--descriptor", "descriptor.json"]), /target/);
  assert.throws(() => parseArguments(["--intent", "interactive", "--target", "both", "--descriptor", "descriptor.json"]), /one explicit target/);
});

test("installed Inspector preflight resolves the exact public launcher", async () => {
  const info = await inspectInstallation();
  assert.equal(info.package, "@modelcontextprotocol/inspector");
  assert.equal(info.version, "2.4.0");
  assert.equal(info.bin, "clients/launcher/build/index.js");
});

test("schema-v2 descriptor is the only target source and requires modern discovery fields", () => {
  const validated = validateDescriptor(descriptor(), "both");
  assert.equal(validated.scenario, "empty");
  assert.equal(validated.services.stdio.package, "akb-mcp");
  const contract = validateRuntimeDiscovery(validated, discovery(), "both");
  assert.equal(contract.http.url, "http://127.0.0.1:8000/mcp/");
  assert.equal(contract.representative.tool, "akb_list_vaults");
  assert.equal(contract.representative.readOnly, true);
  assert.equal(contract.fixtureGeneration, "fixture-generation");
});

test("Inspector public executable invocation keeps exact JSON args out of coercion", () => {
  const info = { entry: "/private/inspector/clients/launcher/build/index.js" };
  const representative = { tool: "akb_list_vaults", arguments: { nested: { value: "012" }, enabled: false } };
  const args = inspectorArguments(info, "tools/call", representative, {
    configPath: "/private/run-scoped-inspector/inspector-config.fifo",
  });
  assert.ok(args.includes("--cli"));
  assert.ok(args.includes("--stored-auth-only"));
  assert.ok(args.includes("--config"));
  assert.ok(args.includes("/private/run-scoped-inspector/inspector-config.fifo"));
  assert.equal(args.includes("/dev/stdin"), false);
  assert.equal(args[args.indexOf("--tool-args-json") + 1], JSON.stringify(representative.arguments));
  assert.ok(!args.includes(marker));
});

test("credential values are redacted from evidence and config digest", () => {
  const value = { headers: { Authorization: `Bearer ${marker}` }, nested: marker };
  const redacted = redactValue(value, [marker]);
  assert.equal(JSON.stringify(redacted).includes(marker), false);
  const digest = configDigest(value, [marker]);
  assert.match(digest, /^sha256:[0-9a-f]{64}$/);
  assert.equal(stableStringify({ b: 1, a: 2 }), '{"a":2,"b":1}');
});

test("Inspector exit classes remain stable", () => {
  assert.equal(classifyInspectorFailure(3, "initialize"), FAILURE_CLASSES.authentication);
  assert.equal(classifyInspectorFailure(4, "tools/list"), FAILURE_CLASSES.unreachable);
  assert.equal(classifyInspectorFailure(5, "tools/call"), FAILURE_CLASSES.tool);
  assert.equal(classifyInspectorFailure(5, "tools/list"), FAILURE_CLASSES.tool);
  assert.equal(classifyInspectorFailure(6, "tools/list"), FAILURE_CLASSES.schema);
  assert.equal(classifyInspectorFailure(1, "initialize"), FAILURE_CLASSES.usage);
});

test("run-scoped config uses a private FIFO and secrets never enter Inspector argv", async () => {
  let captured = null;
  const spawnProcess = (_command, args, options) => {
    captured = { args, options };
    const child = new EventEmitter();
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    const configPath = args[args.indexOf("--config") + 1];
    void readFile(configPath, "utf8").then((value) => {
      captured.config = JSON.parse(value);
      child.stdout.emit("data", `${JSON.stringify({ result: { protocolVersion: MODERN_PROTOCOL_VERSION, serverInfo: { name: "akb", version: "1" } } })}\n`);
      child.emit("close", 0, null);
    }).catch((error) => {
      child.stderr.emit("data", `${error.code || "config_read_error"}\n`);
      child.emit("close", 1, null);
    });
    return child;
  };
  const d = descriptor();
  const config = buildInspectorConfig("http", {
    protocolEra: "modern",
    protocolVersion: MODERN_PROTOCOL_VERSION,
    http: { url: "http://127.0.0.1:8000/mcp/" },
    representative: { tool: "akb_list_vaults", arguments: {} },
  }, marker, null);
  const runtimeRoot = await mkdtemp(join(tmpdir(), "akb-inspector-handoff-test-"));
  try {
    const result = await runInspectorInvocation({
      info: { entry: "/private/inspector.js" },
      descriptor: validateDescriptor(d, "http"),
      config,
      method: "initialize",
      representative: { tool: "akb_list_vaults", arguments: {} },
      runtimeRoot,
      spawnProcess,
    });
    assert.equal(result.code, 0);
    assert.equal(captured.args.includes(marker), false);
    assert.equal(captured.args.includes("/dev/stdin"), false);
    assert.equal(captured.args.includes(join(runtimeRoot, "inspector-config.fifo")), true);
    assert.equal(captured.config.mcpServers.akb.headers.Authorization, `Bearer ${marker}`);
    assert.equal(captured.options.env.AKB_E2E_PASSWORD, undefined);
    assert.equal(captured.options.stdio[0], "ignore");
    assert.equal(captured.options.env.MCP_INSPECTOR_SECRET_STORE, "memory");
    await assert.rejects(stat(join(runtimeRoot, "inspector-config.fifo")), { code: "ENOENT" });
  } finally {
    await rm(runtimeRoot, { recursive: true, force: true });
  }
});

test("a child can reopen the private FIFO and the handoff leaves no config state", async () => {
  const runtimeRoot = await mkdtemp(join(tmpdir(), "akb-inspector-fifo-regression-"));
  const fifoPath = join(runtimeRoot, "config.fifo");
  const config = { marker };
  let child = null;
  let stdout = "";
  let stderr = "";
  try {
    await createConfigFifo(fifoPath);
    child = spawn(process.execPath, [
      "-e",
      "const { readFile } = require('node:fs/promises'); readFile(process.argv[1], 'utf8').then(value => process.stdout.write(value)).catch(error => { process.stderr.write(error.code || 'read_error'); process.exitCode = 1; });",
      fifoPath,
    ], { stdio: ["ignore", "pipe", "pipe"] });
    child.stdout.on("data", (chunk) => { stdout += chunk.toString(); });
    child.stderr.on("data", (chunk) => { stderr += chunk.toString(); });
    const resultPromise = new Promise((resolveResult) => child.once("close", (code, signal) => resolveResult({ code, signal })));
    await writeConfigToFifo(fifoPath, config, child);
    const result = await resultPromise;
    assert.equal(result.code, 0, stderr);
    assert.deepEqual(JSON.parse(stdout), config);
    assert.equal((await stat(fifoPath)).isFIFO(), true);
  } finally {
    if (child && child.exitCode === null && child.signalCode === null) child.kill();
    await rm(fifoPath, { force: true });
    await rm(runtimeRoot, { recursive: true, force: true });
  }
  assert.equal(stdout.includes(marker), true);
  assert.equal(stderr.includes(marker), false);
  await assert.rejects(stat(fifoPath), { code: "ENOENT" });
});

test("interactive Inspector also receives config through the private FIFO", async () => {
  let captured = null;
  const d = descriptor();
  const config = buildInspectorConfig("http", {
    protocolEra: "modern",
    protocolVersion: MODERN_PROTOCOL_VERSION,
    http: { url: "http://127.0.0.1:8000/mcp/" },
    representative: { tool: "akb_list_vaults", arguments: {} },
  }, marker, null);
  const runtimeRoot = await mkdtemp(join(tmpdir(), "akb-inspector-interactive-test-"));
  const spawnProcess = (_command, args, options) => {
    captured = { args, options };
    const child = new EventEmitter();
    const configPath = args[args.indexOf("--config") + 1];
    void readFile(configPath, "utf8").then((value) => {
      captured.config = JSON.parse(value);
      child.emit("close", 0, null);
    });
    return child;
  };
  try {
    const result = await runInspectorInvocation({
      info: { entry: "/private/inspector.js" },
      descriptor: validateDescriptor(d, "http"),
      config,
      method: "interactive",
      representative: { tool: "akb_list_vaults", arguments: {} },
      runtimeRoot,
      spawnProcess,
      interactive: true,
    });
    assert.equal(result.code, 0);
    assert.equal(captured.args.includes("--web"), true);
    assert.equal(captured.args.includes(marker), false);
    assert.equal(captured.args.includes(join(runtimeRoot, "inspector-config.fifo")), true);
    assert.equal(captured.config.mcpServers.akb.headers.Authorization, `Bearer ${marker}`);
    assert.deepEqual(captured.options.stdio, ["ignore", "inherit", "inherit"]);
    await assert.rejects(stat(join(runtimeRoot, "inspector-config.fifo")), { code: "ENOENT" });
  } finally {
    await rm(runtimeRoot, { recursive: true, force: true });
  }
});

test("Linux reproduces the Inspector /dev/stdin reopen failure that the FIFO avoids", {
  skip: process.platform !== "linux",
}, async () => {
  const child = spawn(process.execPath, [
    "-e",
    "const { readFile } = require('node:fs/promises'); readFile('/dev/stdin', 'utf8').then(() => process.exitCode = 2).catch(error => { process.stdout.write(error.code || 'read_error'); });",
  ], { stdio: ["pipe", "pipe", "pipe"] });
  let stdout = "";
  child.stdout.on("data", (chunk) => { stdout += chunk.toString(); });
  child.stdin.end('{"config":"run-scoped"}\n');
  const result = await new Promise((resolveResult) => child.once("close", (code, signal) => resolveResult({ code, signal })));
  assert.equal(result.code, 0);
  assert.equal(stdout, "ENXIO");
});

test("smoke uses the declared reset, credential, and operation sequence", async () => {
  const calls = [];
  const responses = {
    "http://127.0.0.1:8000/readyz": { status: "ready" },
    "http://127.0.0.1:8889/health": { status: "ready" },
    "http://127.0.0.1:8889/discover": discovery(),
    "http://127.0.0.1:8889/reset": { status: "ready", scenario: "empty", generation: "fixture-generation" },
    "http://127.0.0.1:8000/api/v1/auth/login": { token: "session-token" },
    "http://127.0.0.1:8000/api/v1/auth/tokens": { token: "akb_minted_pat" },
  };
  const fetchImpl = async (url, options = {}) => {
    calls.push({ url, options });
    const body = responses[url];
    assert.ok(body, `unexpected fixture request: ${url}`);
    return { ok: true, status: 200, text: async () => JSON.stringify(body) };
  };
  const tools = [
    { name: "akb_list_vaults", inputSchema: { type: "object", properties: {} } },
    { name: "akb_put", inputSchema: { type: "object", properties: { content: { type: "string" } } } },
    { name: "akb_delete", inputSchema: { type: "object", properties: { uri: { type: "string" } } } },
  ];
  const spawnArgs = [];
  const spawnProcess = (_command, args, options) => {
    spawnArgs.push({ args, options });
    const child = new EventEmitter();
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    const configPath = args[args.indexOf("--config") + 1];
    void readFile(configPath, "utf8").then(() => {
      const method = args[args.indexOf("--method") + 1];
      const result = method === "initialize"
        ? { serverInfo: { name: "akb-mcp", version: "2.3.0" }, protocolVersion: MODERN_PROTOCOL_VERSION, capabilities: { tools: {} } }
        : method === "tools/list"
          ? { resultType: "complete", ttlMs: 0, cacheScope: "private", tools }
          : { resultType: "complete", content: [{ type: "text", text: JSON.stringify({ vaults: [{ name: "akb_minted_pat" }], total: 1, returned: 1 }) }], isError: false };
      child.stderr.emit("data", "diagnostic: akb_minted_pat\n");
      child.stdout.emit("data", `${JSON.stringify({ result })}\n`);
      child.emit("close", 0, null);
    }).catch((error) => {
      child.stderr.emit("data", `${error.code || "config_read_error"}\n`);
      child.emit("close", 1, null);
    });
    return child;
  };
  const oldUsername = process.env.AKB_E2E_USERNAME;
  const oldPassword = process.env.AKB_E2E_PASSWORD;
  const oldPat = process.env.AKB_E2E_PAT;
  process.env.AKB_E2E_USERNAME = "fixture-user";
  process.env.AKB_E2E_PASSWORD = "fixture-password"; // pragma: allowlist secret
  delete process.env.AKB_E2E_PAT;
  try {
    const validated = validateDescriptor(descriptor(), "http");
    const output = await runSmoke({
      info: { entry: "/private/inspector.js", package: "@modelcontextprotocol/inspector", version: "2.4.0", nodeVersion: "22.19.0" },
      descriptor: descriptor(),
      target: "http",
      fetchImpl,
      spawnProcess,
    });
    assert.equal(validated.scenario, "empty");
    assert.equal(output.status, "passed");
    assert.equal(output.inspector.config_handoff, "private_fifo");
    assert.deepEqual(output.transports.http.operation_order, ["initialize", "tools/list", "tools/call"]);
    assert.equal(output.transports.http.operations[2].observable_match, true);
    assert.equal(output.transports.http.config_digest.startsWith("sha256:"), true);
    assert.equal(JSON.stringify(output).includes("akb_minted_pat"), false);
    assert.equal(spawnArgs.length, 3);
    assert.equal(spawnArgs.every(({ args }) => !args.includes("akb_minted_pat")), true);
    assert.equal(calls.some(({ url, options }) => url.endsWith("/reset") && options.method === "POST"), true);
  } finally {
    if (oldUsername === undefined) delete process.env.AKB_E2E_USERNAME; else process.env.AKB_E2E_USERNAME = oldUsername;
    if (oldPassword === undefined) delete process.env.AKB_E2E_PASSWORD; else process.env.AKB_E2E_PASSWORD = oldPassword;
    if (oldPat === undefined) delete process.env.AKB_E2E_PAT; else process.env.AKB_E2E_PAT = oldPat;
  }
});
