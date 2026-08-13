import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ADMIN_AUTH_CONFIG_UNAVAILABLE,
  getAdminAuthConfig,
} from "../api";


function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}


afterEach(() => {
  vi.unstubAllGlobals();
});


describe("getAdminAuthConfig v1", () => {
  it("accepts exactly one local admin login surface", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({
      schema_version: 1,
      auth_mode: "local",
      local: {
        enabled: true,
        login_url: "/api/v1/admin/auth/local/login",
      },
      keycloak: { enabled: false, login_url: null },
    })));

    expect(await getAdminAuthConfig()).toEqual({
      available: true,
      schema_version: 1,
      auth_mode: "local",
      local: {
        enabled: true,
        login_url: "/api/v1/admin/auth/local/login",
      },
      keycloak: { enabled: false, login_url: null },
    });
  });

  it.each([
    ["both enabled", {
      schema_version: 1,
      auth_mode: "sso",
      local: { enabled: true, login_url: "/local" },
      keycloak: { enabled: true, login_url: "/sso" },
    }],
    ["wrong-mode URL", {
      schema_version: 1,
      auth_mode: "local",
      local: { enabled: true, login_url: "/api/v1/admin/auth/local/login" },
      keycloak: { enabled: false, login_url: "/hidden-sso" },
    }],
    ["unknown property", {
      schema_version: 1,
      auth_mode: "sso",
      local: { enabled: false, login_url: null },
      keycloak: { enabled: true, login_url: "/api/v1/admin/auth/keycloak/login" },
      local_fallback: true,
    }],
  ])("fails closed for %s", async (_name, body) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(body)));
    expect(await getAdminAuthConfig()).toEqual(ADMIN_AUTH_CONFIG_UNAVAILABLE);
  });
});
