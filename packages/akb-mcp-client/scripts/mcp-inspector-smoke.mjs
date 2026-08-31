#!/usr/bin/env node

import { spawn as nodeSpawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { chmod, mkdtemp, readFile, realpath, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { isDeepStrictEqual, parseArgs } from "node:util";

export const INSPECTOR_VERSION = "2.4.0";
export const MIN_NODE_VERSION = "22.19.0";
export const MODERN_PROTOCOL_VERSION = "2026-07-28";
export const SERVER_NAME = "akb";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
export const PACKAGE_ROOT = resolve(SCRIPT_DIR, "..");
export const REPOSITORY_ROOT = resolve(PACKAGE_ROOT, "../..");
const INSPECTOR_BIN = "clients/launcher/build/index.js";

function error(message) {
  throw new Error(message);
}

function messageOf(value, fallback = "Inspector command failed") {
  return value instanceof Error ? value.message : String(value ?? fallback);
}

function object(value, label) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) error(`${label} must be an object`);
  return value;
}

function string(value, label) {
  if (typeof value !== "string" || value.trim() === "") error(`${label} is required`);
  return value;
}

function nodeVersionMeetsFloor(value, floor = MIN_NODE_VERSION) {
  const actual = String(value).replace(/^v/, "").split(".").map(Number);
  const minimum = floor.split(".").map(Number);
  if (actual.length !== 3 || minimum.length !== 3 || actual.some(Number.isNaN)) return false;
  for (let index = 0; index < 3; index += 1) {
    if (actual[index] !== minimum[index]) return actual[index] > minimum[index];
  }
  return true;
}

function originUrl(value, label) {
  let url;
  try {
    url = new URL(string(value, label));
  } catch {
    error(`${label} is not a URL`);
  }
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) error(`${label} must be an HTTP(S) origin`);
  return url;
}

export function parseArguments(argv) {
  let values;
  try {
    ({ values } = parseArgs({
      args: argv,
      options: {
        intent: { type: "string" },
        target: { type: "string" },
        descriptor: { type: "string" },
        config: { type: "string" },
        help: { type: "boolean", short: "h" },
      },
      strict: true,
      allowPositionals: false,
    }));
  } catch {
    error("invalid command options");
  }
  const result = {
    intent: values.intent ?? null,
    target: values.target ?? null,
    descriptor: values.descriptor ?? null,
    config: values.config ?? null,
    help: values.help === true,
  };
  if (result.help) return result;
  if (result.intent === "smoke") {
    if (!["http", "stdio", "both"].includes(result.target)) error("smoke target must be http, stdio, or both");
    if (!result.descriptor) error("smoke descriptor is required");
    return result;
  }
  if (result.intent === "interactive") {
    if (!result.config) error("interactive Inspector config is required");
    if (result.target || result.descriptor) error("interactive accepts --config only");
    return result;
  }
  error("intent must be smoke or interactive");
}

