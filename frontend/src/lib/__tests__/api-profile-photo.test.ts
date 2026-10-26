import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { configureAuthTransport, getProfilePhotoBlob, getToken, removeProfilePhoto, setToken, uploadProfilePhoto } from "../api";

const REVISION = "22cc6a5f-6eba-4d7c-8500-d1c6518e2a8c";
const avatar = { revision: REVISION, url: `/api/v1/auth/me/avatar?v=${REVISION}`, width: 512, height: 512 };
const emptyAvatar = { revision: null, url: null };
const file = new File(["png bytes"], "private-name.png", { type: "image/png" });
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });

describe("profile photo API", () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    localStorage.clear();
    document.cookie = "akb_dev_sso_csrf=; Max-Age=0; Path=/";
    configureAuthTransport("local"); setToken("local-session");
    fetchMock.mockReset(); vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => { setToken(null); configureAuthTransport(null); vi.unstubAllGlobals(); });

  it("uses raw-body local Bearer uploads, explicit empty revision and no filename/token URL", async () => {
    fetchMock.mockResolvedValueOnce(json({ avatar }));
    await expect(uploadProfilePhoto(file, null)).resolves.toEqual({ avatar });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/auth/me/avatar?expected_revision=none");
    expect(init.method).toBe("PUT"); expect(init.body).toBe(file); expect(init.credentials).toBe("same-origin");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer local-session");
    expect(new Headers(init.headers).get("Content-Type")).toBe("image/png");
    expect(url).not.toContain(file.name); expect(url).not.toContain("local-session");
  });

  it("uses cookie/CSRF for SSO replacement and removal without leaking a stale Bearer", async () => {
    configureAuthTransport("sso"); document.cookie = "akb_dev_sso_csrf=csrf-value; Path=/";
    fetchMock.mockResolvedValueOnce(json({ avatar })).mockResolvedValueOnce(json({ avatar: emptyAvatar }));
    await uploadProfilePhoto(file, REVISION); await removeProfilePhoto(REVISION);
    for (const [url, init] of fetchMock.mock.calls as [string, RequestInit][]) {
      expect(url).toBe(`/api/v1/auth/me/avatar?expected_revision=${REVISION}`);
      expect(init.credentials).toBe("same-origin");
      expect(new Headers(init.headers).has("Authorization")).toBe(false);
      expect(new Headers(init.headers).get("X-AKB-CSRF")).toBe("csrf-value");
    }
    expect(fetchMock.mock.calls[1][1].method).toBe("DELETE"); expect(fetchMock.mock.calls[1][1].body).toBeUndefined();
  });

  it("fetches only the authenticated exact revision endpoint with cancellation and no-store", async () => {
    const controller = new AbortController();
    fetchMock.mockResolvedValueOnce(new Response("image", { headers: { "Content-Type": "image/webp" } }));
    const blob = await getProfilePhotoBlob(REVISION, { signal: controller.signal });
    expect(blob.size).toBe(5);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`/api/v1/auth/me/avatar?v=${REVISION}`); expect(init.cache).toBe("no-store"); expect(init.signal).toBe(controller.signal);
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer local-session");
  });

  it("uses cookie-only SSO photo reads without CSRF or Authorization headers", async () => {
    configureAuthTransport("sso"); document.cookie = "akb_dev_sso_csrf=csrf-value; Path=/";
    fetchMock.mockResolvedValueOnce(new Response("image", { headers: { "Content-Type": "image/webp" } }));
    await getProfilePhotoBlob(REVISION);
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.credentials).toBe("same-origin"); expect(new Headers(init.headers).has("Authorization")).toBe(false); expect(new Headers(init.headers).has("X-AKB-CSRF")).toBe(false);
  });

  it("distinguishes unsupported, authorization, conflicts and outages instead of converting failures into success", async () => {
    for (const [status, code] of [[404, "avatar_unsupported"], [403, "avatar_forbidden"], [409, "avatar_revision_conflict"], [503, "avatar_storage_unavailable"]] as const) {
      fetchMock.mockResolvedValueOnce(json({ detail: { code, message: "Photo action unavailable" } }, status));
      await expect(removeProfilePhoto(REVISION)).rejects.toMatchObject({ status, message: "Photo action unavailable", detail: { code } });
    }
  });

  it.each([getProfilePhotoBlob, (revision: string) => removeProfilePhoto(revision)])("does not expire or redirect the session for a photo 401", async request => {
    fetchMock.mockResolvedValueOnce(json({ detail: "Unauthorized" }, 401));
    await expect(request(REVISION)).rejects.toMatchObject({ status: 401 });
    expect(getToken()).toBe("local-session");
  });

  it.each([
    {}, { avatar: {} }, { avatar: { revision: REVISION, url: "https://other.example/photo" } },
    { avatar: { revision: REVISION, url: "/unexpected/path" } },
    { avatar: { revision: REVISION, url: "http://[" } },
    { avatar: { ...avatar, width: "512" } }, { avatar: emptyAvatar },
  ])("rejects malformed write responses: %j", async body => {
    fetchMock.mockResolvedValueOnce(json(body));
    await expect(uploadProfilePhoto(file, null)).rejects.toMatchObject({ status: 502, detail: { code: "invalid_avatar_response" } });
  });

  it("does not report deletion success if the server still reports a photo", async () => {
    fetchMock.mockResolvedValueOnce(json({ avatar }));
    await expect(removeProfilePhoto(REVISION)).rejects.toMatchObject({ status: 502 });
  });

  it("preserves a removal tombstone for idempotent deletion and the next upload", async () => {
    const tombstone = { revision: REVISION, url: null };
    fetchMock.mockResolvedValueOnce(json({ avatar: tombstone })).mockResolvedValueOnce(json({ avatar: tombstone })).mockResolvedValueOnce(json({ avatar }));
    await expect(removeProfilePhoto(REVISION)).resolves.toEqual({ avatar: tombstone });
    await expect(removeProfilePhoto(tombstone.revision)).resolves.toEqual({ avatar: tombstone });
    await uploadProfilePhoto(file, tombstone.revision);
    expect(fetchMock.mock.calls[2][0]).toContain(`expected_revision=${REVISION}`);
  });

  it("rejects oversized normalized output", async () => {
    fetchMock.mockResolvedValueOnce(new Response(new Uint8Array(256 * 1024 + 1), { headers: { "Content-Type": "image/webp" } }));
    await expect(getProfilePhotoBlob(REVISION)).rejects.toMatchObject({ status: 502 });
  });

  it.each(["text/html", "image/svg+xml", "image/png"])("rejects unexpected photo bytes (%s)", async contentType => {
    fetchMock.mockResolvedValueOnce(new Response("untrusted", { headers: { "Content-Type": contentType } }));
    await expect(getProfilePhotoBlob(REVISION)).rejects.toMatchObject({ status: 502 });
  });

  it("rejects a late successful write after the account carrier changed", async () => {
    let resolve!: (response: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise<Response>(done => { resolve = done; }));
    const pending = uploadProfilePhoto(file, null);
    setToken("replacement-session"); resolve(json({ avatar }));
    await expect(pending).rejects.toMatchObject({ status: 409, detail: { code: "account_identity_changed" } });
  });

  it("rejects a late SSO photo read after the readable session cookie changed", async () => {
    configureAuthTransport("sso"); document.cookie = "akb_dev_sso_csrf=session-a; Path=/";
    let resolve!: (response: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise<Response>(done => { resolve = done; }));
    const pending = getProfilePhotoBlob(REVISION);
    document.cookie = "akb_dev_sso_csrf=session-b; Path=/"; resolve(new Response("image", { headers: { "Content-Type": "image/webp" } }));
    await expect(pending).rejects.toMatchObject({ status: 409, detail: { code: "account_identity_changed" } });
  });

  it("returns non-JSON success as a typed protocol error", async () => {
    fetchMock.mockResolvedValueOnce(new Response("not-json", { headers: { "Content-Type": "text/plain" } }));
    await expect(uploadProfilePhoto(file, null)).rejects.toMatchObject({ status: 502, detail: { code: "invalid_avatar_response" } });
  });

  it("rejects invalid revisions before sending a request", async () => {
    await expect(getProfilePhotoBlob("anything?token=secret")).rejects.toThrow("Invalid profile photo revision");
    await expect(removeProfilePhoto("anything")).rejects.toThrow("Invalid profile photo revision");
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
