import { afterEach, describe, expect, it, vi } from "vitest";

import {
  DocumentRevisionApiError,
  getDocumentDiff,
  getDocumentHistory,
  getDocumentHistoryWithFallback,
} from "@/lib/api";

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});
describe("document revision API", () => {
  it("loads the canonical logical document history", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({
      kind: "document_history",
      uri: "akb://demo/coll/notes/doc/guide.md",
      history: [{
        hash: "abcdef123456",
        message: "Update guide",
        author: "user-1",
        author_name: "Kim",
        date: "2026-09-02T00:00:00Z",
      }],
    }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await getDocumentHistory("demo", "notes/guide.md", 20);

    expect(result.source).toBe("document");
    expect(result.history[0].message).toBe("Update guide");
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/v1/history/demo/notes%2Fguide.md?limit=20",
    );
  });

  it("falls back to path activity only when an old server returns 404/405", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ detail: "Not Found" }, 404))
      .mockResolvedValueOnce(jsonResponse({
        vault: "demo",
        total: 1,
        activity: [{
          hash: "abcdef123456",
          subject: "Legacy update",
          agent: "legacy-user",
          author_name: "Legacy User",
          timestamp: "2026-09-01T00:00:00Z",
        }],
      }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await getDocumentHistoryWithFallback("demo", "notes/guide.md");

    expect(result.source).toBe("activity");
    expect(result.history[0]).toMatchObject({
      message: "Legacy update",
      author: "legacy-user",
      author_name: "Legacy User",
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("preserves 403 instead of mislabelling it as an unsupported server", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ detail: "Forbidden" }, 403),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      getDocumentHistoryWithFallback("demo", "notes/guide.md"),
    ).rejects.toMatchObject({
      name: "DocumentRevisionApiError",
      status: 403,
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("keeps string-detail status codes for Diff compatibility states", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ detail: "Method Not Allowed" }, 405)),
    );

    try {
      await getDocumentDiff("demo", "notes/guide.md", "abcdef1");
      throw new Error("expected getDocumentDiff to fail");
    } catch (error) {
      expect(error).toBeInstanceOf(DocumentRevisionApiError);
      expect(error).toMatchObject({ status: 405, message: "Method Not Allowed" });
    }
  });
});