export async function inspectInstallation({ packageRoot = PACKAGE_ROOT, nodeVersion = process.versions.node } = {}) {
  if (!nodeVersionMeetsFloor(nodeVersion)) error(`Node.js ${MIN_NODE_VERSION} or newer is required`);
  const inspectorRoot = resolve(packageRoot, "node_modules/@modelcontextprotocol/inspector");
  let manifest;
  try {
    manifest = JSON.parse(await readFile(join(inspectorRoot, "package.json"), "utf8"));
  } catch {
    error("exact MCP Inspector is not installed; run npm ci in packages/akb-mcp-client");
  }
  if (manifest.name !== "@modelcontextprotocol/inspector" || manifest.version !== INSPECTOR_VERSION) {
    error("installed MCP Inspector is not exactly version 2.4.0");
  }
  if (manifest.engines?.node !== `>=${MIN_NODE_VERSION}`) error("MCP Inspector Node.js engine contract is unexpected");
  const declaredBin = manifest.bin?.["mcp-inspector"];
  if (String(declaredBin).replace(/^\.\//, "") !== INSPECTOR_BIN) error("MCP Inspector public bin is unexpected");
  const entry = resolve(inspectorRoot, declaredBin);
  if (!(await stat(entry).then((value) => value.isFile()).catch(() => false))) error("MCP Inspector public launcher is missing");
  return { package: manifest.name, version: manifest.version, nodeVersion: String(nodeVersion).replace(/^v/, ""), entry };
}

export async function loadDescriptor(source) {
  const raw = source === "-" ? readFileSync(0, "utf8") : await readFile(source, "utf8");
  return object(JSON.parse(raw), "descriptor");
}

function redactText(value, secrets) {
  let output = String(value ?? "");
  for (const secret of secrets.filter((item) => typeof item === "string" && item.length > 0)) output = output.split(secret).join("[REDACTED]");
  return output.replace(/Bearer\s+[^\s,}]+/gi, "Bearer [REDACTED]");
}

function redact(value, secrets) {
  if (typeof value === "string") return redactText(value, secrets);
  if (Array.isArray(value)) return value.map((item) => redact(item, secrets));
  if (value !== null && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, redact(item, secrets)]));
  return value;
}

async function jsonRequest(url, options = {}, fetchImpl = globalThis.fetch) {
  let response;
  try {
    response = await fetchImpl(url, {
      ...options,
      headers: { ...(options.body === undefined ? {} : { "content-type": "application/json" }), ...(options.headers ?? {}) },
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });
  } catch {
    error(`request failed: ${url}`);
  }
  const text = await response.text();
  if (!response.ok) error(`request returned HTTP ${response.status}`);
  try {
    return JSON.parse(text);
  } catch {
    error("request returned invalid JSON");
  }
}

function sameOriginUrl(value, base, label) {
  let url;
  try {
    url = new URL(value, base);
  } catch {
    error(`${label} is not a URL`);
  }
  if (url.origin !== base.origin || !["http:", "https:"].includes(url.protocol) || url.username || url.password) error(`${label} must stay on its declared origin`);
  return url;
}

function runtimeInputs(descriptor, target) {
  if (!["http", "stdio", "both"].includes(target)) error("smoke target must be http, stdio, or both");
  descriptor = object(descriptor, "descriptor");
  if (descriptor.schema_version !== 2 || descriptor.status !== "ready") error("descriptor must be a ready schema-v2 descriptor");
  const scenario = string(descriptor.scenario, "descriptor scenario");
  const services = object(descriptor.services, "descriptor services");
  const app = object(services.app, "app service");
  const fixture = object(services.fixture, "fixture service");
  const appOrigin = originUrl(app.origin, "app origin");
  const appHealth = sameOriginUrl(string(app.health?.url, "app health"), appOrigin, "app health");
  const fixtureOrigin = originUrl(fixture.origin, "fixture origin");
  const fixtureHealth = sameOriginUrl(string(fixture.health?.url, "fixture health"), fixtureOrigin, "fixture health");
  const resetRequest = object(fixture.reset, "fixture reset");
  const reset = sameOriginUrl(string(resetRequest.url, "fixture reset"), fixtureOrigin, "fixture reset");
  const resetBody = object(resetRequest.body, "fixture reset body");
  if (resetBody.scenario !== scenario) error("fixture reset scenario does not match descriptor");
  const discovery = sameOriginUrl(string(fixture.discovery?.url, "fixture discovery"), fixtureOrigin, "fixture discovery");
  const credentials = object(descriptor.credentials, "descriptor credentials");
  const usernameEnv = string(credentials.username_env, "username environment name");
  const passwordEnv = string(credentials.password_env, "password environment name");
  const patEnv = credentials.pat_env ? string(credentials.pat_env, "PAT environment name") : null;
  const loginPath = string(credentials.login_path, "credential login path");
  if (!loginPath.startsWith("/") || loginPath.startsWith("//") || loginPath.includes("#")) error("credential login path is invalid");
  if (target === "stdio" || target === "both") {
    if (!patEnv) error("stdio smoke requires a PAT environment name");
    if (!services.stdio) error("stdio service is missing");
  }
  return { scenario, appOrigin, mcpUrl: new URL("/mcp/", appOrigin).toString(), appHealth, fixtureHealth, reset, resetBody, discovery, credentials, usernameEnv, passwordEnv, patEnv, stdio: services.stdio ?? null };
}

