import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readRecentCollections, recordRecentCollection, RECENT_COLLECTIONS_EVENT, useRecentCollections } from "@/lib/recent-collections";
import { MemoryRouter } from "react-router-dom";
import { render } from "@/test-query-client";
import { CurrentUserProvider } from "@/contexts/current-user-context";
import { VaultExplorer } from "@/components/vault-explorer";
import { useVaultTree } from "@/hooks/use-vault-tree";
import type { CurrentUser } from "@/lib/api";

vi.mock("@/hooks/use-vault-tree", async original => ({
  ...await original<typeof import("@/hooks/use-vault-tree")>(),
  useVaultTree: vi.fn(),
}));
vi.mock("@/lib/api", async original => ({
  ...await original<typeof import("@/lib/api")>(),
  getVaultInfo: vi.fn().mockResolvedValue({ role: "reader" }),
}));

const key = (userId: string) => `akb.recentCollections.v1:${userId}`;
const now = new Date("2026-10-21T10:00:00.000Z");
const row = (path: string, viewedAt = now.toISOString()) => ({ vault: "team", path, viewedAt });

beforeEach(() => {
  localStorage.clear();
  vi.useFakeTimers();
  vi.setSystemTime(now);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("recent collections", () => {
  it("isolates accounts and distinguishes the same collection path in different Vaults", () => {
    recordRecentCollection("alice", { vault: "team", path: "notes" });
    recordRecentCollection("alice", { vault: "personal", path: "notes" });
    recordRecentCollection("bob", { vault: "private", path: "plans" });
    expect(readRecentCollections("alice").map(item => item.vault)).toEqual(["personal", "team"]);
    expect(readRecentCollections("bob").map(item => item.path)).toEqual(["plans"]);
    expect(readRecentCollections("")).toEqual([]);
  });

  it("moves revisits to the front, retains at most twelve, and bounds caller limits", () => {
    for (let index = 0; index < 15; index++) {
      vi.setSystemTime(now.getTime() + index * 1000);
      recordRecentCollection("alice", { vault: "team", path: `notes/${index}` });
    }
    recordRecentCollection("alice", { vault: "team", path: "notes/5" });
    const recent = readRecentCollections("alice");
    expect(recent).toHaveLength(12);
    expect(recent[0].path).toBe("notes/5");
    expect(recent.filter(item => item.path === "notes/5")).toHaveLength(1);
    expect(recent.some(item => item.path === "notes/0")).toBe(false);
    expect(JSON.parse(localStorage.getItem(key("alice"))!)).toHaveLength(12);
    expect(readRecentCollections("alice", 100)).toHaveLength(12);
    expect(readRecentCollections("alice", 3.9)).toHaveLength(3);
    expect(readRecentCollections("alice", -1)).toEqual([]);
    expect(readRecentCollections("alice", Number.NaN)).toHaveLength(12);
  });

  it("rejects corrupt records and deduplicates newest-first without exposing extra fields", () => {
    localStorage.setItem(key("alice"), JSON.stringify([
      row("notes", "2026-10-20T10:00:00.000Z"),
      { ...row("notes"), unrelated: "discard" },
      row(""), row("  "), row("x".repeat(2049)),
      { ...row("valid"), vault: "x".repeat(2049) },
      row("bad-date", "invalid"), row("number-date", "123"),
      row("future", "2099-01-01T00:00:00.000Z"),
      null, 3, {}, row("guides"),
    ]));
    expect(readRecentCollections("alice")).toEqual([row("notes"), row("guides")]);
    for (const invalid of ["invalid-json", "null", "{}", "1"]) {
      localStorage.setItem(key("alice"), invalid);
      expect(readRecentCollections("alice")).toEqual([]);
    }
  });

  it("does not create history for empty users or invalid collection identities", () => {
    recordRecentCollection("", { vault: "team", path: "notes" });
    recordRecentCollection("alice", { vault: "", path: "notes" });
    recordRecentCollection("alice", { vault: "team", path: " " });
    recordRecentCollection("alice", { vault: "team", path: "x".repeat(2049) });
    expect(localStorage.length).toBe(0);
  });

  it("tolerates denied reads and writes without publishing a successful change", () => {
    const listener = vi.fn();
    window.addEventListener(RECENT_COLLECTIONS_EVENT, listener);
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("denied"); });
    expect(readRecentCollections("alice")).toEqual([]);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("denied"); });
    expect(() => recordRecentCollection("alice", { vault: "team", path: "notes" })).not.toThrow();
    expect(listener).not.toHaveBeenCalled();
    window.removeEventListener(RECENT_COLLECTIONS_EVENT, listener);
  });

  it("updates mounted consumers in the same tab and switches account history immediately", () => {
    const { result, rerender } = renderHook(({ userId }: { userId: string | undefined }) => useRecentCollections(userId), {
      initialProps: { userId: "alice" as string | undefined },
    });
    act(() => recordRecentCollection("alice", { vault: "team", path: "notes" }));
    expect(result.current).toEqual([row("notes")]);
    act(() => recordRecentCollection("bob", { vault: "private", path: "plans" }));
    expect(result.current).toEqual([row("notes")]);
    rerender({ userId: "bob" });
    expect(result.current[0]).toMatchObject({ vault: "private", path: "plans" });
    rerender({ userId: undefined });
    expect(result.current).toEqual([]);
  });

  it("syncs cross-tab writes and clear events without reacting to other accounts", () => {
    const { result } = renderHook(() => useRecentCollections("alice"));
    localStorage.setItem(key("alice"), JSON.stringify([row("notes")]));
    act(() => window.dispatchEvent(new StorageEvent("storage", { key: key("bob") })));
    expect(result.current).toEqual([]);
    act(() => window.dispatchEvent(new StorageEvent("storage", { key: key("alice") })));
    expect(result.current).toEqual([row("notes")]);
    localStorage.clear();
    act(() => window.dispatchEvent(new StorageEvent("storage", { key: null })));
    expect(result.current).toEqual([]);
  });
});

