import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  discardAsset,
  clearPrivateAssetCache,
  getAssetBlob,
  getPublication,
  publicationAssetUrl,
  refreshPublicationViewGrant,
  setPublicationToken,
  setToken,
  setViewGrant,
  uploadAsset,
} from "@/lib/api";

const ASSET_ID = "6d04dc8a-0302-4a85-a314-e7485ff5a610";

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("editor image asset API", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    localStorage.clear();
    sessionStorage.clear();
    setToken("test-token");
    clearPrivateAssetCache();
  });

  afterEach(() => {
    setToken(null);
    vi.restoreAllMocks();
  });

  it("uploads the image as a raw MIME body", async () => {
    const file = new File(["image bytes"], "diagram one.png", { type: "image/png" });
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        id: ASSET_ID,
        url: `/api/assets/${ASSET_ID}`,
        name: file.name,
        mime_type: file.type,
        size_bytes: file.size,
      }),
    );

    const result = await uploadAsset("team vault", file);

    expect(result.id).toBe(ASSET_ID);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/assets/team%20vault?filename=diagram+one.png");
    expect(init.method).toBe("POST");
    expect(init.body).toBe(file);
    expect(init.headers).toEqual({
      "Content-Type": "image/png",
      Authorization: "Bearer test-token",
    });
  });

  it("fetches private image bytes with Bearer auth", async () => {
    const blob = new Blob(["image bytes"], { type: "image/png" });
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      blob: vi.fn().mockResolvedValue(blob),
    } as unknown as Response);

    await expect(getAssetBlob(ASSET_ID, "team vault")).resolves.toEqual(blob);
    expect(fetchMock).toHaveBeenCalledWith(`/api/assets/${ASSET_ID}?vault=team+vault`, {
      headers: { Authorization: "Bearer test-token" },
      signal: expect.any(AbortSignal),
    });
  });

  it("scopes a historical image read to its document revision", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      blob: vi.fn().mockResolvedValue(new Blob(["history"])),
    } as unknown as Response);

    await getAssetBlob(ASSET_ID, "team", undefined, {
      document: "notes/weekly.md",
      commit: "abcdef123456", // pragma: allowlist secret — synthetic Git commit
    });

    expect(fetchMock).toHaveBeenCalledWith(
      `/api/assets/${ASSET_ID}?vault=team&document=notes%2Fweekly.md&commit=abcdef123456`, // pragma: allowlist secret — synthetic Git commit
      {
        headers: { Authorization: "Bearer test-token" },
        signal: expect.any(AbortSignal),
      },
    );
  });

  it("reuses bounded private image bytes within the same auth session", async () => {
    const blob = new Blob(["cached image"], { type: "image/png" });
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      blob: vi.fn().mockResolvedValue(blob),
    } as unknown as Response);

    await expect(getAssetBlob(ASSET_ID, "team")).resolves.toBe(blob);
    await expect(getAssetBlob(ASSET_ID, "team")).resolves.toBe(blob);

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("discards an uncommitted upload through the vault-scoped endpoint", async () => {
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));

    await expect(discardAsset("team vault", ASSET_ID)).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/assets/team%20vault/${ASSET_ID}`,
      {
        method: "DELETE",
        headers: { Authorization: "Bearer test-token" },
        keepalive: true,
      },
    );
  });

  it("carries the publication token and view grant in asset URLs", () => {
    setPublicationToken("public slug", "password token");
    setViewGrant("public slug", "view grant");

    expect(publicationAssetUrl("public slug", ASSET_ID)).toBe(
      `/api/v1/public/public%20slug/assets/${ASSET_ID}?token=password+token&grant=view+grant`,
    );
  });

  it("uses the page session grant when refreshing a legacy fetch grant", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({
        resource_type: "document",
        content: "![image](/api/assets/id)",
        view_grant: "1000.legacy",
        view_grant_session: "1000.4600.session",
      }))
      .mockResolvedValueOnce(jsonResponse({
        view_grant: "1601.2201.renewed",
        view_grant_session: "1000.4600.rotated",
      }));

    await getPublication("share");
    await refreshPublicationViewGrant("share");

    expect(fetchMock.mock.calls[1]?.[0]).toBe(
      "/api/v1/public/share/grant?grant=1000.4600.session",
    );
    expect(publicationAssetUrl("share", ASSET_ID)).toContain(
      "grant=1601.2201.renewed",
    );
  });
});
