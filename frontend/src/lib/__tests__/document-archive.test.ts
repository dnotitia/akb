import { afterEach, describe, expect, it, vi } from "vitest";
import { changeDocumentArchiveState, documentArchiveDisabledReason } from "@/lib/document-archive";
import { getDocument, updateDocument } from "@/lib/api";

vi.mock("@/lib/api", () => ({ getDocument: vi.fn(), updateDocument: vi.fn() }));
afterEach(() => vi.resetAllMocks());

describe("document archive permissions", () => {
  const allowed = { role: "writer", readOnly: false, historical: false, guide: false };
  it("allows writers on ordinary current documents", () => {
    expect(documentArchiveDisabledReason(allowed)).toBeUndefined();
  });
  it.each([
    [{ role: "reader" }, "Writer"],
    [{ readOnly: true }, "read-only"],
    [{ historical: true }, "latest version"],
    [{ guide: true }, "owner"],
  ])("explains unavailable actions: %j", (overrides, reason) => {
    expect(documentArchiveDisabledReason({ ...allowed, ...overrides })).toContain(reason);
  });
  it("lets only an owner change a guide", () => {
    expect(documentArchiveDisabledReason({ ...allowed, role: "owner", guide: true })).toBeUndefined();
  });
});

describe("verified archive mutations", () => {
  it.each(["active", "archived"] as const)("patches only status and concurrency token for %s", async (status) => {
    vi.mocked(updateDocument).mockResolvedValue({} as never);
    vi.mocked(getDocument).mockResolvedValue({ status, path: "research/a.md", title: "A" } as never);
    const listener = vi.fn();
    window.addEventListener("akb:document-status-changed", listener);
    try {
      const result = await changeDocumentArchiveState("team", "research/a.md", status, "commit1");
      expect(updateDocument).toHaveBeenCalledWith("team", "research/a.md", { status, expected_commit: "commit1" });
      expect(result.path).toBe("research/a.md");
      expect(listener).toHaveBeenCalledOnce();
    } finally {
      window.removeEventListener("akb:document-status-changed", listener);
    }
  });
  it("does not claim success when an older server ignores status", async () => {
    vi.mocked(updateDocument).mockResolvedValue({} as never);
    vi.mocked(getDocument).mockResolvedValue({ status: "active" } as never);
    await expect(changeDocumentArchiveState("team", "a.md", "archived")).rejects.toThrow("could not be verified");
  });
  it("preserves conflict failure without retrying or reading a false success", async () => {
    vi.mocked(updateDocument).mockRejectedValue(new Error("Document changed. Reload before trying again."));
    await expect(changeDocumentArchiveState("team", "a.md", "archived", "old")).rejects.toThrow("Reload");
    expect(updateDocument).toHaveBeenCalledOnce();
    expect(getDocument).not.toHaveBeenCalled();
  });
});
