import { describe, it, expect } from "vitest";
import { sameCommitRef } from "../commit";

describe("sameCommitRef", () => {
  const full = "72a6a3d7bedeb1514923c157017c72373e21cb16";
  const short = "72a6a3d7bede"; // 12-char abbreviation the commit log emits

  it("matches identical full SHAs", () => {
    expect(sameCommitRef(full, full)).toBe(true);
  });

  it("matches a short hash that prefixes the full SHA (the bug fix)", () => {
    expect(sameCommitRef(short, full)).toBe(true);
  });

  it("matches regardless of argument order", () => {
    expect(sameCommitRef(full, short)).toBe(true);
  });

  it("rejects a different commit that shares no prefix", () => {
    expect(sameCommitRef("1f994a7021b3", full)).toBe(false);
  });

  it("rejects when HEAD is missing — stays conservative while loading", () => {
    expect(sameCommitRef(short, undefined)).toBe(false);
    expect(sameCommitRef(short, null)).toBe(false);
    expect(sameCommitRef(undefined, full)).toBe(false);
  });

  it("rejects when both refs are empty", () => {
    expect(sameCommitRef("", "")).toBe(false);
  });
});
