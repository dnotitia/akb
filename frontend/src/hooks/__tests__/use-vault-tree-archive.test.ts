import { act, cleanup, waitFor } from "@testing-library/react";
import { renderHook } from "@/test-query-client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { browseVault, type ArchiveScope } from "@/lib/api";
import { useVaultTree } from "../use-vault-tree";

vi.mock("@/lib/api", async original => ({ ...await original<typeof import("@/lib/api")>(), browseVault: vi.fn() }));
const browse = vi.mocked(browseVault);
const response = (scope?: ArchiveScope) => ({
  vault: "v", path: "", archive_scope: scope, complete: true, next_cursor: null,
  items: [{ type: "document", name: scope ?? "Current", path: "guides/doc.md", status: scope === "archived" ? "archived" : "active" }],
});
beforeEach(() => { vi.clearAllMocks(); browse.mockResolvedValue(response()); });
afterEach(cleanup);

describe("archive-aware Vault tree", () => {
  it("does not fetch the next page automatically and follows an empty page's cursor", async () => {
    browse.mockResolvedValueOnce({ ...response(), items: [], complete: false, next_cursor: "next" });
    const { result } = renderHook(() => useVaultTree("v"));
    await waitFor(() => expect(result.current.hasMore).toBe(true));
    expect(result.current.complete).toBe(false);
    expect(result.current.tree).toEqual([]);
    expect(browse).toHaveBeenCalledTimes(1);
    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.complete).toBe(true));
    expect(browse).toHaveBeenLastCalledWith("v", undefined, -1, {
      page_size: 100, signal: expect.anything(), archive_scope: "unarchived", cursor: "next",
    });
    expect(result.current.tree?.[0].children?.[0].name).toBe("Current");
  });

  it("keeps previous items after a failed next page and retries the same cursor", async () => {
    browse.mockResolvedValueOnce({ ...response(), complete: false, next_cursor: "next" });
    const { result } = renderHook(() => useVaultTree("v"));
    await waitFor(() => expect(result.current.hasMore).toBe(true));
    browse.mockRejectedValueOnce(new Error("Read unavailable"));
    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.error).toBe("Read unavailable"));
    expect(result.current.tree?.[0].children?.[0].name).toBe("Current");
    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.complete).toBe(true));
    expect(browse.mock.calls.at(-1)?.[3]?.cursor).toBe("next");
  });

  it("sends Collection and resource-kind scopes before pagination", async () => {
    const { result } = renderHook(() => useVaultTree("v", "unarchived", { collection: "guides", content_type: "files" }));
    await waitFor(() => expect(result.current.complete).toBe(true));
    expect(browse).toHaveBeenCalledWith("v", "guides", -1, {
      page_size: 100, signal: expect.anything(), archive_scope: "unarchived", content_type: "files",
    });
  });

  it("does not mistake an old server's missing receipt for confirmed completeness", async () => {
    browse.mockResolvedValueOnce({ vault: "v", path: "", items: [] });
    const { result } = renderHook(() => useVaultTree("v"));
    await waitFor(() => expect(result.current.paginationUnsupported).toBe(true));
    expect(result.current.complete).toBe(false);
    expect(result.current.hasMore).toBe(false);
  });

  it("requests a bounded first page and refetches on a status change", async () => {
    const { result } = renderHook(() => useVaultTree("v"));
    await waitFor(() => expect(result.current.tree).not.toBeNull());
    expect(browse).toHaveBeenCalledWith("v", undefined, -1, { page_size: 100, signal: expect.anything(), archive_scope: "unarchived" });
    act(() => window.dispatchEvent(new CustomEvent("akb:document-status-changed", { detail: { vault: "other" } })));
    expect(browse).toHaveBeenCalledTimes(1);
    act(() => window.dispatchEvent(new CustomEvent("akb:document-status-changed", { detail: { vault: "v" } })));
    await waitFor(() => expect(browse).toHaveBeenCalledTimes(2));
  });

  it("retains the prior snapshot during scope loading, then replaces it with confirmed archives", async () => {
    const { result, rerender } = renderHook(({ scope }: { scope: ArchiveScope }) => useVaultTree("v", scope), { initialProps: { scope: "unarchived" as ArchiveScope } });
    await waitFor(() => expect(result.current.tree).not.toBeNull());
    let resolve!: (value: ReturnType<typeof response>) => void;
    browse.mockReturnValueOnce(new Promise((done) => { resolve = done; }));
    rerender({ scope: "archived" });
    expect(result.current.tree?.[0].children?.[0].name).toBe("Current");
    expect(result.current.showingPreviousScope).toBe(true);
    expect(result.current.refreshing).toBe(true);
    await act(async () => resolve(response("archived")));
    await waitFor(() => expect(result.current.showingPreviousScope).toBe(false));
    expect(result.current.tree?.[0].children?.[0].raw.status).toBe("archived");
    expect(browse).toHaveBeenLastCalledWith("v", undefined, -1, { page_size: 100, signal: expect.anything(), archive_scope: "archived" });
  });

  it("reuses a Vault's tree on return and never shows another Vault's tree", async () => {
    const { result, rerender } = renderHook(({ vault }: { vault: string }) => useVaultTree(vault), { initialProps: { vault: "v" } });
    await waitFor(() => expect(result.current.tree).not.toBeNull());
    browse.mockReturnValueOnce(new Promise(() => {}));
    rerender({ vault: "other" });
    expect(result.current.tree).toBeNull();
    expect(result.current.loading).toBe(true);
    rerender({ vault: "v" });
    expect(result.current.tree?.[0].children?.[0].name).toBe("Current");
    expect(browse.mock.calls.filter(([vault]) => vault === "v")).toHaveLength(1);
  });

  it("does not expose an ignored scope as a valid tree", async () => {
    const { result } = renderHook(() => useVaultTree("v", "archived"));
    await waitFor(() => expect(result.current.unsupported).toBe(true));
    expect(result.current.tree).toEqual([]);
    expect(result.current.loading).toBe(false);
  });
});

