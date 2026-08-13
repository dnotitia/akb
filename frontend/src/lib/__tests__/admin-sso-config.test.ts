import { afterEach, describe, expect, it, vi } from "vitest";

import {
  configureAdminSsoProvider,
  getAdminSsoCatalog,
  parseAdminSsoCatalog,
  setAdminSsoProviderEnabled,
} from "../api";


const provider = {
  provider_type: "keycloak-oidc",
  alias: "workforce",
  display_name: "Company SSO",
  state: "configured_disabled",
  enabled: false,
  issuer: "https://accounts.example.com/realms/workforce",
  discovery_url: "https://accounts.example.com/realms/workforce/.well-known/openid-configuration",
  client_id: "akb-broker",
  client_secret_configured: true,
  redirect_uri: "https://auth.akb.example.com/realms/akb/broker/workforce/endpoint",
  capabilities: {
    supports_logout: true,
    supports_identity_migration: true,
  },
};

const catalog = {
  schema_version: 1,
  auth_mode: "sso",
  control_mode: "direct",
  supported_provider_types: ["keycloak-oidc"],
  providers: [provider],
};

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  document.cookie = "akb_admin_csrf=; Max-Age=0; Path=/";
});

describe("admin SSO provider API", () => {
  it("strictly parses the versioned secret-free catalog", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => Promise.resolve(response(catalog))));

    expect(await getAdminSsoCatalog()).toEqual(catalog);
    expect(JSON.stringify(await getAdminSsoCatalog())).not.toContain("client_secret\"");
  });

  it.each([
    ["extra root field", { ...catalog, arbitrary: true }],
    ["delegated catalog with provider data", { ...catalog, control_mode: "delegated" }],
    ["duplicate alias", { ...catalog, providers: [provider, provider] }],
    ["provider type outside registry", {
      ...catalog,
      providers: [{ ...provider, provider_type: "google" }],
    }],
    ["contradictory enabled state", {
      ...catalog,
      providers: [{ ...provider, state: "enabled", enabled: false }],
    }],
    ["exact state without a configured secret", {
      ...catalog,
      providers: [{ ...provider, client_secret_configured: false }], // pragma: allowlist secret
    }],
    ["unsafe display label", {
      ...catalog,
      providers: [{ ...provider, display_name: "unsafe\nlabel" }],
    }],
    ["provider secret field", {
      ...catalog,
      providers: [{ ...provider, client_secret: "leaked" }], // pragma: allowlist secret
    }],
  ])("rejects %s", (_name, value) => {
    expect(() => parseAdminSsoCatalog(value)).toThrow(/Invalid SSO provider/);
  });

  it("sends a write-only secret with same-origin CSRF and never expects it back", async () => {
    document.cookie = "akb_admin_csrf=csrf-proof-value; Path=/";
    const fetchMock = vi.fn().mockResolvedValue(response({ provider }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await configureAdminSsoProvider("workforce", {
      provider_type: "keycloak-oidc",
      display_name: "Company SSO",
      issuer: provider.issuer,
      discovery_url: provider.discovery_url,
      client_id: "akb-broker",
      client_secret: "write-only-secret", // pragma: allowlist secret
    });

    const [, init] = fetchMock.mock.calls[0];
    expect(init.method).toBe("PUT");
    expect(init.credentials).toBe("same-origin");
    expect((init.headers as Headers).get("X-AKB-Admin-CSRF")).toBe("csrf-proof-value");
    expect(JSON.parse(init.body)).toMatchObject({ client_secret: "write-only-secret" }); // pragma: allowlist secret
    expect(JSON.stringify(result)).not.toContain("write-only-secret");
  });

  it("uses an explicit enable endpoint with no body", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({
      provider: { ...provider, state: "enabled", enabled: true },
    }));
    vi.stubGlobal("fetch", fetchMock);

    await setAdminSsoProviderEnabled("workforce", true);

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/admin/sso/providers/workforce/enable",
      expect.objectContaining({ method: "POST" }),
    );
    expect(fetchMock.mock.calls[0][1]).not.toHaveProperty("body");
  });
});
