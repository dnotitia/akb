#!/usr/bin/env node
// Each template in akb/login replaces one of Keycloak's. When Keycloak
// changes the original, the copy has to be looked at again: both reference
// themes this one learned from had silently dropped a script Keycloak 26
// added. upstream/keycloak-<version>.json records the sha256 of every
// original this theme replaces, read from the pinned image itself.
//
//   node keycloak-theme/scripts/upstream-hashes.mjs           # check (needs docker)
//   node keycloak-theme/scripts/upstream-hashes.mjs --write   # re-record after review
//
// A check failure names the templates whose original moved. Diff the old and
// new original, carry the change into the copy, then --write.
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { inflateRawSync } from "node:zlib";

const here = dirname(fileURLToPath(import.meta.url));
export const THEME = resolve(here, "../akb/login");
export const RECORD = resolve(here, "../upstream/keycloak-26.7.0.json");

/** Entries of a zip archive, by name. Enough of the format for a jar. */
function unzip(buffer) {
  let end = buffer.length - 22;
  while (end >= 0 && buffer.readUInt32LE(end) !== 0x06054b50) end -= 1;
  if (end < 0) throw new Error("not a zip archive");
  const count = buffer.readUInt16LE(end + 10);
  let offset = buffer.readUInt32LE(end + 16);
  const entries = new Map();
  for (let i = 0; i < count; i += 1) {
    if (buffer.readUInt32LE(offset) !== 0x02014b50) throw new Error("bad central directory");
    const method = buffer.readUInt16LE(offset + 10);
    const size = buffer.readUInt32LE(offset + 20);
    const nameLength = buffer.readUInt16LE(offset + 28);
    const extraLength = buffer.readUInt16LE(offset + 30);
    const commentLength = buffer.readUInt16LE(offset + 32);
    const local = buffer.readUInt32LE(offset + 42);
    const name = buffer.toString("utf8", offset + 46, offset + 46 + nameLength);
    entries.set(name, () => {
      const start = local + 30 + buffer.readUInt16LE(local + 26) + buffer.readUInt16LE(local + 28);
      const data = buffer.subarray(start, start + size);
      return method === 8 ? inflateRawSync(data) : data;
    });
    offset += 46 + nameLength + extraLength + commentLength;
  }
  return entries;
}

function sha256(buffer) {
  return createHash("sha256").update(buffer).digest("hex");
}

/** The templates this theme replaces: every .ftl in akb/login. */
export function overriddenTemplates() {
  return readdirSync(THEME)
    .filter((name) => name.endsWith(".ftl"))
    .sort();
}

/** sha256 of each replaced original, read from the image's themes jar. */
export function measure(record) {
  const result = spawnSync("docker", ["run", "--rm", "--entrypoint", "cat", record.image, record.jar], {
    maxBuffer: 256 * 1024 * 1024,
  });
  if (result.status !== 0) throw new Error(`docker could not read ${record.jar}: ${result.stderr}`);
  const jar = unzip(result.stdout);
  const measured = {};
  for (const name of overriddenTemplates()) {
    // keycloak.v2 inherits from base: the original is whichever one
    // Keycloak would have served.
    const upstream = [`theme/keycloak.v2/login/${name}`, `theme/base/login/${name}`].find((path) => jar.has(path));
    if (!upstream) throw new Error(`${name} has no original in ${record.jar}`);
    measured[name] = { upstream, sha256: sha256(jar.get(upstream)()) };
  }
  return measured;
}

const isMain = process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  const record = JSON.parse(readFileSync(RECORD, "utf8"));
  const measured = measure(record);
  if (process.argv.includes("--write")) {
    writeFileSync(RECORD, `${JSON.stringify({ ...record, overrides: measured }, null, 2)}\n`);
    console.log(`recorded ${Object.keys(measured).length} originals from ${record.image}`);
  } else {
    const moved = Object.keys(measured).filter(
      (name) => JSON.stringify(measured[name]) !== JSON.stringify(record.overrides?.[name]),
    );
    const gone = Object.keys(record.overrides ?? {}).filter((name) => !(name in measured));
    if (moved.length || gone.length) {
      for (const name of moved) console.error(`changed or unrecorded: ${name} (${measured[name].upstream})`);
      for (const name of gone) console.error(`recorded but no longer overridden: ${name}`);
      console.error("Review each original against its copy, then run with --write.");
      process.exit(1);
    }
    console.log(`${Object.keys(measured).length} originals match ${record.image}`);
  }
}
