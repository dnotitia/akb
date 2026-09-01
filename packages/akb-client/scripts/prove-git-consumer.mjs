#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { access, copyFile, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

const repositoryUrl = normalizeRepositoryUrl(
  process.env.AKB_GIT_REPOSITORY
    ?? (process.env.GITHUB_REPOSITORY ? `https://github.com/${process.env.GITHUB_REPOSITORY}.git` : ""),
);
const commit = requireFullCommit(process.env.AKB_GIT_SHA ?? process.env.GITHUB_SHA ?? "");
const subdirectory = process.env.AKB_GIT_SUBDIRECTORY ?? "packages/akb-client";
validateSubdirectory(subdirectory);

const gitSpecifier = `${repositoryUrl}#${commit}&path:/${subdirectory}`;
const repositoryPath = new URL(repositoryUrl).pathname.replace(/^\//u, "").replace(/\.git$/u, "");
const tarballResolution = `https://codeload.github.com/${repositoryPath}/tar.gz/${commit}`;
const buildApprovalKey = `@akb/client@${tarballResolution}#path:/${subdirectory}`;
const packageManager = "pnpm";
const packageManagerVersion = "11.10.0";
const proofRoot = await mkdtemp(join(tmpdir(), "akb-client-git-consumer-"));

try {
  const consumerPackage = join(proofRoot, "package.json");
  const workspaceConfig = join(proofRoot, "pnpm-workspace.yaml");
  const runtimeProof = join(proofRoot, "runtime-proof.mjs");
  const typeProof = join(proofRoot, "type-proof.ts");

  await writeFile(
    consumerPackage,
    `${JSON.stringify({
      name: "akb-client-git-consumer",
      private: true,
      type: "module",
      packageManager: `${packageManager}@${packageManagerVersion}`,
      engines: { node: ">=22" },
      dependencies: { "@akb/client": gitSpecifier },
      devDependencies: { typescript: "5.9.3" },
    }, null, 2)}\n`,
  );
  await writeFile(
    workspaceConfig,
    `allowBuilds:\n  ${JSON.stringify(buildApprovalKey)}: true\n`,
  );

  const version = run(packageManager, ["--version"], proofRoot).stdout.trim();
  if (version !== packageManagerVersion) {
    throw new Error(`Expected ${packageManager}@${packageManagerVersion}, got ${packageManager}@${version}.`);
  }

  run(packageManager, ["install", "--reporter=append-only"], proofRoot);
  const firstLockfile = await readFile(join(proofRoot, "pnpm-lock.yaml"), "utf8");
  assertResolution(firstLockfile, gitSpecifier);
  const lockfileResolution = firstLockfile
    .split("\n")
    .filter((line) => line.includes("@akb/client") && line.includes(`path:/${subdirectory}`));
  await assertInstalledPackage(proofRoot);
  const installedScripts = join(proofRoot, "node_modules", "@akb", "client", "scripts");
  await copyFile(join(installedScripts, "packed-sdk-runtime.mjs"), runtimeProof);
  await copyFile(join(installedScripts, "packed-sdk-types.ts"), typeProof);
  run(process.execPath, [runtimeProof], proofRoot);
  run(packageManager, ["exec", "tsc", "--noEmit", "--strict", "--target", "ES2022", "--module", "NodeNext", "--moduleResolution", "NodeNext", typeProof], proofRoot);

  await rm(join(proofRoot, "node_modules"), { recursive: true, force: true });
  run(packageManager, ["install", "--frozen-lockfile", "--reporter=append-only"], proofRoot);
  const artifacts = await assertInstalledPackage(proofRoot);
  run(process.execPath, [runtimeProof], proofRoot);
  run(packageManager, ["exec", "tsc", "--noEmit", "--strict", "--target", "ES2022", "--module", "NodeNext", "--moduleResolution", "NodeNext", typeProof], proofRoot);

  const installedManifest = JSON.parse(
    await readFile(join(proofRoot, "node_modules", "@akb", "client", "package.json"), "utf8"),
  );
  console.log(JSON.stringify({
    result: "passed",
    distribution_channel: "development/nightly-git",
    npm_release_artifact_proof: "proof:packed",
    repository: repositoryUrl,
    commit,
    subdirectory,
    git_specifier: gitSpecifier,
    package_version: installedManifest.version,
    lockfile_resolution: lockfileResolution,
    build_approval: {
      package_manager: `${packageManager}@${packageManagerVersion}`,
      key: buildApprovalKey,
      scope: "exact codeload resolution for this commit and subdirectory",
    },
    cold_install: "passed",
    frozen_lockfile_reinstall: "passed",
    imports: { main: "passed", lite: "passed", control_plane: "passed" },
    types: "passed",
    dist: artifacts,
  }, null, 2));
} finally {
  await rm(proofRoot, { recursive: true, force: true });
}

function normalizeRepositoryUrl(value) {
  if (!value) throw new Error("AKB_GIT_REPOSITORY or GITHUB_REPOSITORY is required.");
  const url = new URL(value);
  if (url.protocol !== "https:" || url.hostname !== "github.com" || !url.pathname.endsWith(".git")) {
    throw new Error("AKB_GIT_REPOSITORY must be an https GitHub URL ending in .git.");
  }
  return url.href.replace(/\/$/u, "");
}

function requireFullCommit(value) {
  if (!/^[0-9a-f]{40}$/iu.test(value)) {
    throw new Error("AKB_GIT_SHA must be a full 40-character commit SHA.");
  }
  return value.toLowerCase();
}

function validateSubdirectory(value) {
  if (!value || value.startsWith("/") || value.split("/").some((part) => part === "" || part === "." || part === "..")) {
    throw new Error("AKB_GIT_SUBDIRECTORY must be a relative path without dot segments.");
  }
}

function assertResolution(lockfile, specifier) {
  if (!lockfile.includes(specifier)) {
    throw new Error("pnpm-lock.yaml does not retain the exact Git URL, commit, and subdirectory specifier.");
  }
}

async function assertInstalledPackage(proofRoot) {
  const installedRoot = join(proofRoot, "node_modules", "@akb", "client");
  const files = [
    "dist/index.js",
    "dist/lite.js",
    "dist/control-plane.js",
    "dist/index.d.ts",
    "dist/lite.d.ts",
    "dist/control-plane.d.ts",
  ];
  for (const file of files) {
    await access(join(installedRoot, file));
  }
  for (const directory of ["src", "test"]) {
    try {
      await access(join(installedRoot, directory));
    } catch (error) {
      if (error?.code === "ENOENT") continue;
      throw error;
    }
    throw new Error(`installed package leaked repository ${directory}/ files`);
  }
  return Object.fromEntries(
    await Promise.all(files.map(async (file) => [file, sha256(await readFile(join(installedRoot, file)))])),
  );
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function run(command, args, cwd) {
  const result = spawnSync(command, args, {
    cwd,
    encoding: "utf8",
    env: { ...process.env, npm_config_update_notifier: "false" },
  });
  if (result.status !== 0) {
    throw new Error(`${command} ${args.join(" ")} failed (${result.status})\n${result.stdout}\n${result.stderr}`);
  }
  return result;
}
