import { describe, it, expect } from "vitest";
import { sameCommitRef } from "../commit";

// Fixtures are git commit SHAs, not secrets — the allowlist pragmas silence
// detect-secrets' high-entropy-hex heuristic.
const FULL = "72a6a3d7bedeb1514923c157017c72373e21cb16"; // pragma: allowlist secret
const SHORT = FULL.slice(0, 12); // the 12-char abbreviation the commit log emits
const OTHER = "1f994a7021b3"; // pragma: allowlist secret — a different commit

describe("sameCommitRef", () => {
  it("matches identical full SHAs", () => {
    expect(sameCommitRef(FULL, FULL)).toBe(true);
  });

  it("matches a short hash that prefixes the full SHA (the bug fix)", () => {
    expect(sameCommitRef(SHORT, FULL)).toBe(true);
  });

  it("matches regardless of argument order", () => {
    expect(sameCommitRef(FULL, SHORT)).toBe(true);
  });

  it("rejects a different commit that shares no prefix", () => {
    expect(sameCommitRef(OTHER, FULL)).toBe(false);
  });

  it("rejects when HEAD is missing — stays conservative while loading", () => {
    expect(sameCommitRef(SHORT, undefined)).toBe(false);
    expect(sameCommitRef(SHORT, null)).toBe(false);
    expect(sameCommitRef(undefined, FULL)).toBe(false);
  });

  it("rejects when both refs are empty", () => {
    expect(sameCommitRef("", "")).toBe(false);
  });
});
