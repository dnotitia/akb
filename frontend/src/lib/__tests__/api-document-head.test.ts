import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, configureAuthTransport, getDocument, getDocumentHead, getToken, setToken, type DocumentHead } from "@/lib/api";

const head: DocumentHead = {
  kind: "document_head",
  uri: "akb://team/coll/notes/doc/guide.md",
  vault: "team",
  path: "notes/guide.md",
  current_commit: "remote-commit",
  updated_at: "2026-10-20T00:00:00Z",
};

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

beforeEach(() => {
  configureAuthTransport("local");
  setToken(null);
});

afterEach(() => {
  setToken(null);
  configureAuthTransport(null);
  vi.unstubAllGlobals();
});

describe("document head API", () => {
  it("preserves the draft session and cancellation on full recovery reads too", async () => {
    setToken("document-recovery-test-session");
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ detail: "Unauthorized" }, 401));
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();
    const before = window.location.href;
    await expect(getDocument("team vault", "notes/회의.md", undefined, { preserveSession: true, signal: controller.signal })).rejects.toMatchObject({ status: 401 });
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/documents/team%20vault/notes%2F%ED%9A%8C%EC%9D%98.md", expect.objectContaining({ signal: controller.signal, cache: "no-store" }));
    expect(getToken()).toBe("document-recovery-test-session");
    expect(window.location.href).toBe(before);
  });
  it("reads only the encoded head endpoint and forwards cancellation", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(head));
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();

    expect(await getDocumentHead("team vault", "notes/회의 #?.md", controller.signal)).toEqual(head);

    expect(fetchMock).toHaveBeenCalledExactlyOnceWith(
      "/api/v1/document-head/team%20vault/notes%2F%ED%9A%8C%EC%9D%98%20%23%3F.md",
      expect.objectContaining({ signal: controller.signal, credentials: "same-origin", cache: "no-store" }),
    );
  });

  it.each([
    [403, { detail: "Forbidden" }, "Forbidden"],
    [404, { detail: { message: "Document no longer exists", code: "not_found" } }, "Document no longer exists"],
    [503, { detail: "Unavailable" }, "Unavailable"],
  ])("preserves status %s and its server detail", async (status, body, message) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(body, status)));

    await expect(getDocumentHead("team", "d-guide")).rejects.toMatchObject({
      name: "ApiError", status, message, detail: body.detail,
    });
  });

  it("reports a background 401 without expiring or redirecting the draft's session", async () => {
    setToken("document-head-test-session");
    const originalUrl = window.location.href;
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ detail: "Unauthorized" }, 401)));

    const failure = getDocumentHead("team", "d-guide");
    await expect(failure).rejects.toBeInstanceOf(ApiError);
    await expect(failure).rejects.toMatchObject({ status: 401 });
    expect(getToken()).toBe("document-head-test-session");
    expect(window.location.href).toBe(originalUrl);
  });

  it("keeps network and abort failures intact", async () => {
    const abort = new DOMException("Aborted", "AbortError");
    const network = new TypeError("Failed to fetch");
    vi.stubGlobal("fetch", vi.fn().mockRejectedValueOnce(abort).mockRejectedValueOnce(network));

    await expect(getDocumentHead("team", "d-guide", new AbortController().signal)).rejects.toBe(abort);
    await expect(getDocumentHead("team", "d-guide")).rejects.toBe(network);
  });
});
