import { describe, it, expect, beforeEach } from "vitest";
import { inScope, readScope, writeScope } from "@/lib/vault-scope";

beforeEach(() => localStorage.clear());

describe("inScope", () => {
  it("all → always true (incl. undefined role)", () => {
    expect(inScope("reader", "all")).toBe(true);
    expect(inScope(undefined, "all")).toBe(true);
  });

  it("owned → owner only", () => {
    expect(inScope("owner", "owned")).toBe(true);
    expect(inScope("admin", "owned")).toBe(false);
    expect(inScope("writer", "owned")).toBe(false);
    expect(inScope(undefined, "owned")).toBe(false);
  });

  it("editable → owner/admin/writer, not reader/unknown/undefined", () => {
    expect(inScope("owner", "editable")).toBe(true);
    expect(inScope("admin", "editable")).toBe(true);
    expect(inScope("writer", "editable")).toBe(true);
    expect(inScope("reader", "editable")).toBe(false);
    expect(inScope(undefined, "editable")).toBe(false);
  });
});

describe("readScope / writeScope", () => {
  it("defaults to 'all' when nothing stored", () => {
    expect(readScope()).toBe("all");
  });

  it("round-trips a valid scope", () => {
    writeScope("owned");
    expect(readScope()).toBe("owned");
  });

  it("rejects a garbage stored value back to 'all'", () => {
    localStorage.setItem("akb-vault-role-scope", "bogus");
    expect(readScope()).toBe("all");
  });
});
