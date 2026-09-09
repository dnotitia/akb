import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, getRecent } from "@/lib/api";

afterEach(() => vi.unstubAllGlobals());

describe("recent feed transport", () => {
  it.each([404, 405, 501])("preserves unsupported status %s from legacy string errors", async status => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "Not Found" }), { status })));
    await expect(getRecent(undefined, 6, { scope: "watching" })).rejects.toMatchObject({ status, name: "ApiError" });
  });
  it("passes scope and cursor without changing a legacy response", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ changes: [] })));
    vi.stubGlobal("fetch", fetch);
    await expect(getRecent("my vault", 6, { scope: "watching", cursor: "a+b=" })).resolves.toEqual({ changes: [] });
    const url = new URL(fetch.mock.calls[0][0], "http://localhost");
    expect(Object.fromEntries(url.searchParams)).toEqual({ vault: "my vault", limit: "6", scope: "watching", cursor: "a+b=" });
  });
  it("does not classify a server outage as missing support", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "Unavailable" }), { status: 503 })));
    await expect(getRecent()).rejects.not.toBeInstanceOf(ApiError);
  });
});
