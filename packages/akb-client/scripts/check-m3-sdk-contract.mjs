#!/usr/bin/env node
import { readFile } from "node:fs/promises";

import { createClient } from "../dist/index.js";
import { createClient as createLiteClient } from "../dist/lite.js";

const CONTRACT_URL = new URL("./m3-sdk-contract.json", import.meta.url);
const OPENAPI_URL = new URL("../test/fixtures/openapi.core.json", import.meta.url);
const EXPECTED_OPERATION_IDS = [
  "graphNeighbors",
  "graphOverview",
  "graphHealth",
  "graphRelations",
  "graphLink",
  "graphUnlink",
  "graphProvenance",
  "activityList",
  "activityRecent",
  "documentsHistory",
  "documentsDiff",
  "collectionsCreateCollection",
  "collectionsDeleteCollection",
  "tablesGetVault",
  "tablesGetVaultSchema",
  "tablesGetTableSchema",
  "tablesPostVault",
  "tablesAlterTable",
  "tablesApplyMigration",
  "tablesDeleteTableName",
];

const contract = JSON.parse(await readFile(CONTRACT_URL, "utf8"));
const openapi = JSON.parse(await readFile(OPENAPI_URL, "utf8"));
const matrix = contract.operations;
const byId = new Map(matrix.map((item) => [item.operationId, item]));

for (const operationId of EXPECTED_OPERATION_IDS) {
  assert(byId.has(operationId), operationId, "missing from M3 SDK coverage matrix");
}
for (const operationId of byId.keys()) {
  assert(EXPECTED_OPERATION_IDS.includes(operationId), operationId, "is not part of the selected M3 SDK surface");
}
assert(byId.size === EXPECTED_OPERATION_IDS.length, "matrix", "contains duplicate operation IDs");
assert(openapi.components?.schemas?.[contract.errorSchema], contract.errorSchema, "missing error schema component");

const clients = [
  [".", createClient({ baseUrl: "https://contract.invalid/api/v1", defaultVault: "proof" })],
  ["./lite", createLiteClient({ baseUrl: "https://contract.invalid/api/v1", defaultVault: "proof" })],
];

for (const item of matrix) {
  const operation = openapi.paths?.[item.path]?.[item.method];
  assert(operation, item.operationId, `missing fixture operation ${item.method.toUpperCase()} ${item.path}`);
  assert(operation.operationId === item.operationId, item.operationId, `fixture operationId is ${operation.operationId}`);

  const success = ["200", "201", "202"]
    .map((status) => operation.responses?.[status])
    .find(Boolean);
  const successRef = success?.content?.["application/json"]?.schema?.$ref;
  assert(
    successRef === `#/components/schemas/${item.successSchema}`,
    item.operationId,
    `success schema is ${successRef ?? "missing"}`,
  );

  const requestSchema = operation.requestBody?.content?.["application/json"]?.schema;
  if (item.requestSchema === "never") {
    assert(operation.requestBody === undefined, item.operationId, "bodyless operation has requestBody");
  } else if (item.requestSchema === "TableMigrationOperation[]") {
    assert(operation.requestBody?.required === true, item.operationId, "migration request body is not required");
    assert(requestSchema?.type === "array", item.operationId, "migration request body is not an array");
    assert(
      requestSchema?.items?.discriminator?.propertyName === "op",
      item.operationId,
      "migration request body has no op discriminator",
    );
  } else {
    assert(operation.requestBody?.required === true, item.operationId, "request body is not required");
    assert(
      requestSchema?.$ref === `#/components/schemas/${item.requestSchema}`,
      item.operationId,
      `request schema is ${requestSchema?.$ref ?? "missing"}`,
    );
  }

  const headers = new Set(
    (operation.parameters ?? [])
      .filter((parameter) => parameter.in === "header" && parameter.required === true)
      .map((parameter) => parameter.name),
  );
  assert(
    sameSet(headers, new Set(item.requiredHeaders ?? [])),
    item.operationId,
    `required headers are ${[...headers].join(", ") || "empty"}`,
  );
  assert(item.packedProof, item.operationId, "missing packed consumer proof mapping");
  assert(item.rawPrefix?.startsWith("/"), item.operationId, "missing raw request prefix");

  for (const [entrypoint, client] of clients) {
    assert(item.entrypoints.includes(entrypoint), item.operationId, `missing ${entrypoint} export proof`);
    assert(
      typeof client[item.facade.namespace]?.[item.facade.method] === "function",
      item.operationId,
      `${entrypoint} does not expose ${item.facade.namespace}.${item.facade.method}`,
    );
  }
}

console.log(`M3 SDK contract matrix passed for ${matrix.length} operations.`);

function assert(condition, operationId, message) {
  if (!condition) throw new Error(`${operationId}: ${message}`);
}

function sameSet(left, right) {
  return left.size === right.size && [...left].every((value) => right.has(value));
}
