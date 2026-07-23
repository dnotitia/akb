#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import {
  copyFile,
  mkdtemp,
  readFile,
  readdir,
  rm,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const packageRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const proofRoot = await mkdtemp(join(tmpdir(), "akb-m3-packed-"));

try {
  const packDir = join(proofRoot, "pack");
  const consumerDir = join(proofRoot, "consumer");
  run("mkdir", ["-p", packDir, consumerDir], proofRoot);
  run("pnpm", ["pack", "--pack-destination", packDir], packageRoot);
  const tarballName = (await readdir(packDir)).find((name) => name.endsWith(".tgz"));
  if (!tarballName) throw new Error("pnpm pack did not create a tarball.");
  const tarball = join(packDir, tarballName);

  const listing = run("tar", ["-tf", tarball], proofRoot).stdout.trim().split("\n");
  for (const required of [
    "package/dist/index.js",
    "package/dist/index.d.ts",
    "package/dist/lite.js",
    "package/dist/lite.d.ts",
    "package/scripts/m3-sdk-contract.json",
    "package/scripts/m3-packed-runtime.mjs",
    "package/scripts/m3-packed-types.ts",
  ]) {
    if (!listing.includes(required)) throw new Error(`packed artifact missing ${required}`);
  }
  const leaked = listing.find(
    (path) => path.startsWith("package/src/") || path.startsWith("package/test/"),
  );
  if (leaked) throw new Error(`packed artifact leaked repository source: ${leaked}`);

  await writeFile(
    join(consumerDir, "package.json"),
    JSON.stringify({ private: true, type: "module" }, null, 2),
  );
  run(
    "npm",
    ["install", "--ignore-scripts", "--no-audit", "--no-fund", tarball],
    consumerDir,
  );

  const installedScripts = join(consumerDir, "node_modules", "@akb", "client", "scripts");
  const installedContract = JSON.parse(
    await readFile(join(installedScripts, "m3-sdk-contract.json"), "utf8"),
  );
  if (installedContract.operations.length !== 20) {
    throw new Error(`installed matrix has ${installedContract.operations.length} operations`);
  }
  for (const item of installedContract.operations) {
    if (!item.packedProof) throw new Error(`${item.operationId}: missing packed proof mapping`);
  }

  const runtimeProof = join(consumerDir, "m3-packed-runtime.mjs");
  const typeProof = join(consumerDir, "m3-packed-types.ts");
  await copyFile(join(installedScripts, "m3-packed-runtime.mjs"), runtimeProof);
  await copyFile(join(installedScripts, "m3-packed-types.ts"), typeProof);
  const tsc = join(packageRoot, "node_modules", ".bin", "tsc");
  run(
    tsc,
    [
      "--noEmit",
      "--strict",
      "--skipLibCheck",
      "--target",
      "ES2022",
      "--module",
      "NodeNext",
      "--moduleResolution",
      "NodeNext",
      typeProof,
    ],
    consumerDir,
  );
  const runtime = run(process.execPath, [runtimeProof], consumerDir);

  console.log(JSON.stringify({
    result: "passed",
    source_blind: true,
    package_manager: "pnpm pack",
    operations: installedContract.operations.length,
    packed_files: listing.length,
    main_lite_typecheck: "passed",
    runtime: runtime.stdout.trim(),
  }, null, 2));
} finally {
  await rm(proofRoot, { recursive: true, force: true });
}

function run(command, args, cwd) {
  const result = spawnSync(command, args, {
    cwd,
    encoding: "utf8",
    env: { ...process.env, npm_config_update_notifier: "false" },
  });
  if (result.status !== 0) {
    throw new Error(
      `${command} ${args.join(" ")} failed (${result.status})\n${result.stdout}\n${result.stderr}`,
    );
  }
  return result;
}
