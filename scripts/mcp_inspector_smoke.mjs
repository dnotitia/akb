#!/usr/bin/env node

import { createHash } from "node:crypto";
import { constants as fsConstants, readFileSync } from "node:fs";
import { mkdtemp, open, readFile, realpath, rm, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, isAbsolute, join, relative, resolve } from "node:path";
import { parseArgs as parseNodeArgs } from "node:util";
import { fileURLToPath } from "node:url";
import { execFile as nodeExecFile, spawn as nodeSpawn } from "node:child_process";
import { promisify } from "node:util";

export const INSPECTOR_PACKAGE = "@modelcontextprotocol/inspector";
export const INSPECTOR_VERSION = "2.4.0";
export const INSPECTOR_BIN = "clients/launcher/build/index.js";
export const MIN_NODE_VERSION = "22.19.0";
export const SCHEMA_VERSION = 2;
export const MODERN_PROTOCOL_VERSION = "2026-07-28";
export const CONFIG_HANDOFF = "private_fifo";
export const SERVER_NAME = "akb";

export const FAILURE_CLASSES = Object.freeze({
  unexpected: "unexpected_failure",
  usage: "usage_configuration_error",
  authentication: "authentication_required",
  unreachable: "server_unreachable",
  tool: "tool_result_error",
  schema: "schema_portability_error",
});

export const FAILURE_EXIT_CODES = Object.freeze({
  [FAILURE_CLASSES.unexpected]: 1,
  [FAILURE_CLASSES.usage]: 2,
  [FAILURE_CLASSES.authentication]: 3,
  [FAILURE_CLASSES.unreachable]: 4,
  [FAILURE_CLASSES.tool]: 5,
  [FAILURE_CLASSES.schema]: 6,
});

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
export const REPOSITORY_ROOT = resolve(SCRIPT_DIR, "..");
export const TOOLING_ROOT = resolve(REPOSITORY_ROOT, "tools", "mcp-inspector");
const SERVER_INFO_META_KEY = "io.modelcontextprotocol/serverInfo";
const SOURCE_REVISION_RE = /^[0-9a-f]{40,64}$/i;
const ENV_NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;
const MAX_DIAGNOSTIC_CHARS = 4000;
const CONFIG_FIFO_NAME = "inspector-config.fifo";
const FIFO_RETRY_DELAY_MS = 10;
const execFile = promisify(nodeExecFile);

export class DiagnosticError extends Error {
  constructor(failureClass, message) {
    super(message);
    this.name = "DiagnosticError";
    this.failureClass = failureClass;
  }
}

function configuration(message) {
  return new DiagnosticError(FAILURE_CLASSES.usage, message);
}

function unexpected(message) {
  return new DiagnosticError(FAILURE_CLASSES.unexpected, message);
}

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function requireRecord(value, message) {
  if (!isRecord(value)) throw configuration(message);
  return value;
}

function requireNonEmptyString(value, message) {
  if (typeof value !== "string" || value.trim() === "") {
    throw configuration(message);
  }
  return value;
}

function requireArray(value, message) {
  if (!Array.isArray(value)) throw configuration(message);
  return value;
}

function parseVersion(value) {
  const match = String(value).trim().replace(/^v/, "").match(/^(\d+)\.(\d+)\.(\d+)/);
  return match ? match.slice(1).map(Number) : null;
}

export function nodeVersionMeetsFloor(value, floor = MIN_NODE_VERSION) {
  const actual = parseVersion(value);
  const minimum = parseVersion(floor);
  if (!actual || !minimum) return false;
  for (let index = 0; index < 3; index += 1) {
    if (actual[index] !== minimum[index]) return actual[index] > minimum[index];
  }
  return true;
}

function validEnvName(value) {
  return typeof value === "string" && ENV_NAME_RE.test(value);
}

function parseUrl(value, message) {
  let url;
  try {
    url = new URL(value);
  } catch {
    throw configuration(message);
  }
  if (!/^https?:$/.test(url.protocol) || url.username || url.password) {
    throw configuration(message);
  }
  return url;
}

function requireOperation(service, key, method, origin) {
  const operation = requireRecord(service[key], `descriptor is missing ${key} coordinate`);
  if (operation.method !== method) {
    throw configuration(`${key} coordinate has an unsupported method`);
  }
  const url = parseUrl(operation.url, `${key} coordinate has an invalid URL`);
  if (url.origin !== origin.origin) {
    throw configuration(`${key} coordinate must stay on its declared service origin`);
  }
  return operation;
}

function serviceCoordinate(value, expectedService, expectedMethod, appOrigin, label) {
  const coordinate = requireRecord(value, `runtime discovery is missing ${label}`);
  if (coordinate.service !== expectedService || coordinate.method !== expectedMethod) {
    throw configuration(`${label} coordinate does not target the expected service`);
  }
  const path = requireNonEmptyString(coordinate.path, `${label} coordinate is missing a path`);
  if (!path.startsWith("/") || path.startsWith("//") || path.includes("#")) {
    throw configuration(`${label} coordinate has an invalid path`);
  }
  const urlObject = new URL(path, appOrigin);
  if (urlObject.origin !== new URL(appOrigin).origin) {
    throw configuration(`${label} coordinate escaped the app service origin`);
  }
  const url = urlObject.toString();
  return { ...coordinate, url };
}

function uniqueStrings(value, label) {
  const items = requireArray(value, `${label} must be an array`);
  if (items.some((item) => typeof item !== "string" || item.length === 0)) {
    throw configuration(`${label} must contain non-empty strings`);
  }
  if (new Set(items).size !== items.length) throw configuration(`${label} must not contain duplicates`);
  return items;
}

function validateObservable(value) {
  const observable = requireRecord(value, "representative observable outcome is missing");
  if (observable.content_type !== "json" || observable.result_type !== "object") {
    throw configuration("representative observable outcome must declare JSON object content");
  }
  const requiredKeys = uniqueStrings(observable.required_keys, "representative observable required_keys");
  const itemsKey = requireNonEmptyString(observable.items_key, "representative observable items_key is missing");
  if (!requiredKeys.includes(itemsKey) || !requiredKeys.includes("total") || !requiredKeys.includes("returned")) {
    throw configuration("representative observable outcome must declare item and count keys");
  }
  if (observable.is_error !== false) throw configuration("representative observable outcome must require isError=false");
  if (observable.count_rule !== "total>=returned==items.length") {
    throw configuration("representative observable count rule is unsupported");
  }
  return observable;
}

