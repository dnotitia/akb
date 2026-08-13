import { afterEach, describe, expect, it, vi } from "vitest";

import { AUTH_CONFIG_UNAVAILABLE, getAuthConfig } from "../api";


function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}


afterEach(() => {
  vi.unstubAllGlobals();
});


describe("getAuthConfig v1", () => {
  it("accepts an exact local-mode v1 payload", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({
      schema_version: 1,
      auth_mode: "local",
      local_auth: { enabled: true },
      keycloak: {
        enabled: false,
        browser_session_ready: false,
        login_url: null,
      },
      mcp_oauth: { enabled: true },
    })));

    expect(await getAuthConfig()).toEqual({
      available: true,
      schema_version: 1,
      auth_mode: "local",
      local_auth: { enabled: true },
      keycloak: {
        enabled: false,
        browser_session_ready: false,
        login_url: null,
      },
      mcp_oauth: { enabled: true },
    });
  });

  it.each([
    ["old shape", { local_auth: { enabled: true }, keycloak: { enabled: false } }],
    ["unknown schema", { schema_version: 2, auth_mode: "local" }],
    ["missing mode", { schema_version: 1 }],
    ["unknown mode", { schema_version: 1, auth_mode: "hybrid" }],
    [
      "additional root property",
      {
        schema_version: 1,
        auth_mode: "local",
        local_auth: { enabled: true },
        keycloak: {
          enabled: false,
          browser_session_ready: false,
          login_url: null,
        },
        mcp_oauth: { enabled: false },
        local_auth_override: true,
      },
    ],
    [
      "additional nested property",
      {
        schema_version: 1,
        auth_mode: "sso",
        local_auth: { enabled: false },
        keycloak: {
          enabled: true,
          browser_session_ready: false,
          login_url: null,
          fallback_to_local: true,
        },
        mcp_oauth: { enabled: false },
      },
    ],
    [
      "mode contradiction",
      {
        schema_version: 1,
        auth_mode: "sso",
        local_auth: { enabled: true },
        keycloak: {
          enabled: true,
          browser_session_ready: false,
          login_url: null,
        },
        mcp_oauth: { enabled: false },
      },
    ],
    [
      "local mode advertising human SSO",
      {
        schema_version: 1,
        auth_mode: "local",
        local_auth: { enabled: true },
        keycloak: {
          enabled: true,
          browser_session_ready: false,
          login_url: null,
        },
        mcp_oauth: { enabled: true },
      },
    ],
    [
      "SSO mode without its human authority",
      {
        schema_version: 1,
        auth_mode: "sso",
        local_auth: { enabled: false },
        keycloak: {
          enabled: false,
          browser_session_ready: false,
          login_url: null,
        },
        mcp_oauth: { enabled: false },
      },
    ],
    [
      "staged SSO advertising a login URL",
      {
        schema_version: 1,
        auth_mode: "sso",
        local_auth: { enabled: false },
        keycloak: {
          enabled: true,
          browser_session_ready: false,
          login_url: "/api/v1/auth/keycloak/login",
        },
        mcp_oauth: { enabled: false },
      },
    ],
    [
      "ready SSO without a login URL",
      {
        schema_version: 1,
        auth_mode: "sso",
        local_auth: { enabled: false },
        keycloak: {
          enabled: true,
          browser_session_ready: true,
          login_url: null,
        },
        mcp_oauth: { enabled: false },
      },
    ],
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
