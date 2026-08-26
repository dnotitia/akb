import { afterEach, describe, expect, it, vi } from "vitest";

import { AUTH_CONFIG_UNAVAILABLE, getAuthConfig } from "../api";


function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const local = {
  schema_version: 2,
  auth_mode: "local",
  local_auth: { enabled: true },
  keycloak: { enabled: false, browser_session_ready: false },
  providers: [],
  mcp_oauth: { enabled: true },
};

const stagedSso = {
  schema_version: 2,
  auth_mode: "sso",
  local_auth: { enabled: false },
  keycloak: { enabled: true, browser_session_ready: false },
  providers: [{
    provider_type: "keycloak-oidc",
    alias: "workforce",
    display_name: "Company SSO",
    login_url: null,
  }],
  mcp_oauth: { enabled: false },
};

const legacyHybrid = {
  local_auth: { enabled: true },
  keycloak: {
    enabled: true,
    enrollment_mode: "open",
    login_url: "/api/v1/auth/keycloak/login",
    sso_only: false,
  },
  mcp_oauth: { enabled: true },
};


afterEach(() => {
  vi.unstubAllGlobals();
});


describe("getAuthConfig v2", () => {
  it("accepts an exact local-mode v2 payload", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(local)));

    expect(await getAuthConfig()).toEqual({
      available: true,
      ...local,
    });
  });

  it("accepts named providers only when every login URL is server-owned", async () => {
    const ready = {
      ...stagedSso,
      keycloak: { enabled: true, browser_session_ready: true },
      providers: [{
        ...stagedSso.providers[0],
        login_url: "/api/v1/auth/sso/workforce/login",
      }],
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(ready)));

    expect(await getAuthConfig()).toEqual({ available: true, ...ready });
  });

  it("adapts only the exact legacy hybrid payload to Bearer-backed compatibility mode", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(legacyHybrid)));

    expect(await getAuthConfig()).toEqual({
      available: true,
      schema_version: 1,
      auth_mode: "hybrid",
      local_auth: { enabled: true },
      keycloak: { enabled: true, browser_session_ready: false },
      providers: [{
        provider_type: "legacy-keycloak-oidc",
        alias: "legacy-keycloak",
        display_name: "SSO",
        login_url: "/api/v1/auth/keycloak/login",
      }],
      mcp_oauth: { enabled: true },
    });
  });

  it.each([
    ["external Keycloak URL", {
      ...legacyHybrid,
      keycloak: { ...legacyHybrid.keycloak, login_url: "https://id.example/login" },
    }],
    ["extra root capability", { ...legacyHybrid, debug: true }],
    ["unknown enrollment policy", {
      ...legacyHybrid,
      keycloak: { ...legacyHybrid.keycloak, enrollment_mode: "anything" },
    }],
    ["SSO-only without Keycloak", {
      ...legacyHybrid,
      keycloak: {
        ...legacyHybrid.keycloak,
        enabled: false,
        login_url: null,
        sso_only: true,
      },
    }],
  ])("fails closed for legacy payload with %s", async (_name, body) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(body)));

    expect(await getAuthConfig()).toEqual(AUTH_CONFIG_UNAVAILABLE);
  });

  it.each([
    ["old v1 shape", {
      schema_version: 1,
      auth_mode: "local",
      local_auth: { enabled: true },
      keycloak: { enabled: false, browser_session_ready: false, login_url: null },
      mcp_oauth: { enabled: false },
    }],
    ["missing mode", { ...local, auth_mode: undefined }],
    ["unknown mode", { ...local, auth_mode: "hybrid" }],
    ["additional root property", { ...local, local_auth_override: true }],
    ["additional nested property", {
      ...stagedSso,
      keycloak: { ...stagedSso.keycloak, fallback_to_local: true },
    }],
    ["mode contradiction", {
      ...stagedSso,
      local_auth: { enabled: true },
    }],
    ["local mode advertising human SSO", {
      ...local,
      keycloak: { enabled: true, browser_session_ready: false },
    }],
    ["local mode advertising providers", {
      ...local,
      providers: stagedSso.providers,
    }],
    ["SSO mode without its broker authority", {
      ...stagedSso,
      keycloak: { enabled: false, browser_session_ready: false },
    }],
    ["staged provider advertising a login URL", {
      ...stagedSso,
      providers: [{
        ...stagedSso.providers[0],
        login_url: "/api/v1/auth/sso/workforce/login",
      }],
    }],
    ["ready provider with an external URL", {
      ...stagedSso,
      keycloak: { enabled: true, browser_session_ready: true },
      providers: [{
        ...stagedSso.providers[0],
        login_url: "https://attacker.example/login",
      }],
    }],
    ["ready provider without a login URL", {
      ...stagedSso,
      keycloak: { enabled: true, browser_session_ready: true },
    }],
    ["duplicate provider alias", {
      ...stagedSso,
      providers: [stagedSso.providers[0], stagedSso.providers[0]],
    }],
    ["provider with an extra property", {
      ...stagedSso,
      providers: [{ ...stagedSso.providers[0], client_id: "must-not-be-public" }],
    }],
  ])("fails closed for %s", async (_name, body) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(body)));

    expect(await getAuthConfig()).toEqual(AUTH_CONFIG_UNAVAILABLE);
    expect((await getAuthConfig()).local_auth.enabled).toBe(false);
  });

  it.each([
    ["non-2xx", vi.fn().mockResolvedValue(response({ error: "down" }, 503))],
    ["fetch error", vi.fn().mockRejectedValue(new Error("offline"))],
    ["invalid json", vi.fn().mockResolvedValue(new Response("not-json"))],
  ])("fails closed for %s", async (_name, fetchMock) => {
    vi.stubGlobal("fetch", fetchMock);

    expect(await getAuthConfig()).toEqual(AUTH_CONFIG_UNAVAILABLE);
  });
});
