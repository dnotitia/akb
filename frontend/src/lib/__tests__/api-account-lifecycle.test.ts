import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch, authSessionSnapshot, beginAccountLifecycle, clearCompletedAccountSession, configureAuthTransport, getToken, setToken } from "../api";
import { deleteAccount, getAccountLifecycle, revokeAccountSessions } from "../api-account-lifecycle";

const preview = { schema_version: 1, user_id: "a", username: "alice", revoke_sessions: { supported: true, scope: "local_sessions", includes_current: true, affects_pats: false, reason: null }, deletion: { supported: true, allowed: true, reason: null, confirmation: "username_and_current_password", blockers: [], effects: { active_pats_revoked: 2, shared_publications_preserved: 1 } } };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

beforeEach(() => { configureAuthTransport("local"); setToken("token-a"); });
afterEach(() => { document.cookie = "akb_dev_sso_csrf=; Max-Age=0; path=/"; setToken(null); configureAuthTransport(null); vi.unstubAllGlobals(); });

describe("account lifecycle safety contract", () => {
  it.each([404, 405, 403, 409, 429, 500])("retains status %s and authentication for non-JSON errors", async status => {
    const fetch = vi.fn().mockResolvedValue(new Response("<html>error</html>", { status }));
    vi.stubGlobal("fetch", fetch);
    await expect(deleteAccount(authSessionSnapshot(), "a", "alice", "secret")).rejects.toMatchObject({ status });
    expect(getToken()).toBe("token-a");
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][0]).toBe("/api/v1/my/account/deletion");
    expect(fetch.mock.calls[0][1].method).toBe("POST");
  });
  it("preserves status for 401 without expiring the session", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({}, 401)));
    await expect(getAccountLifecycle(authSessionSnapshot())).rejects.toMatchObject({ status: 401 });
    expect(getToken()).toBe("token-a");
  });
  it("validates preview counts and keeps absent effects distinct from zero", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(json(preview)).mockResolvedValueOnce(json({ ...preview, deletion: { ...preview.deletion, effects: {} } })).mockResolvedValueOnce(json({ ...preview, deletion: { ...preview.deletion, effects: { active_pats_revoked: -1 } } }));
    vi.stubGlobal("fetch", fetch);
    await expect(getAccountLifecycle(authSessionSnapshot())).resolves.toEqual(preview);
    expect((await getAccountLifecycle(authSessionSnapshot())).deletion.effects.active_pats_revoked).toBeUndefined();
    await expect(getAccountLifecycle(authSessionSnapshot())).rejects.toThrow("contract");
  });
  it.each([{ deleted: true, user_id: "b" }, { deleted: "true", user_id: "a" }, null])("rejects unverified deletion success %j", async body => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json(body)));
    await expect(deleteAccount(authSessionSnapshot(), "a", "alice", "secret")).rejects.toThrow("unknown");
    expect(getToken()).toBe("token-a");
  });
  it("requires a valid revocation receipt", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({ user_id: "a", revoked_before: "nonsense" })));
    await expect(revokeAccountSessions(authSessionSnapshot(), "a")).rejects.toThrow("unknown");
  });
  it("rejects account switches before transmission", async () => {
    const snapshot = authSessionSnapshot();
    const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
    localStorage.setItem("akb_token", "token-b");
    await expect(deleteAccount(snapshot, "a", "alice", "secret")).rejects.toMatchObject({ status: 409 });
    expect(fetch).not.toHaveBeenCalled();
  });
  it("pins the actual credential and never clears a newer login after late success", async () => {
    const snapshot = authSessionSnapshot();
    let resolve!: (response: Response) => void;
    const fetch = vi.fn().mockReturnValue(new Promise<Response>(r => { resolve = r; }));
    vi.stubGlobal("fetch", fetch);
    const request = deleteAccount(snapshot, "a", "alice", "secret");
    setToken("new-a-session");
    resolve(json({ user_id: "a", deleted: true })); await request;
    expect(new Headers(fetch.mock.calls[0][1].headers).get("Authorization")).toBe("Bearer token-a");
    expect(clearCompletedAccountSession(snapshot)).toBe(false);
    expect(getToken()).toBe("new-a-session");
  });
  it("does not let a late background 401 expire a newer account", async () => {
    let resolve!: (response: Response) => void;
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(new Promise<Response>(r => { resolve = r; })));
    const request = authenticatedFetch("/api/v1/auth/me");
    setToken("token-b"); resolve(json({}, 401));
    await expect(request).rejects.toMatchObject({ status: 401, name: "DeferredSessionError" });
    expect(getToken()).toBe("token-b");
  });
  it("defers background 401 during a lifecycle attempt and releases suppression afterward", async () => {
    const snapshot = authSessionSnapshot(); const end = beginAccountLifecycle(snapshot);
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => Promise.resolve(json({}, 401))));
    try {
      await expect(authenticatedFetch("/api/v1/auth/me", undefined, { unauthorized: "expire" })).rejects.toMatchObject({ name: "DeferredSessionError" });
      expect(getToken()).toBe("token-a");
    } finally { end(); }
    await expect(authenticatedFetch("/api/v1/auth/me", undefined, { unauthorized: "expire" })).rejects.toMatchObject({ name: "ApiError", status: 401 });
    expect(getToken()).toBeNull();
  });
});

