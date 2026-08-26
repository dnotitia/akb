import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import {
  getAuthConfig,
  keycloakExchange,
  markLegacySsoSession,
  setToken,
} from "@/lib/api";
import AuthCallbackPage from "../auth-callback";

vi.mock("@/lib/api", () => ({
  getAuthConfig: vi.fn(),
  keycloakExchange: vi.fn(),
  markLegacySsoSession: vi.fn(),
  setToken: vi.fn(),
}));

const navigate = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>(
    "react-router-dom",
  );
  return { ...actual, useNavigate: () => navigate };
});

const v2Config = {
  available: true,
  schema_version: 2 as const,
  auth_mode: "sso" as const,
  local_auth: { enabled: false },
  keycloak: { enabled: true, browser_session_ready: true },
  providers: [],
  mcp_oauth: { enabled: true },
};

const legacyHybridConfig = {
  available: true,
  schema_version: 1 as const,
  auth_mode: "hybrid" as const,
  local_auth: { enabled: true },
  keycloak: { enabled: true, browser_session_ready: false },
  providers: [{
    provider_type: "legacy-keycloak-oidc",
    alias: "legacy-keycloak",
    display_name: "SSO",
    login_url: "/api/v1/auth/keycloak/login",
  }],
  mcp_oauth: { enabled: true },
};

beforeEach(() => {
  window.history.replaceState({}, "", "/auth/callback?code=legacy-code");
  vi.mocked(getAuthConfig).mockReset();
  vi.mocked(keycloakExchange).mockReset();
  vi.mocked(markLegacySsoSession).mockReset();
  vi.mocked(setToken).mockReset();
  navigate.mockReset();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("AuthCallbackPage", () => {
  it("keeps the browser exchange retired for current v2 SSO", async () => {
    vi.mocked(getAuthConfig).mockResolvedValue(v2Config);

    render(
      <MemoryRouter initialEntries={["/auth/callback?code=legacy-code"]}>
        <AuthCallbackPage />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", {
        name: /Legacy SSO callback retired/i,
      }),
    ).toBeInTheDocument();
    expect(keycloakExchange).not.toHaveBeenCalled();
    expect(setToken).not.toHaveBeenCalled();
  });

  it("exchanges a one-time code only for the validated legacy hybrid contract", async () => {
    window.history.replaceState(
      {},
      "",
      "/auth/callback?code=legacy-code&redirect=%2Fvault%2Fdemo",
    );
    vi.mocked(getAuthConfig).mockResolvedValue(legacyHybridConfig);
    vi.mocked(keycloakExchange).mockResolvedValue({
      token: "legacy-akb-jwt",
      kc_id_token: "legacy-keycloak-id-token",
    });

    render(
      <MemoryRouter>
        <AuthCallbackPage />
      </MemoryRouter>,
    );

    expect(await screen.findByText(/Completing sign-in/i)).toBeInTheDocument();
    await waitFor(() => {
      expect(keycloakExchange).toHaveBeenCalledWith("legacy-code");
      expect(setToken).toHaveBeenCalledWith("legacy-akb-jwt");
      expect(markLegacySsoSession).toHaveBeenCalledWith(
        "legacy-keycloak-id-token",
      );
      expect(navigate).toHaveBeenCalledWith("/vault/demo", { replace: true });
    });
  });

  it("rejects a malformed legacy exchange without storing a credential", async () => {
    vi.mocked(getAuthConfig).mockResolvedValue(legacyHybridConfig);
    vi.mocked(keycloakExchange).mockResolvedValue({ token: "" });

    render(
      <MemoryRouter>
        <AuthCallbackPage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(navigate).toHaveBeenCalledWith(
        "/auth?sso_error=exchange_failed",
        { replace: true },
      );
    });
    expect(setToken).not.toHaveBeenCalled();
    expect(markLegacySsoSession).not.toHaveBeenCalled();
  });
});
