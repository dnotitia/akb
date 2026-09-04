import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { mkdir, mkdtemp, readFile, rm, stat, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import {
  INSPECTOR_VERSION,
  PACKAGE_ROOT,
  inspectInstallation,
  parseArguments,
  runSmoke,
} from "../scripts/mcp-inspector-smoke.mjs";

const marker = "akb_synthetic_inspector_marker";

function descriptor(consumerRoot) {
  return {
    schema_version: 2,
    status: "ready",
    scenario: "empty",
    services: {
      app: { origin: "http://127.0.0.1:8000", health: { url: "http://127.0.0.1:8000/readyz" } },
      fixture: {
        origin: "http://127.0.0.1:8889",
        health: { url: "http://127.0.0.1:8889/health" },
        reset: { url: "http://127.0.0.1:8889/reset", body: { scenario: "empty" } },
        discovery: { url: "http://127.0.0.1:8889/discover" },
      },
      stdio: { consumer_root: consumerRoot },
    },
    credentials: {
      username_env: "AKB_TEST_USERNAME",
      password_env: "AKB_TEST_PASSWORD", // pragma: allowlist secret
      pat_env: "AKB_TEST_PAT",
      login_path: "/api/v1/auth/login",
    },
  };
}

function discovery() {
  return {
    status: "ready",
    scenario: "empty",
    access: { login: { path: "/api/v1/auth/login" } },
    runtime: { pat: { mint: { path: "/api/v1/auth/tokens" } } },
  };
}

async function consumerRoot() {
  const root = await mkdtemp(join(tmpdir(), "akb-inspector-consumer-test-"));
  await writeFile(join(root, "package.json"), "{}\n");
  await mkdir(join(root, "node_modules"), { recursive: true });
  await symlink(PACKAGE_ROOT, join(root, "node_modules/akb-mcp"), "dir");
  return root;
}

test("parses the smoke interface and rejects removed mode options", () => {
  assert.deepEqual(parseArguments(["--target", "both", "--descriptor", "-"]), {
    target: "both", descriptor: "-", help: false,
  });
  assert.throws(() => parseArguments(["--intent", "smoke", "--target", "both", "--descriptor", "-"]));
  assert.throws(() => parseArguments(["--config", "config.json"]));
  assert.throws(() => parseArguments(["--intent", "interactive", "--config", "config.json"]));
});

test("preflights the exact installed public Inspector", async () => {
  const info = await inspectInstallation();
  assert.equal(info.version, INSPECTOR_VERSION);
  assert.match(info.entry, /clients\/launcher\/build\/index\.js$/);
});

test("smoke runs both transports, redacts the marker, and removes config state", async () => {
  const root = await consumerRoot();
  const captured = [];
  const calls = [];
  const responses = {
    "http://127.0.0.1:8000/readyz": { status: "ready" },
    "http://127.0.0.1:8889/health": { status: "ready" },
    "http://127.0.0.1:8889/discover": discovery(),
    "http://127.0.0.1:8889/reset": { status: "ready", scenario: "empty" },
    "http://127.0.0.1:8000/api/v1/auth/login": { token: "session" },
    "http://127.0.0.1:8000/api/v1/auth/tokens": { token: marker },
  };
  const fetchImpl = async (url, options = {}) => {
    calls.push({ url, options });
    return { ok: true, status: 200, text: async () => JSON.stringify(responses[url]) };
  };
  const spawnProcess = (_command, args, options) => {
    const child = new EventEmitter();
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    const path = args[args.indexOf("--config") + 1];
    captured.push({ args, options, path });
    void readFile(path, "utf8").then(async (raw) => {
      const config = JSON.parse(raw);
      const configuredSecret = config.mcpServers.akb.headers?.Authorization ?? config.mcpServers.akb.headers?.authorization ?? config.mcpServers.akb.env?.AKB_PAT;
      assert.equal(configuredSecret, config.mcpServers.akb.headers ? `Bearer ${marker}` : marker);
      const configStat = await stat(path);
      const directoryStat = await stat(dirname(path));
      assert.equal(configStat.mode & 0o777, 0o600);
      assert.equal(directoryStat.mode & 0o777, 0o700);
      child.stderr.emit("data", `diagnostic ${marker}\n`);
      const method = args[args.indexOf("--method") + 1];
      const result = method === "initialize"
        ? { protocolVersion: "2026-07-28", serverInfo: { name: "akb", version: "1" } }
        : method === "tools/list"
          ? { tools: [{ name: "akb_list_vaults", inputSchema: { type: "object", properties: {} } }] }
          : { content: [{ type: "text", text: JSON.stringify({ vaults: [], total: 0, returned: 0 }) }], isError: false };
      child.stdout.emit("data", `${JSON.stringify({ result })}\n`);
      child.emit("close", 0);
    });
    return child;
  };
  const oldUser = process.env.AKB_TEST_USERNAME;
  const oldPassword = process.env.AKB_TEST_PASSWORD;
  const oldPat = process.env.AKB_TEST_PAT;
  const oldInspectorEnv = Object.fromEntries(["MCP_CATALOG_PATH", "MCP_CLIENT_CONFIG_PATH", "MCP_INSPECTOR_SECRET_FILE", "MCP_INSPECTOR_SECRET_KEY", "MCP_INSPECTOR_API_TOKEN"].map((name) => [name, process.env[name]]));
  process.env.AKB_TEST_USERNAME = "fixture-user";
  process.env.AKB_TEST_PASSWORD = "fixture-password"; // pragma: allowlist secret
  delete process.env.AKB_TEST_PAT;
  for (const name of Object.keys(oldInspectorEnv)) process.env[name] = marker;
  try {
    const output = await runSmoke({ info: await inspectInstallation(), descriptor: descriptor(root), target: "both", fetchImpl, spawnProcess });
    assert.equal(output.status, "passed", JSON.stringify(output));
    assert.equal(output.transports.http.status, "passed");
    assert.equal(output.transports.stdio.status, "passed");
    assert.equal(output.comparison.status, "passed");
    assert.equal(JSON.stringify(output).includes(marker), false);
    assert.equal(captured.length, 6);
    assert.equal(captured.every(({ args }) => !args.includes(marker) && !args.includes("/dev/stdin")), true);
    assert.equal(captured.every(({ options }) => options.env.AKB_TEST_PASSWORD === undefined), true);
    assert.equal(captured.every(({ options }) => Object.keys(oldInspectorEnv).every((name) => options.env[name] === undefined)), true);
    for (const { path } of captured) await assert.rejects(stat(path), { code: "ENOENT" });
    for (const { options } of captured) await assert.rejects(stat(options.env.MCP_STORAGE_DIR), { code: "ENOENT" });
    assert.equal(calls.some(({ url, options }) => String(url).endsWith("/reset") && options.method === "POST"), true);
  } finally {
    if (oldUser === undefined) delete process.env.AKB_TEST_USERNAME; else process.env.AKB_TEST_USERNAME = oldUser;
    if (oldPassword === undefined) delete process.env.AKB_TEST_PASSWORD; else process.env.AKB_TEST_PASSWORD = oldPassword;
    if (oldPat === undefined) delete process.env.AKB_TEST_PAT; else process.env.AKB_TEST_PAT = oldPat;
    for (const [name, value] of Object.entries(oldInspectorEnv)) {
      if (value === undefined) delete process.env[name]; else process.env[name] = value;
    }
    await rm(root, { recursive: true, force: true });
  }
});

test("strict catalog errors fail while warnings remain in the operation result", async () => {
  const root = await consumerRoot();
  const fetchImpl = async (url) => ({
    ok: true,
    status: 200,
    text: async () => {
      const address = String(url);
      return JSON.stringify(address.endsWith("discover") ? discovery() : address.endsWith("reset") ? { status: "ready", scenario: "empty" } : address.endsWith("tokens") ? { token: "akb_test" } : address.endsWith("login") ? { token: "session" } : { status: "ready" });
    },
  });
  let schemaFindings = [{ severity: "warning", message: "untyped" }];
  const configPaths = [];
  const spawnProcess = (_command, args) => {
    const child = new EventEmitter();
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    const method = args[args.indexOf("--method") + 1];
    const path = args[args.indexOf("--config") + 1];
    configPaths.push(path);
    void readFile(path, "utf8").then(() => {
      const result = method === "initialize"
        ? { protocolVersion: "2026-07-28", serverInfo: { name: "akb", version: "1" } }
        : method === "tools/list"
          ? { tools: [{ name: "akb_list_vaults", inputSchema: { type: "object", properties: {} } }] }
          : { content: [{ type: "text", text: JSON.stringify({ vaults: [], total: 0, returned: 0 }) }], isError: false };
      const envelope = { result };
      if (method === "tools/list") envelope.schemaFindings = schemaFindings;
      child.stdout.emit("data", `${JSON.stringify(envelope)}\n`);
      child.emit("close", 0);
    });
    return child;
  };
  const oldUser = process.env.AKB_TEST_USERNAME;
  const oldPassword = process.env.AKB_TEST_PASSWORD;
  process.env.AKB_TEST_USERNAME = "fixture-user";
  process.env.AKB_TEST_PASSWORD = "fixture-password"; // pragma: allowlist secret
  delete process.env.AKB_TEST_PAT;
  try {
    const info = await inspectInstallation();
    let output = await runSmoke({ info, descriptor: descriptor(root), target: "http", fetchImpl, spawnProcess });
    assert.equal(output.status, "passed");
    assert.equal(output.transports.http.operations[1].schema_warning_count, 1);
    schemaFindings = [{ severity: "error", message: "bad schema" }];
    output = await runSmoke({ info, descriptor: descriptor(root), target: "http", fetchImpl, spawnProcess });
    assert.equal(output.status, "failed");
    assert.equal(output.transports.http.operations[1].schema_error_count, 1);
    for (const path of configPaths) await assert.rejects(stat(path), { code: "ENOENT" });
  } finally {
    if (oldUser === undefined) delete process.env.AKB_TEST_USERNAME; else process.env.AKB_TEST_USERNAME = oldUser;
    if (oldPassword === undefined) delete process.env.AKB_TEST_PASSWORD; else process.env.AKB_TEST_PASSWORD = oldPassword;
    await rm(root, { recursive: true, force: true });
  }
});

test("missing credentials and read-call errors cannot pass", async () => {
  const root = await consumerRoot();
  const fetchImpl = async (url) => ({
    ok: true,
    status: 200,
    text: async () => {
      const address = String(url);
      return JSON.stringify(address.endsWith("discover") ? discovery() : address.endsWith("reset") ? { status: "ready", scenario: "empty" } : address.endsWith("tokens") ? { token: marker } : address.endsWith("login") ? { token: "session" } : { status: "ready" });
    },
  });
  const oldUser = process.env.AKB_TEST_USERNAME;
  const oldPassword = process.env.AKB_TEST_PASSWORD;
  const oldPat = process.env.AKB_TEST_PAT;
  delete process.env.AKB_TEST_USERNAME;
  delete process.env.AKB_TEST_PASSWORD;
  delete process.env.AKB_TEST_PAT;
  try {
    const info = await inspectInstallation();
    let output = await runSmoke({ info, descriptor: descriptor(root), target: "http", fetchImpl });
    assert.equal(output.status, "failed");

    process.env.AKB_TEST_USERNAME = "fixture-user";
    process.env.AKB_TEST_PASSWORD = "fixture-password"; // pragma: allowlist secret
    const spawnProcess = (_command, args) => {
      const child = new EventEmitter();
      child.stdout = new EventEmitter();
      child.stderr = new EventEmitter();
      const method = args[args.indexOf("--method") + 1];
      void readFile(args[args.indexOf("--config") + 1], "utf8").then(() => {
        const result = method === "initialize"
          ? { protocolVersion: "2026-07-28", serverInfo: { name: "akb", version: "1" } }
          : method === "tools/list"
            ? { tools: [{ name: "akb_list_vaults", inputSchema: { type: "object", properties: {} } }] }
            : { content: [{ type: "text", text: JSON.stringify({ vaults: [], total: 0, returned: 0 }) }], isError: true };
        child.stdout.emit("data", `${JSON.stringify({ result })}\n`);
        child.emit("close", 0);
      });
      return child;
    };
    output = await runSmoke({ info, descriptor: descriptor(root), target: "http", fetchImpl, spawnProcess });
    assert.equal(output.status, "failed");
    assert.equal(output.transports.http.operations[2].status, "failed");
  } finally {
    if (oldUser === undefined) delete process.env.AKB_TEST_USERNAME; else process.env.AKB_TEST_USERNAME = oldUser;
    if (oldPassword === undefined) delete process.env.AKB_TEST_PASSWORD; else process.env.AKB_TEST_PASSWORD = oldPassword;
    if (oldPat === undefined) delete process.env.AKB_TEST_PAT; else process.env.AKB_TEST_PAT = oldPat;
    await rm(root, { recursive: true, force: true });
  }
});