it("bounds a hung lifecycle request without retrying or clearing authentication", async () => {
  vi.useFakeTimers();
  try {
    const fetch = vi.fn((_input, init: RequestInit) => new Promise<Response>((_resolve, reject) => {
      init.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
    }));
    vi.stubGlobal("fetch", fetch);
    const rejection = expect(deleteAccount(authSessionSnapshot(), "a", "alice", "secret")).rejects.toThrow("Aborted");
    await vi.advanceTimersByTimeAsync(30_000);
    await rejection;
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(getToken()).toBe("token-a");
    expect(vi.getTimerCount()).toBe(0);
  } finally { vi.useRealTimers(); }
});


it("uses the SSO cookie and CSRF carrier without a stale local bearer", async () => {
  configureAuthTransport("sso");
  document.cookie = "akb_dev_sso_csrf=fixture-csrf; path=/";
  const snapshot = authSessionSnapshot();
  expect(snapshot).toMatchObject({ mode: "sso", token: null });
  const fetch = vi.fn().mockResolvedValueOnce(json({ ...preview, revoke_sessions: { ...preview.revoke_sessions, scope: "sso_browser_sessions" } }))
    .mockResolvedValueOnce(json({ user_id: "a", revoked_before: "2026-09-15T00:00:00Z" }));
  vi.stubGlobal("fetch", fetch);
  try {
    expect((await getAccountLifecycle(snapshot)).revoke_sessions.scope).toBe("sso_browser_sessions");
    await revokeAccountSessions(snapshot, "a");
    expect(fetch).toHaveBeenCalledTimes(2);
    const headers = new Headers(fetch.mock.calls[1][1].headers);
    expect(headers.get("X-AKB-CSRF")).toBe("fixture-csrf");
    expect(headers.has("Authorization")).toBe(false);
    expect(fetch.mock.calls[1][1].credentials).toBe("same-origin");
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ expected_user_id: "a" });
  } finally { document.cookie = "akb_dev_sso_csrf=; Max-Age=0; path=/"; }
});

it("rejects a revocation prepared before the SSO transport mode changed", async () => {
  configureAuthTransport("sso");
  const snapshot = authSessionSnapshot();
  configureAuthTransport("local");
  setToken("replacement-local-session");
  const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
  await expect(revokeAccountSessions(snapshot, "a")).rejects.toMatchObject({ status: 409 });
  expect(fetch).not.toHaveBeenCalled();
  expect(clearCompletedAccountSession(snapshot)).toBe(false);
  expect(getToken()).toBe("replacement-local-session");
});


it("rejects an action after the same account replaces its SSO browser session", async () => {
  configureAuthTransport("sso");
  document.cookie = "akb_dev_sso_csrf=first-session; path=/";
  const snapshot = authSessionSnapshot();
  document.cookie = "akb_dev_sso_csrf=new-session; path=/";
  const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
  await expect(revokeAccountSessions(snapshot, "a")).rejects.toMatchObject({ status: 409 });
  expect(fetch).not.toHaveBeenCalled();
  expect(authSessionSnapshot().generation).not.toBe(snapshot.generation);
});

it("does not clear a replacement SSO session after late revocation success", async () => {
  configureAuthTransport("sso");
  document.cookie = "akb_dev_sso_csrf=first-session; path=/";
  const snapshot = authSessionSnapshot();
  let resolve!: (response: Response) => void;
  const fetch = vi.fn().mockReturnValue(new Promise<Response>(r => { resolve = r; }));
  vi.stubGlobal("fetch", fetch);
  const request = revokeAccountSessions(snapshot, "a");
  document.cookie = "akb_dev_sso_csrf=new-session; path=/";
  // The all-session endpoint returns no Set-Cookie deletion: old cookies become
  // inert through server revocation, and a newer login's cookies stay intact.
  resolve(json({ user_id: "a", revoked_before: "2026-09-15T00:00:00Z" }));
  await request;
  expect(new Headers(fetch.mock.calls[0][1].headers).get("X-AKB-CSRF")).toBe("first-session");
  expect(clearCompletedAccountSession(snapshot)).toBe(false);
  expect(authSessionSnapshot().csrfToken).toBe("new-session");
});

it("does not expire a replacement SSO session after a late background 401", async () => {
  configureAuthTransport("sso");
  document.cookie = "akb_dev_sso_csrf=first-session; path=/";
  let resolve!: (response: Response) => void;
  vi.stubGlobal("fetch", vi.fn().mockReturnValue(new Promise<Response>(r => { resolve = r; })));
  const request = authenticatedFetch("/api/v1/auth/me", undefined, { unauthorized: "expire" });
  document.cookie = "akb_dev_sso_csrf=new-session; path=/";
  const replacement = authSessionSnapshot();
  resolve(json({}, 401));
  await expect(request).rejects.toMatchObject({ name: "DeferredSessionError", status: 401 });
  expect(authSessionSnapshot()).toEqual(replacement);
});

it.each(["object", "headers"])("keeps an explicitly pinned CSRF header in %s form", async form => {
  configureAuthTransport("sso");
  document.cookie = "akb_dev_sso_csrf=new-session; path=/";
  const fetch = vi.fn().mockResolvedValue(json({})); vi.stubGlobal("fetch", fetch);
  const header = { "X-AKB-CSRF": "reviewed-session" };
  await authenticatedFetch("/api/v1/my/account/session-revocations", { method: "POST", headers: form === "headers" ? new Headers(header) : header });
  expect(new Headers(fetch.mock.calls[0][1].headers).get("X-AKB-CSRF")).toBe("reviewed-session");
});