function validateProxyLocal(value) {
  const proxyLocal = requireRecord(value, "runtime discovery is missing proxy-local tool metadata");
  const tools = uniqueStrings(proxyLocal.tools, "proxy_local.tools");
  const inputProperties = requireRecord(proxyLocal.input_properties, "proxy_local.input_properties is missing");
  for (const [tool, properties] of Object.entries(inputProperties)) {
    if (typeof tool !== "string" || tool.length === 0) throw configuration("proxy-local schema metadata names an invalid tool");
    uniqueStrings(properties, `proxy_local.input_properties.${tool}`);
  }
  return { tools, inputProperties };
}

function validateCredentialContract(descriptor, discovery, appOrigin, target) {
  const credentials = requireRecord(descriptor.credentials, "descriptor is missing credentials");
  for (const key of ["username_env", "password_env"]) {
    if (!validEnvName(credentials[key])) throw configuration(`credentials.${key} is missing or invalid`);
  }
  if (credentials.pat_env !== undefined && !validEnvName(credentials.pat_env)) {
    throw configuration("credentials.pat_env is invalid");
  }
  requireNonEmptyString(credentials.login_path, "credentials.login_path is missing");

  const access = requireRecord(discovery.access, "runtime discovery is missing access coordinates");
  const login = requireRecord(access.login, "runtime discovery is missing login coordinate");
  if (login.service !== "app" || login.method !== "POST" || !login.path.startsWith("/") || login.path.startsWith("//")) {
    throw configuration("runtime login coordinate is invalid");
  }
  const loginFields = uniqueStrings(login.fields, "runtime login fields");
  if (!loginFields.includes("username") || !loginFields.includes("password")) {
    throw configuration("runtime login coordinate must declare username and password fields");
  }
  if (credentials.login_path !== login.path) throw configuration("descriptor and discovery login paths disagree");

  const runtime = requireRecord(discovery.runtime, "runtime discovery is missing runtime evidence");
  const credentialEnv = requireRecord(runtime.credential_env, "runtime discovery is missing credential environment names");
  for (const key of ["username", "password", "pat"]) {
    if (!validEnvName(credentialEnv[key])) throw configuration(`runtime credential environment name ${key} is invalid`);
  }
  if (credentialEnv.username !== credentials.username_env || credentialEnv.password !== credentials.password_env) {
    throw configuration("descriptor and runtime credential environment names disagree");
  }
  if (credentials.pat_env !== undefined && credentialEnv.pat !== credentials.pat_env) {
    throw configuration("descriptor and runtime PAT environment names disagree");
  }
  const pat = requireRecord(runtime.pat, "runtime discovery is missing PAT mint coordinate");
  const mint = serviceCoordinate(pat.mint, "app", "POST", appOrigin, "PAT mint");
  const bodyFields = uniqueStrings(pat.mint.body_fields, "PAT mint body_fields");
  if (!bodyFields.includes("name") || pat.mint.auth !== "login_session") {
    throw configuration("PAT mint coordinate must declare login_session and name");
  }

  if (target === "stdio" || target === "both") {
    if (!validEnvName(credentials.pat_env)) throw configuration("stdio target requires credentials.pat_env");
  }
  return {
    credentials,
    credentialEnv,
    login: { ...login, url: new URL(login.path, appOrigin).toString() },
    mint,
  };
}

export function validateDescriptor(descriptor, target = "http") {
  requireRecord(descriptor, "descriptor must be a JSON object");
  if (descriptor.schema_version !== SCHEMA_VERSION || descriptor.status !== "ready") {
    throw configuration("descriptor must be a ready schema-v2 descriptor");
  }
  const scenario = requireNonEmptyString(descriptor.scenario, "descriptor scenario is missing");
  if (!["http", "stdio", "both"].includes(target)) throw configuration("target must be http, stdio, or both");

  const services = requireRecord(descriptor.services, "descriptor is missing services");
  const app = requireRecord(services.app, "descriptor is missing app service");
  const fixture = requireRecord(services.fixture, "descriptor is missing fixture service");
  const appOrigin = parseUrl(requireNonEmptyString(app.origin, "app service origin is missing"), "app service origin is invalid");
  const fixtureOrigin = parseUrl(requireNonEmptyString(fixture.origin, "fixture service origin is missing"), "fixture service origin is invalid");
  requireOperation(app, "health", "GET", appOrigin);
  requireOperation(app, "discovery", "GET", appOrigin);
  requireOperation(fixture, "health", "GET", fixtureOrigin);
  requireOperation(fixture, "discovery", "GET", fixtureOrigin);
  const reset = requireOperation(fixture, "reset", "POST", fixtureOrigin);
  const resetBody = requireRecord(reset.body, "fixture reset body is missing");
  if (resetBody.scenario !== scenario) throw configuration("fixture reset scenario does not match descriptor scenario");

  let stdio = null;
  if (target === "stdio" || target === "both") {
    stdio = requireRecord(services.stdio, "stdio target is missing from descriptor");
    if (stdio.transport !== "stdio" || stdio.package !== "akb-mcp" || stdio.executable !== "akb-mcp") {
      throw configuration("stdio descriptor must identify the installed akb-mcp executable");
    }
    if (!isAbsolute(stdio.consumer_root)) throw configuration("stdio consumer_root must be absolute");
    const environment = requireRecord(stdio.environment, "stdio environment mapping is missing");
    requireNonEmptyString(environment.AKB_MCP_URL, "stdio AKB_MCP_URL mapping is missing");
    requireNonEmptyString(environment.AKB_PAT, "stdio AKB_PAT environment-name mapping is missing");
  }

  return {
    raw: descriptor,
    scenario,
    services: { app, fixture, stdio },
    appOrigin,
    fixtureOrigin,
    credentials: descriptor.credentials,
    reset,
  };
}

export async function loadDescriptor(source) {
  const descriptorSource = requireNonEmptyString(source, "descriptor path is required");
  let text;
  try {
    text = descriptorSource === "-" ? readFileSync(0, "utf8") : await readFile(descriptorSource, "utf8");
  } catch {
    throw configuration("could not read the runtime descriptor");
  }
  try {
    const descriptor = JSON.parse(text);
    return descriptor;
  } catch {
    throw configuration("runtime descriptor is not valid JSON");
  }
}