it("cancels obsolete name scopes and does not accept an ignored server query", async () => {
  const { result, rerender } = renderHook(({ name }: { name: string }) => useVaultTree("v", "unarchived", { name_query: name }), { initialProps: { name: "" } });
  await waitFor(() => expect(result.current.complete).toBe(true));
  browse.mockReturnValueOnce(new Promise(() => {}));
  rerender({ name: "old" });
  await waitFor(() => expect(browse.mock.calls.at(-1)?.[3]?.name_query).toBe("old"));
  const oldSignal = browse.mock.calls.at(-1)?.[3]?.signal;
  browse.mockResolvedValueOnce({ ...response(), name_query: "new" });
  rerender({ name: "new" });
  await waitFor(() => expect(result.current.showingPreviousScope).toBe(false));
  expect(oldSignal?.aborted).toBe(true);
  browse.mockResolvedValueOnce(response());
  rerender({ name: "ignored" });
  await waitFor(() => expect(result.current.error).toContain("Name search is unavailable"));
});

it("recognizes the resource phase even when status filtering leaves an empty page", async () => {
  browse.mockResolvedValueOnce({ ...response(), items: [{ type: "collection", name: "Guides", path: "guides" }], complete: false, next_cursor: "documents" });
  const { result } = renderHook(() => useVaultTree("v"));
  await waitFor(() => expect(result.current.hasMore).toBe(true));
  expect(result.current.pastCollectionCatalog).toBe(false);
  browse.mockResolvedValueOnce({ ...response(), items: [], complete: false, next_cursor: "more-documents" });
  act(() => result.current.loadMore());
  await waitFor(() => expect(result.current.pageCount).toBe(2));
  expect(result.current.pastCollectionCatalog).toBe(true);
  expect(result.current.hasMore).toBe(true);
});
