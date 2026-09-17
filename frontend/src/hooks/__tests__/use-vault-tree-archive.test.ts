import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { browseVault, type ArchiveScope } from "@/lib/api";
import { useVaultTree } from "../use-vault-tree";

vi.mock("@/lib/api", () => ({ browseVault: vi.fn() }));
const browse = vi.mocked(browseVault);
const response = (scope?: ArchiveScope) => ({
  vault: "v", path: "", archive_scope: scope,
  items: [{ type: "document", name: scope ?? "Current", path: "guides/doc.md", status: scope === "archived" ? "archived" : "active" }],
});
beforeEach(() => { vi.clearAllMocks(); browse.mockResolvedValue(response()); });
afterEach(cleanup);

describe("archive-aware Vault tree", () => {
  it("keeps the old browse call for default consumers and refetches on a status change", async () => {
    const { result } = renderHook(() => useVaultTree("v"));
    await waitFor(() => expect(result.current.tree).not.toBeNull());
    expect(browse).toHaveBeenCalledWith("v", undefined, -1);
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
    expect(result.current.showingPreviousScope).toBe(false);
    expect(result.current.tree?.[0].children?.[0].raw.status).toBe("archived");
    expect(browse).toHaveBeenLastCalledWith("v", undefined, -1, { archive_scope: "archived" });
  });

  it("does not expose an ignored scope as a valid tree", async () => {
    const { result } = renderHook(() => useVaultTree("v", "archived"));
    await waitFor(() => expect(result.current.unsupported).toBe(true));
    expect(result.current.tree).toEqual([]);
    expect(result.current.loading).toBe(false);
  });
});
