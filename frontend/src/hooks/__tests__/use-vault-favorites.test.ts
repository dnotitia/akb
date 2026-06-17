import { describe, it, expect, beforeEach, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useVaultFavorites } from "../use-vault-favorites";

const KEY = "akb-vault-favorites";
beforeEach(() => localStorage.clear());

describe("useVaultFavorites · readIds guards", () => {
  it("starts empty with no stored value", () => {
    const { result } = renderHook(() => useVaultFavorites());
    expect(result.current.favorites).toEqual([]);
  });

  it("recovers from non-JSON garbage", () => {
    localStorage.setItem(KEY, "{not json");
    const { result } = renderHook(() => useVaultFavorites());
    expect(result.current.favorites).toEqual([]);
  });

  it("ignores a non-array stored root", () => {
    localStorage.setItem(KEY, JSON.stringify({ a: 1 }));
    const { result } = renderHook(() => useVaultFavorites());
    expect(result.current.favorites).toEqual([]);
  });

  it("drops non-string entries from a mixed array", () => {
    localStorage.setItem(KEY, JSON.stringify(["v-1", 42, null, "v-2"]));
    const { result } = renderHook(() => useVaultFavorites());
    expect(result.current.favorites).toEqual(["v-1", "v-2"]);
  });
});

describe("useVaultFavorites · toggle", () => {
  it("adds (prepended, newest-first) and reflects isFavorite/favOrder", () => {
    const { result } = renderHook(() => useVaultFavorites());
    act(() => result.current.toggleFavorite("v-1"));
    act(() => result.current.toggleFavorite("v-2"));
    expect(result.current.favorites).toEqual(["v-2", "v-1"]);
    expect(result.current.isFavorite("v-1")).toBe(true);
    expect(result.current.favOrder("v-2")).toBe(0);
    expect(result.current.favOrder("v-1")).toBe(1);
    expect(result.current.favOrder("missing")).toBe(-1);
  });

  it("removes on a second toggle and persists to storage", () => {
    const { result } = renderHook(() => useVaultFavorites());
    act(() => result.current.toggleFavorite("v-1"));
    act(() => result.current.toggleFavorite("v-1"));
    expect(result.current.favorites).toEqual([]);
    expect(JSON.parse(localStorage.getItem(KEY)!)).toEqual([]);
  });

  it("caps at 100, evicting the oldest", () => {
    const { result } = renderHook(() => useVaultFavorites());
    act(() => {
      for (let i = 0; i < 105; i++) result.current.toggleFavorite(`v-${i}`);
    });
    expect(result.current.favorites.length).toBe(100);
    expect(result.current.favorites[0]).toBe("v-104"); // newest survives
    expect(result.current.isFavorite("v-0")).toBe(false); // oldest evicted
  });
});

describe("useVaultFavorites · cross-tab sync", () => {
  it("re-reads on a storage event for the favorites key", () => {
    const { result } = renderHook(() => useVaultFavorites());
    localStorage.setItem(KEY, JSON.stringify(["from-other-tab"]));
    act(() => {
      window.dispatchEvent(new StorageEvent("storage", { key: KEY }));
    });
    expect(result.current.favorites).toEqual(["from-other-tab"]);
  });

  it("ignores storage events for unrelated keys", () => {
    const { result } = renderHook(() => useVaultFavorites());
    act(() => result.current.toggleFavorite("v-1"));
    localStorage.setItem("some-other-key", "x");
    act(() => {
      window.dispatchEvent(new StorageEvent("storage", { key: "some-other-key" }));
    });
    expect(result.current.favorites).toEqual(["v-1"]);
  });
});

describe("useVaultFavorites · storage-disabled safety", () => {
  it("degrades without throwing when setItem throws", () => {
    const spy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("QuotaExceeded");
    });
    const { result } = renderHook(() => useVaultFavorites());
    expect(() => act(() => result.current.toggleFavorite("v-1"))).not.toThrow();
    // in-memory state still updates even though persistence failed
    expect(result.current.isFavorite("v-1")).toBe(true);
    spy.mockRestore();
  });
});