async function discoverAndReset(inputs, fetchImpl) {
  const ready = async () => {
    const app = await jsonRequest(inputs.appHealth, {}, fetchImpl);
    const fixture = await jsonRequest(inputs.fixtureHealth, {}, fetchImpl);
    if (app.status !== "ready" || fixture.status !== "ready") error("runtime is not ready");
  };
  await ready();
  const discoveryBefore = object(await jsonRequest(inputs.discovery, {}, fetchImpl), "fixture discovery");
  if (discoveryBefore.status !== "ready" || discoveryBefore.scenario !== inputs.scenario) error("fixture discovery is not ready for this scenario");
  const loginPath = string(discoveryBefore.access?.login?.path, "discovery login path");
  if (inputs.credentials.login_path !== loginPath) error("descriptor and discovery login paths disagree");
  const mintPath = string(discoveryBefore.runtime?.pat?.mint?.path, "discovery PAT mint path");
  const resetResult = object(await jsonRequest(inputs.reset, { method: "POST", body: inputs.resetBody ?? { scenario: inputs.scenario } }, fetchImpl), "fixture reset");
  if (resetResult.status !== "ready" || resetResult.scenario !== inputs.scenario) error("fixture reset did not return the selected ready scenario");
  await ready();
  const discovery = object(await jsonRequest(inputs.discovery, {}, fetchImpl), "fixture discovery");
  if (discovery.status !== "ready" || discovery.scenario !== inputs.scenario) error("fixture discovery is not ready after reset");
  return { discovery, loginUrl: sameOriginUrl(loginPath, inputs.appOrigin, "login path"), mintUrl: sameOriginUrl(mintPath, inputs.appOrigin, "PAT mint path") };
}

async function resolveCredential(inputs, coordinates, fetchImpl) {
  const username = process.env[inputs.usernameEnv] ?? "";
  const password = process.env[inputs.passwordEnv] ?? "";
  if (!username || !password) error("declared credential environment values are missing");
  const secrets = [username, password];
  const supplied = inputs.patEnv ? process.env[inputs.patEnv] ?? "" : "";
  if (supplied) return { pat: supplied, secrets: [...secrets, supplied] };
  const loginResponse = object(await jsonRequest(coordinates.loginUrl, { method: "POST", body: { username, password } }, fetchImpl), "credential login");
  const session = loginResponse.token;
  if (typeof session !== "string" || !session) error("credential login returned no session token");
  secrets.push(session);
  const mintResponse = object(await jsonRequest(coordinates.mintUrl, { method: "POST", headers: { Authorization: `Bearer ${session}` }, body: { name: "mcp-inspector" } }, fetchImpl), "credential mint");
  const pat = mintResponse.token;
  if (typeof pat !== "string" || !pat) error("credential mint returned no PAT");
  return { pat, secrets: [...secrets, pat] };
}

async function resolveStdio(inputs) {
  const stdio = object(inputs.stdio, "stdio service");
  const root = await realpath(string(stdio.consumer_root, "stdio consumer root")).catch(() => null);
  if (!root) error("stdio clean consumer root is missing");
  const candidates = [join(root, "node_modules/.bin/akb-mcp"), join(root, "node_modules/akb-mcp/bin/akb-mcp.mjs")];
  let found = null;
  for (const candidate of candidates) {
    if (await stat(candidate).then((value) => value.isFile()).catch(() => false)) {
      found = candidate;
      break;
    }
  }
  if (!found) error("stdio clean consumer does not expose akb-mcp");
  const packageRoot = join(root, "node_modules/akb-mcp");
  const manifest = JSON.parse(await readFile(join(packageRoot, "package.json"), "utf8"));
  if (manifest.name !== "akb-mcp") error("stdio consumer package is not akb-mcp");
  const packageReal = await realpath(packageRoot).catch(() => null);
  const executableReal = await realpath(found).catch(() => null);
  if (!packageReal || !executableReal || !(executableReal === packageReal || executableReal.startsWith(`${packageReal}/`))) error("stdio executable is outside the clean consumer package");
  return { executable: found, cwd: root };
}

