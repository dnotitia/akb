// frontend/src/hooks/__tests__/use-graph-history.test.ts
import { describe, it, expect, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useGraphHistory } from "../use-graph-history";

beforeEach(() => localStorage.clear());

describe("useGraphHistory · recent", () => {
  it("starts empty", () => {
    const { result } = renderHook(() => useGraphHistory("akb"));
    expect(result.current.recent).toEqual([]);
  });

  it("pushes a recent entry", () => {
    const { result } = renderHook(() => useGraphHistory("akb"));
    act(() => result.current.pushRecent({ doc_id: "d-1", title: "First" }));
    expect(result.current.recent).toEqual([{ doc_id: "d-1", title: "First" }]);
  });

  it("dedupes and moves to front on re-push", () => {
    const { result } = renderHook(() => useGraphHistory("akb"));
    act(() => result.current.pushRecent({ doc_id: "d-1", title: "First" }));
    act(() => result.current.pushRecent({ doc_id: "d-2", title: "Second" }));
    act(() => result.current.pushRecent({ doc_id: "d-1", title: "First again" }));
    expect(result.current.recent.map((r) => r.doc_id)).toEqual(["d-1", "d-2"]);
    expect(result.current.recent[0].title).toBe("First again");
  });

  it("caps at 5 entries (oldest first eviction)", () => {
    const { result } = renderHook(() => useGraphHistory("akb"));
    for (let i = 1; i <= 7; i++) {
      act(() => result.current.pushRecent({ doc_id: `d-${i}`, title: `T${i}` }));
    }
    expect(result.current.recent.length).toBe(5);
    expect(result.current.recent[0].doc_id).toBe("d-7");
    expect(result.current.recent[4].doc_id).toBe("d-3");
  });

  it("clearRecent empties storage", () => {
    const { result } = renderHook(() => useGraphHistory("akb"));
    act(() => result.current.pushRecent({ doc_id: "d-1", title: "x" }));
    act(() => result.current.clearRecent());
    expect(result.current.recent).toEqual([]);
  });
});

describe("useGraphHistory · saved views", () => {
  it("starts empty", () => {
    const { result } = renderHook(() => useGraphHistory("akb"));
    expect(result.current.saved).toEqual([]);
  });

  it("saves a named view", () => {
    const { result } = renderHook(() => useGraphHistory("akb"));
    act(() => result.current.saveView("roadmap", "?entry=d-94d8657f&depth=2"));
    expect(result.current.saved).toEqual([{ name: "roadmap", url: "?entry=d-94d8657f&depth=2" }]);
  });

  it("overwrites a duplicate name", () => {
    const { result } = renderHook(() => useGraphHistory("akb"));
    act(() => result.current.saveView("roadmap", "?entry=d-1"));
    act(() => result.current.saveView("roadmap", "?entry=d-2"));
    expect(result.current.saved.length).toBe(1);
    expect(result.current.saved[0].url).toBe("?entry=d-2");
  });

  it("caps at 20 entries (oldest evicted)", () => {
    const { result } = renderHook(() => useGraphHistory("akb"));
    for (let i = 1; i <= 22; i++) {
      act(() => result.current.saveView(`v${i}`, `?entry=d-${i}`));
    }
    expect(result.current.saved.length).toBe(20);
    expect(result.current.saved[0].name).toBe("v22");
    expect(result.current.saved[19].name).toBe("v3");
  });

  it("deleteView removes by name", () => {
    const { result } = renderHook(() => useGraphHistory("akb"));
    act(() => result.current.saveView("a", "?x=1"));
    act(() => result.current.saveView("b", "?y=2"));
    act(() => result.current.deleteView("a"));
    expect(result.current.saved.map((v) => v.name)).toEqual(["b"]);
  });

  it("scopes storage per vault", () => {
    const { result: a } = renderHook(() => useGraphHistory("vault-a"));
    const { result: b } = renderHook(() => useGraphHistory("vault-b"));
    act(() => a.current.saveView("a-view", "?x=1"));
    expect(b.current.saved).toEqual([]);
  });
});
