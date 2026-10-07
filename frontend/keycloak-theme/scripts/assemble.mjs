#!/usr/bin/env node
// Assemble the deployable Keycloak theme directory.
//
//   node keycloak-theme/scripts/assemble.mjs [outDir]   (default: dist-keycloak-theme)
//
// Output: <outDir>/akb/login/... — the source theme, plus
//   - Pretendard (npm `pretendard`, SIL OFL 1.1) as the dynamic-subset build, so
//     a page fetches only the glyph ranges it uses, with its licence beside it;
//   - theme.properties `assetVersion` set to a hash of everything shipped, so a
//     changed file gets a new URL instead of a 30-day-cached old one.
// Fonts are copied rather than committed: the package is already a pinned
// dependency of the app, and one copy of 3 MB of binaries in git is enough.
import { createHash } from "node:crypto";
import { cpSync, existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { buildTokens, OUTPUT as TOKENS } from "./theme-tokens.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const frontend = resolve(here, "../..");
const source = resolve(frontend, "keycloak-theme/akb");
const pretendard = resolve(frontend, "node_modules/pretendard/dist");

function files(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? files(path) : [path];
  });
}

export function assemble(outDir) {
  if (readFileSync(TOKENS, "utf8") !== buildTokens()) {
    throw new Error("akb-tokens.generated.css is stale; run `pnpm run theme:tokens` first");
  }
  if (!existsSync(join(pretendard, "web/variable/pretendardvariable-dynamic-subset.css"))) {
    throw new Error("pretendard is not installed; run `pnpm install` in frontend/");
  }
  const target = resolve(outDir, "akb");
  rmSync(target, { recursive: true, force: true });
  mkdirSync(target, { recursive: true });
  cpSync(source, target, { recursive: true });

  const fonts = join(target, "login/resources/fonts/pretendard");
  mkdirSync(fonts, { recursive: true });
  cpSync(join(pretendard, "web/variable/pretendardvariable-dynamic-subset.css"), join(fonts, "pretendardvariable-dynamic-subset.css"));
  cpSync(join(pretendard, "web/variable/woff2-dynamic-subset"), join(fonts, "woff2-dynamic-subset"), { recursive: true });
  cpSync(join(pretendard, "LICENSE.txt"), join(fonts, "OFL.txt"));

  const properties = join(target, "login/theme.properties");
  const hash = createHash("sha256");
  for (const path of files(target).sort()) {
    if (path === properties) continue;
    hash.update(relative(target, path));
    hash.update("\0");
    hash.update(readFileSync(path));
    hash.update("\0");
  }
  const version = hash.digest("hex").slice(0, 16);
  const text = readFileSync(properties, "utf8");
  if (!/^assetVersion=dev$/m.test(text)) throw new Error("theme.properties must carry `assetVersion=dev` in source");
  writeFileSync(properties, text.replace(/^assetVersion=dev$/m, `assetVersion=${version}`));
  return { target, version };
}

const isMain = process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  const out = resolve(frontend, process.argv[2] || "dist-keycloak-theme");
  const { target, version } = assemble(out);
  console.log(`assembled ${target} (assetVersion=${version})`);
}
