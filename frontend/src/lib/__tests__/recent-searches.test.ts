import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  clearRecentSearches,
  readRecentSearches,
  recordRecentSearch,
} from "@/lib/recent-searches";

describe("recent search history", () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-26T03:00:00.000Z"));
  });

  afterEach(() => vi.useRealTimers());

  it("scopes history by user and moves a repeated search to the front", () => {
    recordRecentSearch("user-a", {
      query: "postgres tuning",
      mode: "semantic",
      surface: "advanced",
    });
    vi.setSystemTime(new Date("2026-08-26T03:05:00.000Z"));
    recordRecentSearch("user-a", {
      query: "TODO",
      mode: "literal",
      vaults: ["beta", "alpha"],
      surface: "advanced",
    });
    vi.setSystemTime(new Date("2026-08-26T03:10:00.000Z"));
    recordRecentSearch("user-a", {
      query: "Postgres tuning",
      mode: "semantic",
      surface: "advanced",
    });

    expect(readRecentSearches("user-a")).toEqual([
      expect.objectContaining({ query: "Postgres tuning", mode: "semantic" }),
      expect.objectContaining({
        query: "TODO",
        mode: "literal",
        vaults: ["alpha", "beta"],
      }),
    ]);
    expect(readRecentSearches("user-b")).toEqual([]);
  });

  it("clears only the selected user's local history", () => {
    recordRecentSearch("user-a", {
      query: "deployment guide",
      mode: "semantic",
      surface: "global",
    });
    recordRecentSearch("user-b", {
      query: "authentication",
      mode: "semantic",
      surface: "global",
    });

    clearRecentSearches("user-a");

    expect(readRecentSearches("user-a")).toEqual([]);
    expect(readRecentSearches("user-b")).toHaveLength(1);
  });

  it("clears only the selected surface and Vault scope", () => {
    recordRecentSearch("user-a", { query: "all vaults", mode: "semantic", surface: "global" });
    recordRecentSearch("user-a", { query: "alpha quick", mode: "semantic", surface: "global", vaults: ["alpha"] });
    recordRecentSearch("user-a", { query: "alpha advanced", mode: "literal", surface: "advanced", vaults: ["alpha"] });
    clearRecentSearches("user-a", { surface: "global", vaults: ["alpha"] });
    expect(readRecentSearches("user-a").map(item => item.query).sort()).toEqual(["all vaults", "alpha advanced"]);
  });
});
