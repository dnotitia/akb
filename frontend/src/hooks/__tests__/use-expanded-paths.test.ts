import { beforeEach, describe, expect, it } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useExpandedPaths } from "@/hooks/use-vault-tree";

describe("useExpandedPaths", () => {
  beforeEach(() => localStorage.clear());

  it("toggle flips a path on and off", () => {
    const { result } = renderHook(() => useExpandedPaths("v"));
    expect(result.current.expanded.has("features")).toBe(false);
    act(() => result.current.toggle("features"));
    expect(result.current.expanded.has("features")).toBe(true);
    act(() => result.current.toggle("features"));
    expect(result.current.expanded.has("features")).toBe(false);
  });

  it("expand is idempotent", () => {
    const { result } = renderHook(() => useExpandedPaths("v"));
    act(() => result.current.expand("a"));
    const first = result.current.expanded;
    act(() => result.current.expand("a"));
    // Same identity — nothing changed, so no new Set was created.
    expect(result.current.expanded).toBe(first);
  });

  it("revealAncestorsOf opens every ancestor in one shot", () => {
    const { result } = renderHook(() => useExpandedPaths("v"));
    act(() => result.current.revealAncestorsOf("a/b/c/d.md"));
    expect([...result.current.expanded].sort()).toEqual(["a", "a/b", "a/b/c"]);
  });

  it("persists expanded state to localStorage, keyed per vault", () => {
    const { result } = renderHook(() => useExpandedPaths("v"));
    act(() => result.current.toggle("features"));
    expect(JSON.parse(localStorage.getItem("akb-explorer-expanded:v")!)).toEqual(["features"]);
  });

  it("callbacks are identity-stable across re-renders (so memoized consumers stay memoized)", () => {
    const { result, rerender } = renderHook(() => useExpandedPaths("v"));
    const before = {
      toggle: result.current.toggle,
      expand: result.current.expand,
      revealAncestorsOf: result.current.revealAncestorsOf,
    };
    act(() => result.current.toggle("features"));
    rerender();
    expect(result.current.toggle).toBe(before.toggle);
    expect(result.current.expand).toBe(before.expand);
    expect(result.current.revealAncestorsOf).toBe(before.revealAncestorsOf);
  });
});
