import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { storageKey, readInitialVisible, migrateLegacyKey } from "../vault-shell";

beforeEach(() => localStorage.clear());
afterEach(() => localStorage.clear());

describe("vault-shell visibility storage", () => {
  it("storageKey is per-vault", () => {
    expect(storageKey("alpha")).toBe("akb-explorer-visible:alpha");
    expect(storageKey("beta")).not.toBe(storageKey("alpha"));
  });

  it("defaults to true (open) for a fresh vault with no key", () => {
    expect(readInitialVisible("fresh-vault")).toBe(true);
  });

  it("reads vault-scoped value when present", () => {
    localStorage.setItem("akb-explorer-visible:alpha", "0");
    expect(readInitialVisible("alpha")).toBe(false);
  });

  it("migrates legacy global key when vault-scoped is absent", () => {
    localStorage.setItem("akb-explorer-visible", "0");
    migrateLegacyKey("alpha");
    expect(localStorage.getItem("akb-explorer-visible:alpha")).toBe("0");
    expect(readInitialVisible("alpha")).toBe(false);
  });

  it("does NOT overwrite an existing vault-scoped key with legacy value", () => {
    localStorage.setItem("akb-explorer-visible", "0");
    localStorage.setItem("akb-explorer-visible:alpha", "1");
    migrateLegacyKey("alpha");
    expect(localStorage.getItem("akb-explorer-visible:alpha")).toBe("1");
  });
});
