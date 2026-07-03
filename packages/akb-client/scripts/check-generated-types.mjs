#!/usr/bin/env node
import { readFile, writeFile } from "node:fs/promises";
import process from "node:process";

import { generateAkbSchema } from "../dist/schema-codegen.js";

const DEFAULT_SCHEMA = new URL("../test/fixtures/schema.eng.json", import.meta.url);
const DEFAULT_TYPES = new URL("../test/fixtures/akb.types.ts", import.meta.url);

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
  const schema = JSON.parse(await readFile(options.schema, "utf8"));
  const generated = generateAkbSchema(schema);
  if (options.write) {
    await writeFile(options.types, generated);
    return;
  }
  const expected = await readFile(options.types, "utf8");
  if (normalize(generated) === normalize(expected)) {
    return;
  }
  const diff = firstDiffLine(expected, generated);
  console.error("Generated AKB types drifted from the committed snapshot.");
  console.error(`Snapshot: ${options.types}`);
  console.error(`First differing line: ${diff}`);
  console.error("Run: node scripts/check-generated-types.mjs --write");
  process.exitCode = 1;
}

/**
 * @param {string[]} args
 * @returns {{ schema: URL | string, types: URL | string, write: boolean }}
 */
function parseOptions(args) {
  /** @type {{ schema: URL | string, types: URL | string, write: boolean }} */
  const options = { schema: DEFAULT_SCHEMA, types: DEFAULT_TYPES, write: false };
  for (let i = 0; i < args.length; i += 1) {
    const arg = args[i];
    if (arg === "--schema") options.schema = requireValue(args, ++i, arg);
    else if (arg === "--types") options.types = requireValue(args, ++i, arg);
    else if (arg === "--write") options.write = true;
    else throw new Error(`Unknown option: ${arg}`);
  }
  return options;
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