function buildConfig(target, inputs, pat, stdio) {
  const server = target === "http"
    ? { type: "http", url: inputs.mcpUrl, headers: { Authorization: `Bearer ${pat}` } }
    : { type: "stdio", command: stdio.executable, args: [], cwd: stdio.cwd, env: { AKB_MCP_URL: inputs.mcpUrl, AKB_PAT: pat } };
  return { mcpServers: { [SERVER_NAME]: { ...server, protocolEra: "modern" } } };
}

const SMOKE_OPERATIONS = ["initialize", "tools/list", "tools/call"];

async function tempConfig(config) {
  const directory = await mkdtemp(join(tmpdir(), "akb-mcp-inspector-"));
  await chmod(directory, 0o700);
  const path = join(directory, "config.json");
  await writeFile(path, `${JSON.stringify(config)}\n`, { mode: 0o600 });
  await chmod(path, 0o600);
  return { directory, path };
}

function inspectorEnvironment(runtimeRoot, inputs) {
  const env = { ...process.env, MCP_STORAGE_DIR: runtimeRoot, MCP_INSPECTOR_SECRET_STORE: "memory" }; // pragma: allowlist secret
  for (const name of [inputs.usernameEnv, inputs.passwordEnv, inputs.patEnv, "AKB_PAT", "AKB_MCP_URL", "DANGEROUSLY_OMIT_AUTH", "MCP_CATALOG_PATH", "MCP_CLIENT_CONFIG_PATH", "MCP_INSPECTOR_SECRET_FILE", "MCP_INSPECTOR_SECRET_KEY", "MCP_INSPECTOR_API_TOKEN"]) {
    if (name) delete env[name];
  }
  return env;
}

function operationArgs(info, configPath, method) {
  const args = [info.entry, "--cli", "--config", configPath, "--stored-auth-only", "--server", SERVER_NAME, "--method", method];
  if (method === "tools/list") args.push("--strict");
  args.push("--format", "json");
  if (method === "tools/call") args.push("--tool-name", "akb_list_vaults", "--tool-args-json", "{}");
  return args;
}

