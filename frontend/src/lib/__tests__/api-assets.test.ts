import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  discardAsset,
  clearPrivateAssetCache,
  configureAuthTransport,
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
    configureAuthTransport("local");
    setToken("test-token");
    clearPrivateAssetCache();
  });

  afterEach(() => {
    setToken(null);
    configureAuthTransport(null);
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
      credentials: "same-origin",
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
        credentials: "same-origin",
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
        credentials: "same-origin",
        headers: { Authorization: "Bearer test-token" },
        keepalive: true,
      },
    );
  });

  it("does not expire the whole session when best-effort discard gets 401", async () => {
    let finishImage: ((response: Response) => void) | undefined;
    let imageSignal: AbortSignal | undefined;
    fetchMock
      .mockImplementationOnce((_url: string, init?: RequestInit) => {
        imageSignal = init?.signal as AbortSignal;
        return new Promise<Response>((resolve) => { finishImage = resolve; });
      })
      .mockResolvedValueOnce(new Response(null, { status: 401 }));

    const image = getAssetBlob(ASSET_ID, "team");
    await expect(discardAsset("team", ASSET_ID)).rejects.toThrow("Unauthorized");

    expect(localStorage.getItem("akb_token")).toBe("test-token");
    expect(imageSignal?.aborted).toBe(false);
    finishImage?.({
      ok: true,
      status: 200,
      blob: vi.fn().mockResolvedValue(new Blob(["image"])),
    } as unknown as Response);
    await expect(image).resolves.toBeInstanceOf(Blob);
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
        view_grant: "1601.renewed",
        view_grant_session: "1601.5201.rotated",
      }))
      .mockResolvedValueOnce(jsonResponse({
        view_grant: "2202.renewed-again",
        view_grant_session: "2202.5802.rotated-again",
      }));

    await getPublication("share");
    await refreshPublicationViewGrant("share");

    expect(fetchMock.mock.calls[1]?.[0]).toBe(
      "/api/v1/public/share/grant?grant=1000.4600.session",
    );
    expect(publicationAssetUrl("share", ASSET_ID)).toContain(
      "grant=1601.renewed",
    );

    await refreshPublicationViewGrant("share");
    expect(fetchMock.mock.calls[2]?.[0]).toBe(
      "/api/v1/public/share/grant?grant=1601.5201.rotated",
    );
  });

  it("deduplicates concurrent refreshes for every image in one publication", async () => {
    setViewGrant("share", "1000.legacy");
    let finish: ((response: Response) => void) | undefined;
    fetchMock.mockImplementationOnce(
      () => new Promise<Response>((resolve) => { finish = resolve; }),
    );

    const first = refreshPublicationViewGrant("share");
    const second = refreshPublicationViewGrant("share");
    expect(fetchMock).toHaveBeenCalledTimes(1);

    finish?.(jsonResponse({
      view_grant: "1601.renewed",
      view_grant_session: "1601.5201.session",
    }));
    await expect(Promise.all([first, second])).resolves.toEqual([
      "1601.renewed",
      "1601.renewed",
    ]);
  });
});
