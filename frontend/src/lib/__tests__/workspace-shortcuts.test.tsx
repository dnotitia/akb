import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";
import { readWorkspaceShortcuts, toggleWorkspaceShortcut, removeWorkspaceShortcut, useWorkspaceShortcuts, workspaceShortcutHref, type WorkspaceShortcut } from "../workspace-shortcuts";

const item: WorkspaceShortcut = { kind: "document", vault: "v", path: "c/a.md", title: "A" };
beforeEach(() => localStorage.clear());
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
describe("workspace shortcuts", () => {
  it("isolates accounts, toggles and removes safely", () => {
    expect(toggleWorkspaceShortcut("a", item)).toBe(true);
    expect(readWorkspaceShortcuts("a")).toEqual([item]);
    expect(readWorkspaceShortcuts("b")).toEqual([]);
    expect(toggleWorkspaceShortcut("a", item)).toBe(true);
    expect(readWorkspaceShortcuts("a")).toEqual([]);
    toggleWorkspaceShortcut("a", item);
    removeWorkspaceShortcut("a", item);
    expect(readWorkspaceShortcuts("a")).toEqual([]);
  });
  it("keeps metadata only, deduplicates and guards malformed storage", () => {
    localStorage.setItem("akb.workspaceShortcuts.v1:a", JSON.stringify([item, item, {}, { ...item, path: 4 }]));
    expect(readWorkspaceShortcuts("a")).toEqual([item]);
    localStorage.setItem("akb.workspaceShortcuts.v1:a", "no json");
    expect(readWorkspaceShortcuts("a")).toEqual([]);
    toggleWorkspaceShortcut("a", { ...item, body: "private body" } as WorkspaceShortcut);
    expect(localStorage.getItem("akb.workspaceShortcuts.v1:a")).not.toContain("private body");
  });
  it("refuses excess pins without evicting existing shortcuts", () => {
    for (let i = 0; i < 30; i++) expect(toggleWorkspaceShortcut("a", { ...item, path: `${i}.md` })).toBe(true);
    expect(toggleWorkspaceShortcut("a", item)).toBe(false);
    expect(readWorkspaceShortcuts("a")).toHaveLength(30);
  });
  it("reports unavailable storage and encodes internal destinations", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw Error("blocked"); });
    expect(toggleWorkspaceShortcut("a", item)).toBe(false);
    expect(workspaceShortcutHref(item)).toBe("/vault/v/doc/c%2Fa.md");
    expect(workspaceShortcutHref({ ...item, kind: "collection", path: "c/a & b" })).toBe("/vault/v?collection=c%2Fa%20%26%20b");
  });
  it("updates mounted consumers in the same tab and across tabs, switching accounts immediately", () => {
    const first = renderHook(({ user }) => useWorkspaceShortcuts(user), { initialProps: { user: "a" } });
    const second = renderHook(() => useWorkspaceShortcuts("a"));
    act(() => { toggleWorkspaceShortcut("a", item); });
    expect(first.result.current).toEqual([item]);
    expect(second.result.current).toEqual([item]);
    act(() => { localStorage.removeItem("akb.workspaceShortcuts.v1:a"); window.dispatchEvent(new StorageEvent("storage", { key: "akb.workspaceShortcuts.v1:a" })); });
    expect(first.result.current).toEqual([]);
    act(() => { toggleWorkspaceShortcut("a", item); });
    first.rerender({ user: "b" });
    expect(first.result.current).toEqual([]);
  });
});
