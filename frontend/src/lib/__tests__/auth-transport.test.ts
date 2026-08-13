import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  authenticatedFetch,
  configureAuthTransport,
  createPAT,
  getAuthConfig,
  getAssetBlob,
  getMe,
  getToken,
  logoutOrdinarySession,
  setToken,
} from "../api";


const localConfig = {
  schema_version: 2,
  auth_mode: "local",
  local_auth: { enabled: true },
  keycloak: { enabled: false, browser_session_ready: false },
  providers: [],
  mcp_oauth: { enabled: false },
};

const ssoConfig = {
  schema_version: 2,
  auth_mode: "sso",
  local_auth: { enabled: false },
  keycloak: { enabled: true, browser_session_ready: true },
  providers: [{
    provider_type: "keycloak-oidc",
    alias: "workforce",
    display_name: "Company SSO",
    login_url: "/api/v1/auth/sso/workforce/login",
  }],
  mcp_oauth: { enabled: true },
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function headersAt(fetchMock: ReturnType<typeof vi.fn>, index: number): Headers {
  return new Headers((fetchMock.mock.calls[index]?.[1] as RequestInit | undefined)?.headers);
}

describe("ordinary browser auth transport", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    configureAuthTransport(null);
    setToken(null);
    localStorage.clear();
    document.cookie = "akb_dev_sso_csrf=; Max-Age=0; Path=/";
  });

  afterEach(() => {
    configureAuthTransport(null);
    setToken(null);
    vi.unstubAllGlobals();
  });

  it("uses only the local session Bearer token in local mode", async () => {
    setToken("local-session-jwt");
    fetchMock
      .mockResolvedValueOnce(jsonResponse(localConfig))
      .mockResolvedValueOnce(jsonResponse({ username: "alice" }));

    await getAuthConfig();
    await getMe({ redirectOnUnauthorized: false });

    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/v1/auth/me");
    expect((fetchMock.mock.calls[1]?.[1] as RequestInit).credentials).toBe("same-origin");
    expect(headersAt(fetchMock, 1).get("Authorization")).toBe("Bearer local-session-jwt");
    expect(headersAt(fetchMock, 1).has("X-AKB-CSRF")).toBe(false);
  });

  it("does not guess that an unverified auth mode may use a stored local token", async () => {
    setToken("stale-unverified-session");
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));

    await authenticatedFetch("/api/v1/example");

    expect(headersAt(fetchMock, 0).has("Authorization")).toBe(false);
  });

  it("removes a stale local JWT and uses only the HttpOnly cookie in SSO mode", async () => {
    setToken("stale-local-session");
    fetchMock
      .mockResolvedValueOnce(jsonResponse(ssoConfig))
      .mockResolvedValueOnce(jsonResponse({ username: "alice" }));

    await getAuthConfig();
    await getMe({ redirectOnUnauthorized: false });

    expect(getToken()).toBeNull();
    expect(localStorage.getItem("akb_token")).toBeNull();
    expect((fetchMock.mock.calls[1]?.[1] as RequestInit).credentials).toBe("same-origin");
    expect(headersAt(fetchMock, 1).has("Authorization")).toBe(false);
    expect(headersAt(fetchMock, 1).has("X-AKB-CSRF")).toBe(false);
  });

  it("keeps cookie-only SSO available when browser storage is blocked", async () => {
    const blocked = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("Storage blocked", "SecurityError");
    });
    fetchMock
      .mockResolvedValueOnce(jsonResponse(ssoConfig))
      .mockResolvedValueOnce(jsonResponse({ username: "alice" }));
    try {
      const config = await getAuthConfig();
      await getMe({ redirectOnUnauthorized: false });

      expect(config.available).toBe(true);
      expect(headersAt(fetchMock, 1).has("Authorization")).toBe(false);
      expect((fetchMock.mock.calls[1]?.[1] as RequestInit).credentials).toBe("same-origin");
    } finally {
      blocked.mockRestore();
    }
  });

  it("adds the readable CSRF cookie only to cookie-backed SSO mutations", async () => {
    configureAuthTransport("sso");
    document.cookie = "akb_dev_sso_csrf=csrf-value; Path=/; SameSite=Lax";
    fetchMock.mockResolvedValueOnce(jsonResponse({ id: "pat-1" }));

    await createPAT("automation");

    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("same-origin");
    expect(headersAt(fetchMock, 0).has("Authorization")).toBe(false);
    expect(headersAt(fetchMock, 0).get("X-AKB-CSRF")).toBe("csrf-value");
  });

  it("preserves an explicit Bearer credential without mixing in cookie CSRF", async () => {
    configureAuthTransport("sso");
    document.cookie = "akb_dev_sso_csrf=csrf-value; Path=/; SameSite=Lax";
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));

    await authenticatedFetch("/api/v1/example", {
      method: "POST",
      headers: { Authorization: "Bearer akb_pat" },
    });

    expect(headersAt(fetchMock, 0).get("Authorization")).toBe("Bearer akb_pat");
    expect(headersAt(fetchMock, 0).has("X-AKB-CSRF")).toBe(false);
  });

  it("rejects a cross-origin target before attaching any browser credential", async () => {
    configureAuthTransport("local");
    setToken("local-session-jwt");

    await expect(
      authenticatedFetch("https://attacker.example/collect"),
    ).rejects.toThrow("same-origin");

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("ends an SSO session through the CSRF-protected server logout", async () => {
    configureAuthTransport("sso");
    document.cookie = "akb_dev_sso_csrf=csrf-value; Path=/; SameSite=Lax";
    fetchMock.mockResolvedValueOnce(jsonResponse({
      logout_url: "https://id.example/realms/akb/logout?state=server-owned",
    }));

    await expect(logoutOrdinarySession()).resolves.toEqual({
      mode: "sso",
      logout_url: "https://id.example/realms/akb/logout?state=server-owned",
    });

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/v1/auth/logout");
    expect((fetchMock.mock.calls[0]?.[1] as RequestInit).method).toBe("POST");
    expect(headersAt(fetchMock, 0).get("X-AKB-CSRF")).toBe("csrf-value");
  });

  it("rejects a non-HTTP logout navigation returned by the server", async () => {
    configureAuthTransport("sso");
    document.cookie = "akb_dev_sso_csrf=csrf-value; Path=/; SameSite=Lax";
    fetchMock.mockResolvedValueOnce(jsonResponse({
      logout_url: "javascript:alert(document.domain)",
    }));

    await expect(logoutOrdinarySession()).rejects.toThrow(
      "Invalid SSO logout response",
    );
  });

  it("ends a local session without calling the SSO logout endpoint", async () => {
    configureAuthTransport("local");
    setToken("local-session-jwt");

    await expect(logoutOrdinarySession()).resolves.toEqual({
      mode: "local",
      logout_url: "/auth",
    });

    expect(getToken()).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not reuse a private blob cache across opaque SSO cookie sessions", async () => {
    configureAuthTransport("sso");
    fetchMock.mockImplementation(
      async () => new Response("private", { status: 200 }),
    );

    await getAssetBlob("file-1", "vault-1");
    await getAssetBlob("file-1", "vault-1");

    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