async function invoke(info, inputs, configPath, runtimeRoot, method, spawnProcess, secrets) {
  const child = spawnProcess(process.execPath, operationArgs(info, configPath, method), {
    cwd: REPOSITORY_ROOT,
    env: inspectorEnvironment(runtimeRoot, inputs),
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stdout = "";
  let stderr = "";
  let spawnError = null;
  child.stdout.on("data", (chunk) => { stdout += chunk.toString(); });
  child.stderr.on("data", (chunk) => { stderr += chunk.toString(); });
  child.on("error", (value) => { spawnError = value; });
  const code = await new Promise((resolveClose) => child.once("close", (value) => resolveClose(value ?? 1)));
  const result = { operation: method, status: code === 0 && !spawnError ? "passed" : "failed", exit_code: code };
  const diagnostics = redactText(stderr, secrets).trim();
  if (diagnostics) result.diagnostics = diagnostics;
  let parsed = null;
  try {
    const lines = stdout.trim().split(/\r?\n/).filter(Boolean);
    if (lines.length !== 1) error("Inspector returned invalid JSON output");
    parsed = JSON.parse(lines[0]);
    result.result = redact(parsed, secrets);
  } catch (value) {
    result.status = "failed";
    result.error = value instanceof Error ? value.message : "Inspector returned invalid JSON output";
    return { result, parsed: null, readSchema: null, publicResult: null };
  }
  if (result.status !== "passed") {
    result.error = spawnError ? "Inspector process could not start" : "Inspector process failed";
    return { result, parsed, readSchema: null, publicResult: null };
  }
  let payload;
  try {
    payload = object(parsed.result, "Inspector result");
  } catch {
    result.status = "failed";
    result.error = "Inspector result is not an object";
    return { result, parsed, readSchema: null, publicResult: null };
  }
  if (method === "initialize") {
    const serverInfo = payload.serverInfo ?? payload._meta?.["io.modelcontextprotocol/serverInfo"];
    if (payload.protocolVersion !== MODERN_PROTOCOL_VERSION || serverInfo === null || typeof serverInfo !== "object" || typeof serverInfo.name !== "string" || typeof serverInfo.version !== "string") {
      result.status = "failed";
      result.error = "initialize did not negotiate the modern protocol";
    }
  } else if (method === "tools/list") {
    const tools = Array.isArray(payload.tools) ? payload.tools : [];
    const readTool = tools.find((tool) => tool?.name === "akb_list_vaults");
    const findings = Array.isArray(parsed.schemaFindings) ? parsed.schemaFindings : [];
    const errors = findings.filter((finding) => finding?.severity === "error");
    const schema = readTool?.inputSchema;
    const schemaValid = schema !== null && typeof schema === "object" && !Array.isArray(schema);
    result.schema_findings = redact(findings, secrets);
    result.schema_warning_count = findings.length - errors.length;
    result.schema_error_count = errors.length;
    if (!readTool || !schemaValid || errors.length > 0) {
      result.status = "failed";
      result.error = !readTool ? "akb_list_vaults is missing from tools/list" : !schemaValid ? "akb_list_vaults has no input schema" : "tools/list contains schema errors";
    }
    return { result, parsed, readSchema: schemaValid ? schema : null, publicResult: null };
  } else {
    const text = Array.isArray(payload.content) ? payload.content.find((item) => item?.type === "text")?.text : null;
    let publicResult;
    try {
      publicResult = object(JSON.parse(text), "akb_list_vaults result");
    } catch {
      result.status = "failed";
      result.error = "akb_list_vaults did not return a JSON object";
      return { result, parsed, readSchema: null, publicResult: null };
    }
    if (payload.isError !== false || !Array.isArray(publicResult.vaults) || !Number.isInteger(publicResult.total) || !Number.isInteger(publicResult.returned) || publicResult.returned !== publicResult.vaults.length || publicResult.total < publicResult.returned) {
      result.status = "failed";
      result.error = "akb_list_vaults returned an invalid or error result";
    }
    return { result, parsed, readSchema: null, publicResult };
  }
  return { result, parsed, readSchema: null, publicResult: null };
}

async function runTransport(info, inputs, credential, target, spawnProcess) {
  const runtimeRoot = await mkdtemp(join(tmpdir(), "akb-mcp-inspector-run-"));
  await chmod(runtimeRoot, 0o700);
  let temporary = null;
  try {
    const stdio = target === "stdio" ? await resolveStdio(inputs) : null;
    const config = buildConfig(target, inputs, credential.pat, stdio);
    temporary = await tempConfig(config);
    const operations = [];
    let readSchema = null;
    let publicResult = null;
    for (const method of SMOKE_OPERATIONS) {
      const operation = await invoke(info, inputs, temporary.path, runtimeRoot, method, spawnProcess, credential.secrets);
      operations.push(operation.result);
      if (operation.readSchema) readSchema = operation.readSchema;
      if (operation.publicResult) publicResult = operation.publicResult;
      if (operation.result.status !== "passed") break;
    }
    while (operations.length < SMOKE_OPERATIONS.length) operations.push({ operation: SMOKE_OPERATIONS[operations.length], status: "not_run" });
    return { transport: target, status: operations.every((operation) => operation.status === "passed") ? "passed" : "failed", operations, readSchema, publicResult };
  } finally {
    if (temporary) await rm(temporary.directory, { recursive: true, force: true });
    await rm(runtimeRoot, { recursive: true, force: true });
  }
}

function compareTransports(http, stdio) {
  if (http.status !== "passed" || stdio.status !== "passed") return { status: "not_run" };
  const schemaMatch = isDeepStrictEqual(http.readSchema, stdio.readSchema);
  const resultMatch = isDeepStrictEqual(http.publicResult, stdio.publicResult);
  return { status: schemaMatch && resultMatch ? "passed" : "failed", schema_match: schemaMatch, result_match: resultMatch };
}

export async function runSmoke({ info, descriptor, target, fetchImpl = globalThis.fetch, spawnProcess = nodeSpawn } = {}) {
  let secrets = [];
  try {
    const inputs = runtimeInputs(descriptor, target);
    const coordinates = await discoverAndReset(inputs, fetchImpl);
    const credential = await resolveCredential(inputs, coordinates, fetchImpl);
    secrets = credential.secrets;
    const targets = target === "both" ? ["http", "stdio"] : [target];
    const transports = {};
    for (const name of targets) {
      try {
        transports[name] = await runTransport(info, inputs, credential, name, spawnProcess);
      } catch (value) {
        const errorMessage = redactText(messageOf(value), secrets);
        transports[name] = {
          transport: name,
          status: "failed",
          operations: SMOKE_OPERATIONS.map((operation) => ({ operation, status: "not_run" })),
          error: errorMessage,
        };
      }
    }
    const comparison = target === "both" ? compareTransports(transports.http, transports.stdio) : null;
    const passed = Object.values(transports).every((transport) => transport.status === "passed") && (!comparison || comparison.status === "passed");
    return { status: passed ? "passed" : "failed", intent: "smoke", target, inspector: { package: info.package, version: info.version, node_version: info.nodeVersion }, transports: Object.fromEntries(Object.entries(transports).map(([name, value]) => [name, { transport: value.transport, status: value.status, operations: value.operations, error: value.error }])), comparison };
  } catch (value) {
    return { status: "failed", intent: "smoke", target, error: redactText(messageOf(value), secrets) };
  }
}

export async function runInteractive(info, configPath, spawnProcess = nodeSpawn) {
  const path = resolve(configPath);
  if (!(await stat(path).then((value) => value.isFile()).catch(() => false))) error("interactive Inspector config file is missing");
  const env = { ...process.env, HOST: "127.0.0.1" };
  delete env.DANGEROUSLY_OMIT_AUTH;
  const child = spawnProcess(process.execPath, [info.entry, "--web", "--config", path], { cwd: REPOSITORY_ROOT, env, stdio: ["ignore", "inherit", "inherit"] });
  return await new Promise((resolveClose) => {
    child.once("error", () => resolveClose(1));
    child.once("close", (value) => resolveClose(value ?? 1));
  });
}

function usage() {
  return "Usage: npm run inspect -- --intent smoke --target <http|stdio|both> --descriptor <path|->\n       npm run inspect -- --intent interactive --config <mcp-config.json>\n";
}

export async function main(argv = process.argv.slice(2)) {
  let args;
  try {
    args = parseArguments(argv);
    if (args.help) {
      process.stdout.write(usage());
      return 0;
    }
    const info = await inspectInstallation();
    if (args.intent === "interactive") return await runInteractive(info, args.config);
    const descriptor = await loadDescriptor(args.descriptor);
    const output = await runSmoke({ info, descriptor, target: args.target });
    process.stdout.write(`${JSON.stringify(output)}\n`);
    return output.status === "passed" ? 0 : 1;
  } catch (value) {
    process.stdout.write(`${JSON.stringify({ status: "failed", intent: args?.intent ?? null, target: args?.target ?? null, error: messageOf(value) })}\n`);
    return 1;
  }
}

if (process.argv[1] && resolve(fileURLToPath(import.meta.url)) === resolve(process.argv[1])) main().then((code) => { process.exitCode = code; });