export async function inspectInstallation({ toolingRoot = TOOLING_ROOT, nodeVersion = process.versions.node } = {}) {
  if (!nodeVersionMeetsFloor(nodeVersion)) {
    throw configuration(`Node.js ${MIN_NODE_VERSION} or newer is required`);
  }
  const packageRoot = resolve(toolingRoot, "node_modules", "@modelcontextprotocol", "inspector");
  const packageFile = join(packageRoot, "package.json");
  let manifest;
  try {
    manifest = JSON.parse(await readFile(packageFile, "utf8"));
  } catch {
    throw configuration("exact MCP Inspector installation is missing; run npm ci in tools/mcp-inspector");
  }
  if (manifest.name !== INSPECTOR_PACKAGE || manifest.version !== INSPECTOR_VERSION) {
    throw configuration("the installed MCP Inspector is not the exact required version");
  }
  if (!isRecord(manifest.engines) || manifest.engines.node !== `>=${MIN_NODE_VERSION}`) {
    throw configuration("the installed MCP Inspector does not declare the required Node.js floor");
  }
  const declaredBin = isRecord(manifest.bin) ? manifest.bin["mcp-inspector"] : null;
  if (typeof declaredBin !== "string" || declaredBin.replace(/^\.\//, "") !== INSPECTOR_BIN) {
    throw configuration("the installed MCP Inspector does not expose the expected public bin");
  }
  const entry = resolve(packageRoot, declaredBin);
  const entryRelative = relative(packageRoot, entry);
  if (entryRelative.startsWith("..") || !(await isFile(entry))) {
    throw configuration("the MCP Inspector public bin is not installed");
  }
  return {
    package: INSPECTOR_PACKAGE,
    version: manifest.version,
    bin: INSPECTOR_BIN,
    entry,
    nodeVersion: String(nodeVersion).replace(/^v/, ""),
  };
}

async function isFile(path) {
  try {
    return (await stat(path)).isFile();
  } catch {
    return false;
  }
}

function redactText(value, secrets) {
  let output = String(value ?? "");
  for (const secret of secrets.filter((item) => typeof item === "string" && item.length > 0)) {
    output = output.split(secret).join("[REDACTED]");
  }
  return output.replace(/Bearer\s+[^\s,}]+/gi, "Bearer [REDACTED]");
}

export function redactValue(value, secrets = []) {
  if (typeof value === "string") return redactText(value, secrets);
  if (Array.isArray(value)) return value.map((item) => redactValue(item, secrets));
  if (isRecord(value)) {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, redactValue(item, secrets)]));
  }
  return value;
}

