import { afterEach, expect, it, vi } from "vitest";
import { ApiError, browseVault, grepDocs } from "@/lib/api";

afterEach(() => vi.restoreAllMocks());

it("sends bounded scope/cursor options and retains explicit completeness", async () => {
  const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
    vault: "Team space", path: "guides", items: [], scanned: 100, complete: false, next_cursor: "next-position",
  }), { status: 200 }));
  const result = await browseVault("Team space", "guides", 0, {
    page_size: 100, cursor: "previous-position", archive_scope: "all", content_type: "files",
  });
  const url = new URL(String(fetch.mock.calls[0][0]), "http://localhost");
  expect(url.pathname).toBe("/api/v1/browse/Team%20space");
  expect(Object.fromEntries(url.searchParams)).toEqual({
    depth: "0", collection: "guides", page_size: "100", cursor: "previous-position", archive_scope: "all", content_type: "files",
  });
  expect(result.items).toEqual([]);
  expect(result.complete).toBe(false);
  expect(result.next_cursor).toBe("next-position");
});

it("preserves a protective top-level REST code rather than converting it to an outage", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
    error: "Candidate scan is too large", code: "grep_scope_too_large", hint: "Narrow collection",
  }), { status: 422 }));
  const error = await grepDocs("needle", "team", 1).catch(error => error);
  expect(error).toBeInstanceOf(ApiError);
  expect(error.status).toBe(422);
  expect(error.detail.code).toBe("grep_scope_too_large");
});

it("encodes literal global name queries and forwards cancellation", async () => {
  const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ items: [], complete: true, name_query: "팀 %_ & title" }), { status: 200 }));
  const controller = new AbortController();
  await browseVault("team", "guides", -1, { page_size: 100, content_type: "resources", name_query: "팀 %_ & title", signal: controller.signal });
  const url = new URL(String(fetch.mock.calls[0][0]), "http://localhost");
  expect(url.searchParams.get("name_query")).toBe("팀 %_ & title");
  expect(url.searchParams.get("content_type")).toBe("resources");
  expect(fetch.mock.calls[0][1]?.signal).toBe(controller.signal);
});
