#!/usr/bin/env node
import { readFile, writeFile } from "node:fs/promises";
import process from "node:process";

import { generateAkbSchema } from "../src/schema-codegen.js";

main(process.argv.slice(2)).catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
});

/**
 * @param {string[]} args
 * @returns {Promise<void>}
 */
async function main(args) {
  if (args.includes("--help") || args.includes("-h")) {
    printHelp();
    return;
  }
  const [command, subcommand, ...rest] = args;
  if (command !== "gen" || subcommand !== "types") {
    printHelp();
    process.exitCode = 1;
    return;
  }

  const options = parseOptions(rest);
  const schema = options.schemaFile
    ? JSON.parse(await readInput(options.schemaFile))
    : await fetchVaultSchema(options);
  const output = generateAkbSchema(schema);

  if (options.check) {
    const expected = await readFile(options.check, "utf8");
    if (normalize(output) !== normalize(expected)) {
      console.error(`Generated AKB types differ from ${options.check}.`);
      console.error("Regenerate with: akb gen types --vault <vault> > akb.types.ts");
      process.exitCode = 1;
    }
    return;
  }

  if (options.output) {
    await writeFile(options.output, output);
    return;
  }
  process.stdout.write(output);
}

/**
 * @typedef {{
 *   vault: string | null,
 *   url: string,
 *   token: string | null,
 *   schemaFile: string | null,
 *   output: string | null,
 *   check: string | null,
 * }} CliOptions
 */

/**
 * @param {string[]} args
 * @returns {CliOptions}
 */
function parseOptions(args) {
  /** @type {CliOptions} */
  const options = {
    vault: null,
    url: process.env.AKB_URL ?? "http://localhost:8000",
    token: process.env.AKB_TOKEN ?? null,
    schemaFile: null,
    output: null,
    check: null,
  };

  for (let i = 0; i < args.length; i += 1) {
    const arg = args[i];
    if (arg === "--vault") options.vault = requireValue(args, ++i, arg);
    else if (arg === "--url" || arg === "--base-url") options.url = requireValue(args, ++i, arg);
    else if (arg === "--token") options.token = requireValue(args, ++i, arg);
    else if (arg === "--schema-file") options.schemaFile = requireValue(args, ++i, arg);
    else if (arg === "--output" || arg === "-o") options.output = requireValue(args, ++i, arg);
    else if (arg === "--check") options.check = requireValue(args, ++i, arg);
    else throw new Error(`Unknown option: ${arg}`);
  }

  if (!options.schemaFile && !options.vault) {
    throw new Error("Missing --vault. Use --schema-file to generate from a saved introspection JSON file.");
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
 * @param {CliOptions} options
 * @returns {Promise<unknown>}
 */
async function fetchVaultSchema(options) {
  if (!options.vault) throw new Error("Missing vault.");
  if (typeof fetch !== "function") {
    throw new Error("Global fetch is required. Use Node.js 18+ or --schema-file.");
  }
  const response = await fetch(`${apiBaseUrl(options.url)}/tables/${encodeURIComponent(options.vault)}/schema`, {
    headers: options.token ? { authorization: `Bearer ${options.token}` } : {},
  });
  const text = await response.text();
  if (!response.ok) {
    throw new Error(`Schema introspection failed (${response.status}): ${text}`);
  }
  return JSON.parse(text);
}

/**
 * @param {string} file
 * @returns {Promise<string>}
 */
async function readInput(file) {
  if (file === "-") {
    const chunks = [];
    for await (const chunk of process.stdin) chunks.push(chunk);
    return Buffer.concat(chunks).toString("utf8");
  }
  return await readFile(file, "utf8");
}

/**
 * @param {string} url
 * @returns {string}
 */
function apiBaseUrl(url) {
  const trimmed = url.replace(/\/+$/, "");
  return trimmed.endsWith("/api/v1") ? trimmed : `${trimmed}/api/v1`;
}

/**
 * @param {string} value
 * @returns {string}
 */
function normalize(value) {
  return value.replace(/\r\n/g, "\n").replace(/\s+$/u, "");
}

function printHelp() {
  console.log(`Usage:
  akb gen types --vault <vault> [--url <akb-url>] [--token <token>]
  akb gen types --schema-file schema.json [--output akb.types.ts]

Options:
  --vault <name>         Vault to introspect via /api/v1/tables/{vault}/schema
  --url <url>            AKB base URL or /api/v1 URL (default: AKB_URL or localhost)
  --token <token>        Bearer token (default: AKB_TOKEN)
  --schema-file <path>   Generate from saved introspection JSON instead of fetching
  --output, -o <path>    Write generated types to a file
  --check <path>         Fail if generated output differs from an existing file`);
}