export function stableStringify(value) {
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`;
  if (isRecord(value)) {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableStringify(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

export function configDigest(config, secrets = []) {
  return `sha256:${createHash("sha256").update(stableStringify(redactValue(config, secrets))).digest("hex")}`;
}

export function parseArguments(argv) {
  let values;
  try {
    ({ values } = parseNodeArgs({
      args: argv,
      options: {
        intent: { type: "string" },
        target: { type: "string" },
        descriptor: { type: "string" },
        help: { type: "boolean", short: "h" },
      },
      strict: true,
      allowPositionals: false,
    }));
  } catch {
    throw configuration("invalid command options");
  }
  const result = {
    intent: typeof values.intent === "string" ? values.intent : null,
    target: typeof values.target === "string" ? values.target : null,
    descriptor: typeof values.descriptor === "string" ? values.descriptor : null,
    help: values.help === true,
  };
  if (result.help) return result;
  if (!["interactive", "smoke"].includes(result.intent)) throw configuration("intent must be interactive or smoke");
  if (!["http", "stdio", "both"].includes(result.target)) throw configuration("target must be http, stdio, or both");
  if (result.intent === "interactive" && result.target === "both") {
    throw configuration("interactive intent accepts one explicit target at a time");
  }
  if (!result.descriptor) throw configuration("descriptor path is required");
  return result;
}

export function validateRuntimeDiscovery(descriptor, discovery, target) {
  requireRecord(discovery, "runtime discovery must be a JSON object");
  if (discovery.status !== "ready" || discovery.scenario !== descriptor.scenario) {
    throw configuration("runtime discovery is not ready for the selected scenario");
  }
  const runtime = requireRecord(discovery.runtime, "runtime discovery is missing runtime evidence");
  const sourceRevision = requireNonEmptyString(runtime.source_revision, "runtime discovery source revision is missing");
  if (!SOURCE_REVISION_RE.test(sourceRevision)) throw configuration("runtime discovery source revision is invalid");
  const descriptorRevision = descriptor.raw.evidence?.source_revision;
  if (descriptorRevision !== undefined && descriptorRevision !== sourceRevision) {
    throw configuration("descriptor and runtime discovery source revisions disagree");
  }
  const profile = requireNonEmptyString(runtime.profile ?? descriptor.raw.profile, "runtime profile is missing");
  if (descriptor.raw.profile !== undefined && descriptor.raw.profile !== profile) {
    throw configuration("descriptor and runtime discovery profiles disagree");
  }
  const consumerSmoke = requireRecord(runtime.consumer_smoke, "runtime discovery is missing consumer-smoke coordinates");
  if (consumerSmoke.protocol_era !== "modern" || consumerSmoke.protocol_version !== MODERN_PROTOCOL_VERSION) {
    throw configuration("consumer-smoke must pin the modern protocol era");
  }
  const http = serviceCoordinate(consumerSmoke.http, "app", "POST", descriptor.appOrigin.toString(), "HTTP MCP");
  const requiredTools = uniqueStrings(consumerSmoke.required_tools, "consumer_smoke.required_tools");
  const sharedTools = uniqueStrings(consumerSmoke.shared_tools, "consumer_smoke.shared_tools");
  const representative = requireRecord(consumerSmoke.representative, "consumer-smoke representative operation is missing");
  const tool = requireNonEmptyString(representative.tool, "representative tool is missing");
  if (representative.read_only !== true) throw configuration("representative tool must be declared read-only");
  const argumentsObject = requireRecord(representative.arguments, "representative arguments must be a JSON object");
  const observable = validateObservable(representative.observable);
  const proxyLocal = validateProxyLocal(consumerSmoke.proxy_local);
  if (!requiredTools.includes(tool) || !sharedTools.includes(tool)) {
    throw configuration("representative tool must be the declared shared read tool");
  }
  if (target === "stdio" || target === "both") {
    const stdio = descriptor.services.stdio;
    if (stdio.environment.AKB_MCP_URL !== http.url) throw configuration("stdio and HTTP MCP coordinates disagree");
    if (stdio.environment.AKB_PAT !== descriptor.credentials.pat_env) {
      throw configuration("stdio PAT mapping and credential env-name disagree");
    }
  }
  const fixture = requireRecord(runtime.fixture, "runtime discovery is missing fixture evidence");
  const generation = requireNonEmptyString(fixture.generation, "fixture generation is missing");
  const reset = requireRecord(fixture.reset, "runtime discovery is missing reset evidence");
  if (reset.method !== "POST" || reset.url !== descriptor.reset.url || reset.body?.scenario !== descriptor.scenario) {
    throw configuration("runtime discovery reset evidence disagrees with the descriptor");
  }
  const credentialContract = validateCredentialContract(descriptor, discovery, descriptor.appOrigin.toString(), target);
  return {
    sourceRevision,
    profile,
    protocolEra: consumerSmoke.protocol_era,
    protocolVersion: consumerSmoke.protocol_version,
    http,
    requiredTools,
    sharedTools,
    representative: { tool, readOnly: true, arguments: argumentsObject, observable },
    proxyLocal,
    fixtureGeneration: generation,
    fixtureReset: reset,
    credentialEnv: credentialContract.credentialEnv,
    login: credentialContract.login,
    mint: credentialContract.mint,
  };
}

async function requestJson(url, { fetchImpl = globalThis.fetch, method = "GET", body, headers = {}, purpose = "service" } = {}) {
  let response;
  try {
    response = await fetchImpl(url, {
      method,
      headers: {
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        ...headers,
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      signal: typeof AbortSignal?.timeout === "function" ? AbortSignal.timeout(30000) : undefined,
    });
  } catch {
    throw new DiagnosticError(FAILURE_CLASSES.unreachable, `${purpose} endpoint was unreachable`);
  }
  let text = "";
  try {
    text = await response.text();
  } catch {
    throw new DiagnosticError(FAILURE_CLASSES.unreachable, `${purpose} endpoint returned no response`);
  }
  if (!response.ok) {
    if (response.status === 401 || response.status === 403) {
      throw new DiagnosticError(FAILURE_CLASSES.authentication, `${purpose} authentication was rejected`);
    }
    throw new DiagnosticError(FAILURE_CLASSES.unreachable, `${purpose} endpoint returned an unavailable status`);
  }
  try {
    return JSON.parse(text);
  } catch {
    throw configuration(`${purpose} endpoint returned invalid JSON`);
  }
}

async function checkReady(descriptor, fetchImpl) {
  const appHealth = await requestJson(descriptor.services.app.health.url, { fetchImpl, purpose: "app readiness" });
  if (appHealth.status !== "ready") throw new DiagnosticError(FAILURE_CLASSES.unreachable, "app readiness did not become ready");
  const fixtureHealth = await requestJson(descriptor.services.fixture.health.url, { fetchImpl, purpose: "fixture readiness" });
  if (fixtureHealth.status !== "ready") throw new DiagnosticError(FAILURE_CLASSES.unreachable, "fixture readiness did not become ready");
}

async function resetFixture(descriptor, fetchImpl) {
  const reset = await requestJson(descriptor.reset.url, {
    fetchImpl,
    method: "POST",
    body: descriptor.reset.body,
    purpose: "fixture reset",
  });
  if (reset.status !== "ready" || reset.scenario !== descriptor.scenario) {
    throw configuration("fixture reset did not return the selected ready scenario");
  }
  const generation = requireNonEmptyString(reset.generation, "fixture reset did not return a generation");
  return generation;
}

async function resolveCredential(descriptor, discovery, fetchImpl) {
  const names = descriptor.credentials;
  const patEnv = names.pat_env ?? discovery.credentialEnv.pat;
  const username = process.env[names.username_env] ?? "";
  const password = process.env[names.password_env] ?? "";
  if (!username || !password) throw configuration("declared credential environment values are missing");
  const suppliedPat = process.env[patEnv] ?? "";
  if (suppliedPat) return { pat: suppliedPat, secrets: [username, password, suppliedPat] };

  const loginPayload = await requestJson(discovery.login.url, {
    fetchImpl,
    method: "POST",
    body: { username, password },
    purpose: "credential login",
  });
  const sessionToken = loginPayload?.token;
  if (typeof sessionToken !== "string" || sessionToken.length === 0) {
    throw unexpected("credential login returned an invalid session");
  }
  const tokenPayload = await requestJson(discovery.mint.url, {
    fetchImpl,
    method: "POST",
    headers: { Authorization: `Bearer ${sessionToken}` },
    body: { name: "mcp-inspector" },
    purpose: "credential mint",
  });
  const pat = tokenPayload?.token;
  if (typeof pat !== "string" || !pat.startsWith("akb_")) throw unexpected("credential mint returned an invalid PAT");
  return { pat, secrets: [username, password, sessionToken, pat] };
}

export async function resolveStdioExecutable(descriptor) {
  const consumerRoot = resolve(descriptor.services.stdio.consumer_root);
  const rootReal = await realpath(consumerRoot).catch(() => null);
  if (!rootReal) throw configuration("stdio clean-consumer root is not available");
  const candidates = [
    join(rootReal, "node_modules", ".bin", "akb-mcp"),
    join(rootReal, "node_modules", "akb-mcp", "bin", "akb-mcp.mjs"),
  ];
  let executable = null;
  for (const candidate of candidates) {
    if (await isFile(candidate)) {
      executable = candidate;
      break;
    }
  }
  if (!executable) throw configuration("clean-consumer akb-mcp executable is missing");
  const packageRoot = join(rootReal, "node_modules", "akb-mcp");
  const packageManifest = join(packageRoot, "package.json");
  let manifest;
  try {
    manifest = JSON.parse(await readFile(packageManifest, "utf8"));
  } catch {
    throw configuration("clean-consumer akb-mcp package metadata is missing");
  }
  if (manifest.name !== "akb-mcp") throw configuration("stdio target is not the installed akb-mcp package");
  const expectedVersion = descriptor.raw.evidence?.proxy_artifact_version;
  if (expectedVersion !== undefined && manifest.version !== expectedVersion) {
    throw configuration("clean-consumer akb-mcp version does not match runtime evidence");
  }
  const executableReal = await realpath(executable).catch(() => null);
  const packageReal = await realpath(packageRoot).catch(() => null);
  if (!executableReal || !packageReal || !(executableReal === packageReal || executableReal.startsWith(`${packageReal}/`))) {
    throw configuration("stdio target is outside the clean-consumer package boundary");
  }
  return { executable, cwd: rootReal, packageVersion: manifest.version };
}

export function buildInspectorConfig(target, discovery, pat, stdio) {
  const common = {
    protocolEra: discovery.protocolEra,
    connectionTimeout: 15000,
    requestTimeout: 30000,
  };
  if (target === "http") {
    return {
      mcpServers: {
        [SERVER_NAME]: {
          type: "http",
          url: discovery.http.url,
          headers: { Authorization: `Bearer ${pat}` },
          ...common,
        },
      },
    };
  }
  if (!stdio) throw configuration("stdio configuration requires a clean-consumer executable");
  return {
    mcpServers: {
      [SERVER_NAME]: {
        type: "stdio",
        command: stdio.executable,
        args: [],
        cwd: stdio.cwd,
        env: {
          AKB_MCP_URL: discovery.http.url,
          AKB_PAT: pat,
        },
        ...common,
      },
    },
  };
}

function childEnvironment(runtimeRoot, descriptor, interactive = false) {
  const environment = { ...process.env };
  for (const key of [
    descriptor.credentials.username_env,
    descriptor.credentials.password_env,
    descriptor.credentials.pat_env,
    "AKB_PAT",
    "AKB_MCP_URL",
    "MCP_CATALOG_PATH",
    "MCP_INSPECTOR_SECRET_FILE",
    "MCP_INSPECTOR_SECRET_KEY",
    "MCP_INSPECTOR_API_TOKEN",
    "DANGEROUSLY_OMIT_AUTH",
  ]) {
    if (key) delete environment[key];
  }
  environment.MCP_STORAGE_DIR = runtimeRoot;
  environment.MCP_INSPECTOR_SECRET_STORE = "memory"; // pragma: allowlist secret
  if (interactive) environment.HOST = "127.0.0.1";
  return environment;
}

export function inspectorArguments(info, method, representative = null, {
  configPath,
  interactive = false,
  strict = false,
} = {}) {
  if (typeof configPath !== "string" || configPath.length === 0) {
    throw configuration("private Inspector config handoff path is missing");
  }
  const args = [info.entry];
  args.push(interactive ? "--web" : "--cli", "--config", configPath);
  if (!interactive) {
    args.push("--stored-auth-only", "--server", SERVER_NAME, "--method", method);
    if (strict) args.push("--strict");
    args.push("--format", "json");
    if (method === "tools/call") {
      args.push("--tool-name", representative.tool, "--tool-args-json", JSON.stringify(representative.arguments));
    }
  }
  return args;
}

export async function createConfigFifo(fifoPath) {
  if (process.platform === "win32") {
    throw configuration("private FIFO Inspector config handoff is unavailable on Windows");
  }
  try {
    await execFile("mkfifo", ["-m", "600", fifoPath], {
      env: { PATH: process.env.PATH ?? "/usr/bin:/bin" },
      windowsHide: true,
    });
    const fifo = await stat(fifoPath);
    if (!fifo.isFIFO() || (fifo.mode & 0o077) !== 0) throw new Error("invalid FIFO permissions");
  } catch {
    throw configuration("could not create the private Inspector config handoff");
  }
}

export async function writeConfigToFifo(fifoPath, config, child) {
  const payload = `${JSON.stringify(config)}\n`;
  while (child.exitCode == null && child.signalCode == null) {
    let handle;
    try {
      handle = await open(fifoPath, fsConstants.O_WRONLY | fsConstants.O_NONBLOCK);
    } catch (error) {
      if (error?.code !== "ENXIO" && error?.code !== "EAGAIN") {
        throw configuration("could not open the private Inspector config handoff");
      }
      await new Promise((resolveDelay) => setTimeout(resolveDelay, FIFO_RETRY_DELAY_MS));
      continue;
    }
    try {
      await handle.writeFile(payload, "utf8");
    } catch {
      throw configuration("could not write the private Inspector config handoff");
    } finally {
      await handle.close();
    }
    return;
  }
  throw configuration("Inspector exited before reading the private config handoff");
}

export async function runInspectorInvocation({
  info,
  descriptor,
  config,
  method,
  representative,
  runtimeRoot,
  spawnProcess = nodeSpawn,
  interactive = false,
}) {
  const fifoPath = join(runtimeRoot, CONFIG_FIFO_NAME);
  await createConfigFifo(fifoPath);
  try {
    const args = inspectorArguments(info, method, representative, {
      configPath: fifoPath,
      interactive,
      strict: method === "tools/list",
    });
    const child = spawnProcess(process.execPath, args, {
      cwd: REPOSITORY_ROOT,
      env: childEnvironment(runtimeRoot, descriptor, interactive),
      stdio: interactive ? ["ignore", "inherit", "inherit"] : ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    let spawnError = null;
    if (!interactive) {
      child.stdout.on("data", (chunk) => { stdout += chunk.toString(); });
      child.stderr.on("data", (chunk) => { stderr += chunk.toString(); });
    }
    child.on("error", (error) => { spawnError = error; });
    const resultPromise = new Promise((resolveResult) => {
      child.once("close", (code) => {
        resolveResult({
          stdout: interactive ? "" : stdout,
          stderr: interactive ? "" : stderr,
          code: code ?? 1,
          error: spawnError,
        });
      });
    });
    const forwardSignal = (signal) => { child.kill(signal); };
    if (interactive) {
      process.once("SIGINT", forwardSignal);
      process.once("SIGTERM", forwardSignal);
    }
    try {
      await writeConfigToFifo(fifoPath, config, child);
      return await resultPromise;
    } catch (error) {
      if (child.exitCode == null && child.signalCode == null) child.kill?.("SIGTERM");
      throw error;
    } finally {
      if (interactive) {
        process.off("SIGINT", forwardSignal);
        process.off("SIGTERM", forwardSignal);
      }
    }
  } finally {
    await rm(fifoPath, { force: true });
  }
}

export function classifyInspectorFailure(code, method, spawnError = null) {
  if (spawnError) return FAILURE_CLASSES.unexpected;
  if (code === 0) return null;
  if (code === 3) return FAILURE_CLASSES.authentication;
  if (code === 4) return FAILURE_CLASSES.unreachable;
  if (code === 5) return FAILURE_CLASSES.tool;
  if (code === 6 && method === "tools/list") return FAILURE_CLASSES.schema;
  if (code === 1) return FAILURE_CLASSES.usage;
  return FAILURE_CLASSES.unexpected;
}

function parseInspectorOutput(stdout) {
  const text = String(stdout ?? "").trim();
  if (!text) throw unexpected("Inspector returned no machine-readable result");
  const lines = text.split(/\r?\n/).filter((line) => line.trim() !== "");
  if (lines.length !== 1) throw unexpected("Inspector returned more than one JSON result");
  try {
    return JSON.parse(lines[0]);
  } catch {
    throw unexpected("Inspector returned invalid machine-readable JSON");
  }
}

function identityFromResult(result) {
  const direct = result?.serverInfo;
  const metadata = result?._meta?.[SERVER_INFO_META_KEY];
  const identity = isRecord(direct) ? direct : metadata;
  if (!isRecord(identity) || typeof identity.name !== "string" || typeof identity.version !== "string") {
    throw unexpected("Inspector did not report server identity");
  }
  return { name: identity.name, version: identity.version };
}

function resultEnvelope(parsed) {
  const envelope = requireRecord(parsed, "Inspector result is not an object");
  const result = requireRecord(envelope.result, "Inspector result envelope is missing result");
  return { envelope, result };
}

function safeDiagnostics(stderr, secrets) {
  const sanitized = redactText(stderr, secrets).trim();
  return sanitized.length > MAX_DIAGNOSTIC_CHARS ? sanitized.slice(-MAX_DIAGNOSTIC_CHARS) : sanitized;
}

function normalizeSchemaValue(value, removals = new Set()) {
  if (Array.isArray(value)) return value.map((item) => normalizeSchemaValue(item, removals));
  if (!isRecord(value)) return value;
  const output = {};
  for (const key of Object.keys(value).sort()) {
    if (["description", "title", "$comment"].includes(key)) continue;
    if (key === "properties" && isRecord(value[key])) {
      output[key] = Object.fromEntries(Object.entries(value[key])
        .filter(([property]) => !removals.has(property))
        .map(([property, item]) => [property, normalizeSchemaValue(item)]));
      continue;
    }
    if (key === "required" && Array.isArray(value[key])) {
      output[key] = value[key].filter((property) => !removals.has(property)).sort();
      continue;
    }
    output[key] = normalizeSchemaValue(value[key]);
  }
  return output;
}

function hasTopLevelSchemaProperty(schema, property) {
  return isRecord(schema)
    && isRecord(schema.properties)
    && Object.prototype.hasOwnProperty.call(schema.properties, property);
}

function toolByName(tools, name) {
  return tools.find((tool) => isRecord(tool) && tool.name === name) ?? null;
}

function validateToolList(result, contract) {
  if (result.isError === true) throw new DiagnosticError(FAILURE_CLASSES.tool, "Inspector tools/list returned isError:true");
  if (!Array.isArray(result.tools)) throw new DiagnosticError(FAILURE_CLASSES.schema, "Inspector tools/list result has no tools array");
  const requiredPresence = Object.fromEntries(contract.requiredTools.map((name) => [name, Boolean(toolByName(result.tools, name))]));
  if (Object.values(requiredPresence).some((present) => !present)) {
    throw unexpected("required MCP tool is missing from the catalog");
  }
  return { tools: result.tools, requiredPresence };
}

function parsePublicToolResult(result) {
  if (!Array.isArray(result.content)) throw unexpected("representative tool result has no public content");
  const textItem = result.content.find((item) => isRecord(item) && item.type === "text" && typeof item.text === "string");
  if (!textItem) throw unexpected("representative tool result has no JSON text content");
  let publicResult;
  try {
    publicResult = JSON.parse(textItem.text);
  } catch {
    throw unexpected("representative tool result content is not JSON");
  }
  return { publicResult, isError: result.isError === true };
}

function observableMatches(publicResult, isError, observable) {
  if (isError !== (observable.is_error === true) || !isRecord(publicResult)) return false;
  if (observable.required_keys.some((key) => !Object.prototype.hasOwnProperty.call(publicResult, key))) return false;
  const items = publicResult[observable.items_key];
  return Array.isArray(items)
    && Number.isInteger(publicResult.total)
    && Number.isInteger(publicResult.returned)
    && publicResult.returned === items.length
    && publicResult.total >= publicResult.returned;
}

async function runOperation({ info, descriptor, config, method, discovery, runtimeRoot, secrets, spawnProcess }) {
  const invocation = await runInspectorInvocation({
    info,
    descriptor,
    config,
    method,
    representative: discovery.representative,
    runtimeRoot,
    spawnProcess,
  });
  const failureClass = classifyInspectorFailure(invocation.code, method, invocation.error);
  const evidence = {
    operation: method,
    status: failureClass ? "failed" : "passed",
    exit_code: invocation.code,
    failure_class: failureClass,
    protocol_era: discovery.protocolEra,
    protocol_version: discovery.protocolVersion,
  };
  if (method === "tools/call") {
    evidence.tool = discovery.representative.tool;
    evidence.arguments = redactValue(discovery.representative.arguments, secrets);
    evidence.observable = redactValue(discovery.representative.observable, secrets);
  }
  if (invocation.stderr) evidence.diagnostics = safeDiagnostics(invocation.stderr, secrets);
  let parsed = null;
  try {
    parsed = parseInspectorOutput(invocation.stdout);
  } catch (error) {
    if (!failureClass) {
      evidence.status = "failed";
      evidence.failure_class = error instanceof DiagnosticError ? error.failureClass : FAILURE_CLASSES.unexpected;
    }
    return { evidence, parsed, tools: null, publicResult: null, serverIdentity: null };
  }
  let envelope;
  let result;
  try {
    ({ envelope, result } = resultEnvelope(parsed));
  } catch (error) {
    if (!failureClass) {
      evidence.status = "failed";
      evidence.failure_class = error instanceof DiagnosticError ? error.failureClass : FAILURE_CLASSES.unexpected;
    }
    return { evidence, parsed, tools: null, publicResult: null, serverIdentity: null };
  }
  const output = { evidence, parsed, tools: null, publicResult: null, serverIdentity: null };
  if (method === "initialize") {
    try {
      output.serverIdentity = identityFromResult(result);
      if (result.protocolVersion !== discovery.protocolVersion) throw unexpected("Inspector negotiated an unexpected protocol version");
      evidence.server_identity = redactValue(output.serverIdentity, secrets);
    } catch (error) {
      if (!failureClass) {
        evidence.status = "failed";
        evidence.failure_class = error instanceof DiagnosticError ? error.failureClass : FAILURE_CLASSES.unexpected;
      }
    }
  } else if (method === "tools/list") {
    try {
      const list = validateToolList(result, discovery);
      output.tools = list.tools;
      evidence.required_tool_presence = list.requiredPresence;
      const findings = Array.isArray(envelope.schemaFindings) ? envelope.schemaFindings : [];
      evidence.schema_findings = redactValue(findings, secrets);
      evidence.schema_warning_count = findings.filter((finding) => finding?.severity !== "error").length;
      evidence.schema_error_count = findings.filter((finding) => finding?.severity === "error").length;
      if (evidence.schema_error_count > 0 && !evidence.failure_class) {
        evidence.status = "failed";
        evidence.failure_class = FAILURE_CLASSES.schema;
      }
    } catch (error) {
      if (!failureClass) {
        evidence.status = "failed";
        evidence.failure_class = error instanceof DiagnosticError ? error.failureClass : FAILURE_CLASSES.unexpected;
      }
    }
  } else if (method === "tools/call") {
    try {
      const publicCall = parsePublicToolResult(result);
      output.publicResult = publicCall.publicResult;
      evidence.is_error = publicCall.isError;
      evidence.public_result = redactValue(publicCall.publicResult, secrets);
      evidence.observable_match = observableMatches(publicCall.publicResult, publicCall.isError, discovery.representative.observable);
      if (publicCall.isError || !evidence.observable_match) {
        evidence.status = "failed";
        evidence.failure_class = publicCall.isError ? FAILURE_CLASSES.tool : FAILURE_CLASSES.unexpected;
      }
    } catch (error) {
      if (!failureClass) {
        evidence.status = "failed";
        evidence.failure_class = error instanceof DiagnosticError ? error.failureClass : FAILURE_CLASSES.unexpected;
      }
    }
  }
  return output;
}

function notRunOperation(method, reason) {
  return { operation: method, status: "not_run", reason };
}

const SMOKE_OPERATIONS = ["initialize", "tools/list", "tools/call"];

function failureClassFor(error) {
  return error instanceof DiagnosticError ? error.failureClass : FAILURE_CLASSES.unexpected;
}

function notRunTransportEvidence(target, failureClass, discovery = null, reason = "common preflight failed", resetGeneration = null) {
  return {
    transport: target,
    status: "not_run",
    failure_class: failureClass,
    protocol_era: discovery?.protocolEra ?? "modern",
    protocol_version: discovery?.protocolVersion ?? MODERN_PROTOCOL_VERSION,
    server_identity: null,
    config_digest: null,
    operation_order: SMOKE_OPERATIONS,
    operations: SMOKE_OPERATIONS.map((method) => notRunOperation(method, reason)),
    fixture_generation: discovery?.fixtureGeneration ?? null,
    reset_generation: resetGeneration ?? discovery?.fixtureGeneration ?? null,
  };
}

function inspectorEvidence(info) {
  return info ? {
    package: info.package,
    version: info.version,
    node_version: info.nodeVersion,
    config_handoff: CONFIG_HANDOFF,
  } : null;
}

function smokeFailureOutput(info, target, validated, discovery, resetGeneration, error) {
  const failureClass = failureClassFor(error);
  const targets = target === "both" ? ["http", "stdio"] : [target];
  return {
    schema_version: 1,
    status: "failed",
    intent: "smoke",
    target,
    protocol_era: discovery?.protocolEra ?? "modern",
    protocol_version: discovery?.protocolVersion ?? MODERN_PROTOCOL_VERSION,
    inspector: inspectorEvidence(info),
    candidate: validated && discovery ? {
      source_revision: discovery.sourceRevision,
      runtime_profile: discovery.profile,
      scenario: validated.scenario,
      fixture_generation: discovery.fixtureGeneration,
      reset_generation: resetGeneration,
      proxy_artifact_version: validated.raw.evidence?.proxy_artifact_version ?? null,
    } : null,
    failure_class: failureClass,
    transports: Object.fromEntries(targets.map((name) => [name, notRunTransportEvidence(name, failureClass, discovery, "common preflight failed", resetGeneration)])),
    comparison: null,
  };
}

function failedTransportEvidence(target, discovery, error, resetGeneration) {
  const failureClass = failureClassFor(error);
  return {
    ...notRunTransportEvidence(target, failureClass, discovery, "transport setup failed", resetGeneration),
    status: "failed",
  };
}

async function runTransport({ target, info, descriptor, discovery, credential, runtimeRoot, spawnProcess }) {
  const stdio = target === "stdio" ? await resolveStdioExecutable(descriptor) : null;
  const config = buildInspectorConfig(target, discovery, credential.pat, stdio);
  const digest = configDigest(config, credential.secrets);
  const methods = SMOKE_OPERATIONS;
  const operations = [];
  let tools = null;
  let publicResult = null;
  let serverIdentity = null;
  let failureClass = null;
  for (const method of methods) {
    if (failureClass) {
      operations.push(notRunOperation(method, "previous operation failed"));
      continue;
    }
    const operation = await runOperation({
      info,
      descriptor,
      config,
      method,
      discovery,
      runtimeRoot,
      secrets: credential.secrets,
      spawnProcess,
    });
    operations.push(operation.evidence);
    failureClass = operation.evidence.failure_class;
    if (operation.tools) tools = operation.tools;
    if (operation.publicResult) publicResult = operation.publicResult;
    if (operation.serverIdentity) serverIdentity = operation.serverIdentity;
  }
  return {
    evidence: {
      transport: target,
      status: failureClass ? "failed" : "passed",
      failure_class: failureClass,
      protocol_era: discovery.protocolEra,
      protocol_version: discovery.protocolVersion,
      server_identity: serverIdentity,
      config_digest: digest,
      operation_order: methods,
      operations,
      fixture_generation: discovery.fixtureGeneration,
      reset_generation: discovery.fixtureGeneration,
    },
    tools,
    publicResult,
  };
}

export function compareTransports(http, stdio, discovery) {
  if (http.evidence.status !== "passed" || stdio.evidence.status !== "passed") {
    return { status: "not_run", reason: "transport operation failed", shared_tools: discovery.sharedTools, proxy_local_tools: discovery.proxyLocal.tools };
  }
  const details = [];
  for (const name of discovery.sharedTools) {
    const httpTool = toolByName(http.tools ?? [], name);
    const stdioTool = toolByName(stdio.tools ?? [], name);
    if (!httpTool || !stdioTool) {
      return { status: "failed", failure_class: FAILURE_CLASSES.unexpected, reason: "declared shared tool is missing", tool: name };
    }
    const allowed = new Set(discovery.proxyLocal.inputProperties[name] ?? []);
    for (const property of allowed) {
      if (!hasTopLevelSchemaProperty(stdioTool.inputSchema, property)
        || hasTopLevelSchemaProperty(httpTool.inputSchema, property)) {
        return {
          status: "failed",
          failure_class: FAILURE_CLASSES.schema,
          tools: [...details, { name, schema_match: false }],
          reason: "declared proxy-local input property is not stdio-only",
          tool: name,
          property,
        };
      }
    }
    const httpSchema = normalizeSchemaValue(httpTool.inputSchema ?? {}, new Set());
    const stdioSchema = normalizeSchemaValue(stdioTool.inputSchema ?? {}, allowed);
    const schemaMatch = stableStringify(httpSchema) === stableStringify(stdioSchema);
    details.push({ name, schema_match: schemaMatch });
    if (!schemaMatch) return { status: "failed", failure_class: FAILURE_CLASSES.schema, tools: details, reason: "shared tool input schemas disagree" };
  }
  const semanticMatch = stableStringify(http.publicResult) === stableStringify(stdio.publicResult);
  if (!semanticMatch) {
    return { status: "failed", failure_class: FAILURE_CLASSES.unexpected, tools: details, reason: "shared representative public results disagree" };
  }
  return {
    status: "passed",
    tools: details,
    semantic_result_match: true,
    proxy_local_tools: discovery.proxyLocal.tools,
  };
}

export async function runSmoke({ info, descriptor, target, fetchImpl = globalThis.fetch, spawnProcess = nodeSpawn } = {}) {
  const validated = validateDescriptor(descriptor, target);
  let discovery = null;
  let resetGeneration = null;
  let credential = null;
  try {
    await checkReady(validated, fetchImpl);
    const initialDiscovery = await requestJson(validated.services.fixture.discovery.url, { fetchImpl, purpose: "runtime discovery" });
    const initialContract = validateRuntimeDiscovery(validated, initialDiscovery, target);
    resetGeneration = await resetFixture(validated, fetchImpl);
    await checkReady(validated, fetchImpl);
    const postResetDiscovery = await requestJson(validated.services.fixture.discovery.url, { fetchImpl, purpose: "runtime discovery" });
    discovery = validateRuntimeDiscovery(validated, postResetDiscovery, target);
    if (initialContract.sourceRevision !== discovery.sourceRevision || resetGeneration !== discovery.fixtureGeneration) {
      throw configuration("fixture reset changed the candidate or did not report the same generation");
    }
    credential = await resolveCredential(validated, discovery, fetchImpl);
  } catch (error) {
    return smokeFailureOutput(info, target, validated, discovery, resetGeneration, error);
  }
  const runtimeRoot = await mkdtemp(join(tmpdir(), "akb-mcp-inspector-"));
  try {
    const targets = target === "both" ? ["http", "stdio"] : [target];
    const transports = {};
    for (const transport of targets) {
      try {
        transports[transport] = await runTransport({
          target: transport,
          info,
          descriptor: validated,
          discovery,
          credential,
          runtimeRoot,
          spawnProcess,
        });
      } catch (error) {
        transports[transport] = {
          evidence: failedTransportEvidence(transport, discovery, error, resetGeneration),
          tools: null,
          publicResult: null,
        };
      }
    }
    const comparison = target === "both" ? compareTransports(transports.http, transports.stdio, discovery) : null;
    const transportFailures = Object.values(transports).map((item) => item.evidence.failure_class).filter(Boolean);
    if (comparison?.failure_class) transportFailures.push(comparison.failure_class);
    const uniqueFailures = [...new Set(transportFailures)];
    const failureClass = uniqueFailures.length === 0
      ? null
      : uniqueFailures.length === 1 ? uniqueFailures[0] : FAILURE_CLASSES.unexpected;
    return {
      schema_version: 1,
      status: failureClass ? "failed" : "passed",
      intent: "smoke",
      target,
      protocol_era: discovery.protocolEra,
      protocol_version: discovery.protocolVersion,
      inspector: inspectorEvidence(info),
      candidate: {
        source_revision: discovery.sourceRevision,
        runtime_profile: discovery.profile,
        scenario: validated.scenario,
        fixture_generation: discovery.fixtureGeneration,
        reset_generation: resetGeneration,
        proxy_artifact_version: validated.raw.evidence?.proxy_artifact_version ?? null,
      },
      failure_class: failureClass,
      transports: Object.fromEntries(Object.entries(transports).map(([name, item]) => [name, item.evidence])),
      comparison,
    };
  } finally {
    await rm(runtimeRoot, { recursive: true, force: true });
  }
}

export async function runInteractive({ info, descriptor, target, fetchImpl = globalThis.fetch, spawnProcess = nodeSpawn } = {}) {
  const validated = validateDescriptor(descriptor, target);
  await checkReady(validated, fetchImpl);
  const discoveryPayload = await requestJson(validated.services.fixture.discovery.url, { fetchImpl, purpose: "runtime discovery" });
  const discovery = validateRuntimeDiscovery(validated, discoveryPayload, target);
  const credential = await resolveCredential(validated, discovery, fetchImpl);
  const stdio = target === "stdio" ? await resolveStdioExecutable(validated) : null;
  const config = buildInspectorConfig(target, discovery, credential.pat, stdio);
  const runtimeRoot = await mkdtemp(join(tmpdir(), "akb-mcp-inspector-web-"));
  try {
    const result = await runInspectorInvocation({
      info,
      descriptor: validated,
      config,
      method: "interactive",
      representative: discovery.representative,
      runtimeRoot,
      spawnProcess,
      interactive: true,
    });
    return result.code;
  } finally {
    await rm(runtimeRoot, { recursive: true, force: true });
  }
}

function usageText() {
  return `Usage: npm run --prefix tools/mcp-inspector inspect -- --intent <interactive|smoke> --target <http|stdio|both> --descriptor <path|->\n\nSmoke runs initialize, tools/list --strict --format json, and the discovery-declared read-only tools/call.\nInteractive opens the Inspector Web client on loopback with its normal launch/session authentication.\n`;
}

function outputFailure(error, args, info = null) {
  const failureClass = error instanceof DiagnosticError ? error.failureClass : FAILURE_CLASSES.unexpected;
  const messages = {
    [FAILURE_CLASSES.unexpected]: "the consumer smoke failed unexpectedly",
    [FAILURE_CLASSES.usage]: "the command or runtime descriptor is invalid",
    [FAILURE_CLASSES.authentication]: "the runtime credential was rejected",
    [FAILURE_CLASSES.unreachable]: "the runtime endpoint is unavailable",
    [FAILURE_CLASSES.tool]: "the representative tool returned an error",
    [FAILURE_CLASSES.schema]: "the tool catalog is not portable",
  };
  const output = {
    schema_version: 1,
    status: "failed",
    intent: args?.intent ?? null,
    target: args?.target ?? null,
    protocol_era: "modern",
    protocol_version: MODERN_PROTOCOL_VERSION,
    inspector: inspectorEvidence(info),
    failure_class: failureClass,
    message: messages[failureClass] ?? messages[FAILURE_CLASSES.unexpected],
  };
  process.stdout.write(`${JSON.stringify(output)}\n`);
  process.stderr.write(`mcp-inspector consumer smoke failed: ${failureClass}\n`);
  return FAILURE_EXIT_CODES[failureClass] ?? FAILURE_EXIT_CODES[FAILURE_CLASSES.unexpected];
}

export async function main(argv = process.argv.slice(2)) {
  let args = null;
  let info = null;
  try {
    args = parseArguments(argv);
    if (args.help) {
      process.stdout.write(usageText());
      return 0;
    }
    info = await inspectInstallation();
    const descriptor = await loadDescriptor(args.descriptor);
    if (args.intent === "interactive") {
      return await runInteractive({ info, descriptor, target: args.target });
    }
    const output = await runSmoke({ info, descriptor, target: args.target });
    process.stdout.write(`${JSON.stringify(output)}\n`);
    if (output.failure_class) {
      process.stderr.write(`mcp-inspector consumer smoke failed: ${output.failure_class}\n`);
      return FAILURE_EXIT_CODES[output.failure_class] ?? FAILURE_EXIT_CODES[FAILURE_CLASSES.unexpected];
    }
    return 0;
  } catch (error) {
    return outputFailure(error, args, info);
  }
}

if (process.argv[1] && resolve(fileURLToPath(import.meta.url)) === resolve(process.argv[1])) {
  main().then((code) => { process.exitCode = code; });
}
