import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { test } from "vitest";

import { generateAkbSchema } from "../src/schema-codegen.js";

const packageRoot = new URL("..", import.meta.url);
const fixtureSchema = new URL("fixtures/schema.eng.json", import.meta.url);
const fixtureTypes = new URL("fixtures/akb.types.ts", import.meta.url);
const guardScript = new URL("../scripts/check-generated-types.mjs", import.meta.url);

test("generateAkbSchema emits Supabase Row Insert Update types", () => {
  const schema = JSON.parse(readFileSync(fixtureSchema, "utf8"));
  const expected = readFileSync(fixtureTypes, "utf8");

  assert.equal(generateAkbSchema(schema), expected);
  assert.ok(expected.includes('"status": "todo" | "done";'));
  assert.ok(expected.includes('"title": string;'));
  assert.ok(expected.includes('"status"?: "todo" | "done";'));
  assert.ok(expected.includes('"owner_id"?: AkbRef<string, "users.id"> | null;'));
  assert.ok(expected.includes('referencedRelation: "users";'));
});

test("generated type drift guard passes for the committed snapshot", () => {
  const result = spawnSync(
    process.execPath,
    [guardScript.pathname],
    { cwd: packageRoot, encoding: "utf8" },
  );

  assert.equal(result.status, 0, result.stderr);
});

test("generated type drift guard fails on stale output", async () => {
  const dir = await mkdtemp(join(tmpdir(), "akb-codegen-"));
  try {
    const staleTypes = join(dir, "akb.types.ts");
    await writeFile(staleTypes, "export interface Database {}\n");
    const result = spawnSync(
      process.execPath,
      [guardScript.pathname, "--schema", fixtureSchema.pathname, "--types", staleTypes],
      { cwd: packageRoot, encoding: "utf8" },
    );

    assert.equal(result.status, 1);
    assert.match(result.stderr, /Generated AKB types drifted/);
  } finally {
    await rm(dir, { force: true, recursive: true });
  }
});
