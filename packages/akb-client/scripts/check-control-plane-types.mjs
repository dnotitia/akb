#!/usr/bin/env node
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(fileURLToPath(new URL("..", import.meta.url)));
const fixture = JSON.parse(await readFile(resolve(root, "test/fixtures/openapi.control-plane.json"), "utf8"));
const contract = JSON.parse(await readFile(resolve(root, "scripts/sdk-surface-contract.json"), "utf8"));
const generated = await readFile(resolve(root, "src/control-plane.gen.ts"), "utf8");
const facade = await readFile(resolve(root, "src/control-plane.ts"), "utf8");

if (/\bany\b/u.test(generated) || /\bany\b/u.test(facade)) {
  throw new Error("control-plane generated types and facade must not contain any");
}

const operations = [];
for (const [path, pathItem] of Object.entries(fixture.paths ?? {})) {
  for (const [method, operation] of Object.entries(pathItem ?? {})) {
    if (!["get", "post", "patch", "put", "delete"].includes(method)) continue;
    operations.push({ path, method, ...operation });
  }
}
// Legacy adoption is intentionally a REST-only operator surface for this
// release.  Keep it in the live OpenAPI fixture without inventing a generated
// SDK facade, while continuing to require a complete typed matrix for the
// public control-plane SDK operations.
const sdkOperations = operations.filter(
  (operation) => !operation.tags?.includes("app-legacy-adoptions"),
);
const matrix = contract.controlPlane;
if (!Array.isArray(matrix) || matrix.length !== sdkOperations.length) {
  throw new Error(`control-plane matrix has ${matrix?.length ?? 0} entries; fixture has ${sdkOperations.length} SDK operations`);
}
const operationIds = new Set(sdkOperations.map((operation) => operation.operationId));
const matrixIds = new Set(matrix.map((operation) => operation.operationId));
if (matrixIds.size !== matrix.length || matrixIds.size !== operationIds.size) {
  throw new Error("control-plane operation IDs are not unique");
}
for (const operation of sdkOperations) {
  const item = matrix.find((candidate) => candidate.operationId === operation.operationId);
  if (!item || item.path !== operation.path || item.method !== operation.method) {
    throw new Error(`${operation.operationId}: contract matrix drifted from fixture`);
  }
  const success = ["200", "201", "202"].map((status) => operation.responses?.[status]).find(Boolean);
  const successSchema = success?.content?.["application/json"]?.schema?.$ref?.split("/").at(-1);
  if (successSchema !== item.successSchema) throw new Error(`${operation.operationId}: success schema drift`);
  const requestBodySchema = operation.requestBody?.content?.["application/json"]?.schema;
  const requestSchema = (
    requestBodySchema?.$ref
    ?? requestBodySchema?.anyOf?.find((candidate) => candidate?.$ref)?.$ref
  )?.split("/").at(-1) ?? "never";
  if (requestSchema !== item.requestSchema) throw new Error(`${operation.operationId}: request schema drift`);
  if (!generated.includes(`  ${operation.operationId}: ControlPlaneOperation`)) {
    throw new Error(`${operation.operationId}: missing generated operation type`);
  }
  if (item.entrypoints?.join(",") !== "./control-plane") {
    throw new Error(`${operation.operationId}: control-plane entrypoint proof missing`);
  }
}

const publicNames = Object.keys(await import("../dist/control-plane.js"));
for (const name of ["createControlPlaneAdminClient", "createControlPlaneAppClient", "exchangeAppCredential"]) {
  if (!publicNames.includes(name)) throw new Error(`${name}: missing public control-plane export`);
}
for (const forbidden of ["createClient", "AkbClient", "akbFetch", "unwrapAkbResponse"]) {
  if (publicNames.includes(forbidden)) throw new Error(`${forbidden}: leaked data-plane export`);
}

console.log(
  `Control-plane generated contract passed for ${sdkOperations.length} SDK operations (${operations.length - sdkOperations.length} REST-only operations omitted).`,
);