describe("Explorer visit recording", () => {
  const treeState = () => ({
    tree: [{ kind: "collection" as const, name: "notes", path: "notes", children: [
      { kind: "document" as const, name: "One", path: "notes/one.md" },
    ] }],
    items: [], path: "", accessDenied: false, complete: true, paginationUnsupported: false, hasMore: false, pageCount: 1, pastCollectionCatalog: false, loadingMore: false, loadMore: vi.fn(),
    loading: false, refreshing: false, showingPreviousScope: false, unsupported: false, error: "", refetch: vi.fn(),
  });
  const explorer = (route: string, userId = "alice", checking = false) => (
    <CurrentUserProvider user={{ user_id: userId, username: userId, is_admin: false } as CurrentUser} checking={checking}>
      <MemoryRouter initialEntries={[route]}><VaultExplorer vault="team" presentation="focused" /></MemoryRouter>
    </CurrentUserProvider>
  );

  it.each(["/vault/team?collection=notes", "/vault/team/doc/notes%2Fone.md"])("records a known visited collection from %s without bumping on tree refresh", async route => {
    vi.mocked(useVaultTree).mockReturnValue(treeState());
    const view = render(explorer(route));
    await act(async () => {});
    expect(readRecentCollections("alice")).toEqual([row("notes")]);
    vi.setSystemTime(now.getTime() + 60_000);
    vi.mocked(useVaultTree).mockReturnValue({ ...treeState(), refreshing: true });
    view.rerender(explorer(route));
    vi.mocked(useVaultTree).mockReturnValue(treeState());
    view.rerender(explorer(route));
    expect(readRecentCollections("alice")).toEqual([row("notes")]);
  });

  it.each(["loading", "refreshing", "showingPreviousScope", "unsupported", "error"] as const)("does not record while tree state is %s", async flag => {
    const state = treeState();
    vi.mocked(useVaultTree).mockReturnValue({ ...state, [flag]: flag === "error" ? "Forbidden" : true });
    const view = render(explorer("/vault/team?collection=notes"));
    await act(async () => {});
    expect(readRecentCollections("alice")).toEqual([]);
    vi.mocked(useVaultTree).mockReturnValue(state);
    view.rerender(explorer("/vault/team?collection=notes"));
    expect(readRecentCollections("alice")).toEqual([row("notes")]);
  });

  it("waits for current access and never copies an old account's placeholder tree into the next account's history", async () => {
    vi.mocked(useVaultTree).mockReturnValue(treeState());
    const route = "/vault/team?collection=notes";
    const view = render(explorer(route, "alice", true));
    await act(async () => {});
    expect(readRecentCollections("alice")).toEqual([]);
    view.rerender(explorer(route));
    expect(readRecentCollections("alice")).toEqual([row("notes")]);
    vi.mocked(useVaultTree).mockReturnValue({ ...treeState(), showingPreviousScope: true });
    view.rerender(explorer(route, "bob"));
    expect(readRecentCollections("bob")).toEqual([]);
    vi.mocked(useVaultTree).mockReturnValue({ ...treeState(), tree: [] });
    view.rerender(explorer(route, "bob"));
    expect(readRecentCollections("bob")).toEqual([]);
    vi.mocked(useVaultTree).mockReturnValue(treeState());
    view.rerender(explorer(route, "bob"));
    expect(readRecentCollections("bob")).toEqual([row("notes")]);
  });

  it.each(["/vault/team", "/vault/team?collection=deleted", "/vault/team/doc/deleted%2Fmissing.md"])("does not infer a visit to an absent collection from %s", async route => {
    vi.mocked(useVaultTree).mockReturnValue(treeState());
    render(explorer(route));
    await act(async () => {});
    expect(readRecentCollections("alice")).toEqual([]);
  });
});
