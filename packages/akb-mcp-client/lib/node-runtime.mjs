import { readFileSync } from "node:fs";

export const NODE_ENGINE = JSON.parse(
  readFileSync(new URL("../package.json", import.meta.url), "utf8"),
).engines.node;

export function nodeVersionMeetsFloor(value) {
  if (!/^v?\d+\.\d+\.\d+$/.test(String(value))) return false;
  const [major, minor, patch] = String(value).replace(/^v/, "").split(".").map(Number);
  const floor = { 22: [23, 4], 24: [22, 1], 26: [12, 1] }[major];
  return Boolean(floor && (minor > floor[0] || (minor === floor[0] && patch >= floor[1])));
}

export function requireSupportedNode(version = process.versions.node) {
  if (!nodeVersionMeetsFloor(version)) {
    throw new Error(`Unsupported Node.js ${version}. Security-patched Node.js ${NODE_ENGINE} is required. Upgrade Node.js before running akb-mcp.`);
  }
}
