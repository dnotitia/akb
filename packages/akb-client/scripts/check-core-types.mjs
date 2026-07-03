#!/usr/bin/env node
import { readFile, writeFile } from "node:fs/promises";
import process from "node:process";

import { generateCoreTypes } from "../dist/core/openapi-codegen.js";

const DEFAULT_OPENAPI = new URL("../test/fixtures/openapi.core.json", import.meta.url);
const DEFAULT_TYPES = new URL("../src/core/schema.gen.ts", import.meta.url);

main(process.argv.slice(2)).catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
});

/**
 * @param {string[]} args
 * @returns {Promise<void>}
 */
async function main(args) {
  const options = parseOptions(args);
  const spec = JSON.parse(await readFile(options.openapi, "utf8"));
  const generated = generateCoreTypes(spec);
  assertNoAny(generated);

  if (options.write) {
    await writeFile(options.types, generated);
    return;
  }

  const expected = await readFile(options.types, "utf8");
  if (normalize(generated) === normalize(expected)) return;

  console.error("Generated AKB core OpenAPI types drifted from the committed snapshot.");
  console.error(`Snapshot: ${options.types}`);
  console.error(`First differing line: ${firstDiffLine(expected, generated)}`);
  console.error("Run: node scripts/check-core-types.mjs --write");
  process.exitCode = 1;
}

/**
 * @param {string[]} args
 * @returns {{ openapi: URL | string, types: URL | string, write: boolean }}
 */
function parseOptions(args) {
  /** @type {{ openapi: URL | string, types: URL | string, write: boolean }} */
  const options = { openapi: DEFAULT_OPENAPI, types: DEFAULT_TYPES, write: false };
  for (let i = 0; i < args.length; i += 1) {
    const arg = args[i];
    if (arg === "--openapi") options.openapi = requireValue(args, ++i, arg);
    else if (arg === "--types") options.types = requireValue(args, ++i, arg);
    else if (arg === "--write") options.write = true;
    else throw new Error(`Unknown option: ${arg}`);
  }
  return options;
}

/**
 * @param {string} value
 * @returns {void}
 */
function assertNoAny(value) {
  if (/\bany\b/.test(value)) {
    throw new Error("Generated core OpenAPI types must not contain `any`.");
  }
}

/**
 * @param {string[]} args
 * @param {number} index
 * @param {string} option
 * @returns {string}
 */
function requireValue(args, index, option) {
  const value = args[index];
  if (!value || value.startsWith("--")) {
    throw new Error(`${option} requires a value.`);
  }
  return value;
}

/**
 * @param {string} expected
 * @param {string} generated
 * @returns {number}
 */
function firstDiffLine(expected, generated) {
  const expectedLines = normalize(expected).split("\n");
  const generatedLines = normalize(generated).split("\n");
  const limit = Math.max(expectedLines.length, generatedLines.length);
  for (let i = 0; i < limit; i += 1) {
    if (expectedLines[i] !== generatedLines[i]) return i + 1;
  }
  return limit;
}

/**
 * @param {string} value
 * @returns {string}
 */
function normalize(value) {
  return value.replace(/\r\n/g, "\n").replace(/\s+$/u, "");
}
