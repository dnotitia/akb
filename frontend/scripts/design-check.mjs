#!/usr/bin/env node
/**
 * Design-system governance guard (centralized enforcement).
 *
 * The token system in src/index.css is the single source of truth for the
 * akb-platform family look. This guard runs in `pnpm build` and fails the
 * build on the two highest-value drift patterns:
 *
 *   1. Hardcoded 6-digit hex colors in component source — colors MUST come
 *      from the token set (var(--color-*) / Tailwind token utilities), so a
 *      theme change in index.css re-skins the whole app.
 *   2. The harsh `bg-foreground text-background` slab — a pre-redesign idiom
 *      that fights the soft family surfaces. Use bg-surface-2 / bg-primary.
 *
 * Allowed: src/index.css (the token definitions themselves) and test files.
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const ROOT = new URL("../src", import.meta.url).pathname;
const HEX = /#[0-9a-fA-F]{6}\b/;
const SLAB = /bg-foreground\s+text-background/;
const EXCLUDE_FILE = /(index\.css|__tests__|\.test\.|\.stories\.)/;

const violations = [];
function walk(dir) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    const s = statSync(p);
    if (s.isDirectory()) walk(p);
    else if (/\.(tsx?|css)$/.test(name)) check(p);
  }
}
function check(file) {
  if (EXCLUDE_FILE.test(file)) return;
  const rel = file.slice(ROOT.length + 1);
  readFileSync(file, "utf8").split("\n").forEach((line, i) => {
    if (HEX.test(line)) violations.push(`${rel}:${i + 1}  hardcoded hex — use a var(--color-*) token`);
    if (SLAB.test(line)) violations.push(`${rel}:${i + 1}  bg-foreground text-background slab — use bg-surface-2 / bg-primary`);
  });
}

walk(ROOT);

if (violations.length) {
  console.error("\n✖ design-check: " + violations.length + " design-system violation(s):\n");
  for (const v of violations) console.error("  " + v);
  console.error("\nColors must come from src/index.css tokens. See frontend/DESIGN_SYSTEM.md.\n");
  process.exit(1);
}
console.log("✓ design-check: token discipline OK (" + "no hardcoded hex / slabs)");
