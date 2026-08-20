import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import {
  AUTH_CONFIG_UNAVAILABLE,
  getAuthConfig,
  getToken,
  setToken,
} from "@/lib/api";
import AuthPage from "../auth";


vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    authLogin: vi.fn(),
    authRegister: vi.fn(),
    setToken: vi.fn(),
    getToken: vi.fn(() => null),
    getMe: vi.fn().mockRejectedValue(new Error("no active session")),
    getAuthConfig: vi.fn(),
  };
});

const navigate = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>(
    "react-router-dom",
  );
  return { ...actual, useNavigate: () => navigate };
});

const localConfig = {
  available: true,
  schema_version: 2 as const,
  auth_mode: "local" as const,
  local_auth: { enabled: true },
  keycloak: {
    enabled: false,
    browser_session_ready: false,
  },
  providers: [],
  mcp_oauth: { enabled: false },
};

const stagedSsoConfig = {
  available: true,
  schema_version: 2 as const,
  auth_mode: "sso" as const,
  local_auth: { enabled: false },
  keycloak: {
    enabled: true,
    browser_session_ready: false,
  },
  providers: [{
    provider_type: "keycloak-oidc",
    alias: "workforce",
    display_name: "Company SSO",
    login_url: null,
  }],
  mcp_oauth: { enabled: false },
};

function renderAuth() {
  return render(
    <MemoryRouter>
      <AuthPage />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  window.history.replaceState({}, "", "/auth");
  vi.mocked(getToken).mockReturnValue(null);
});

afterEach(() => {
  cleanup();
  navigate.mockReset();
  vi.clearAllMocks();
});

describe("AuthPage mode gate", () => {
  it("renders only local login, registration, and recovery in local mode", async () => {
    vi.mocked(getAuthConfig).mockResolvedValue(localConfig);

    renderAuth();

    expect(await screen.findByLabelText(/Username/i)).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Register/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Forgot password/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /SSO/i })).toBeNull();
  });

  it("renders no local or unusable SSO controls while browser custody is staged", async () => {
    vi.mocked(getAuthConfig).mockResolvedValue(stagedSsoConfig);

    renderAuth();

    expect(await screen.findByText(/SSO browser sign-in is not available yet/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/Username/i)).toBeNull();
    expect(screen.queryByRole("tab", { name: /Register/i })).toBeNull();
    expect(screen.queryByRole("link", { name: /Forgot password/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /Company SSO/i })).toBeNull();
  });

  it("does not treat ?local=1 as an SSO-mode escape", async () => {
    window.history.replaceState({}, "", "/auth?local=1");
    vi.mocked(getAuthConfig).mockResolvedValue(stagedSsoConfig);

    renderAuth();

    expect(await screen.findByText(/SSO browser sign-in is not available yet/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/Username/i)).toBeNull();
  });

  it("clears a stale local JWT instead of redirecting it through SSO mode", async () => {
    vi.mocked(getAuthConfig).mockResolvedValue(stagedSsoConfig);
    vi.mocked(getToken).mockReturnValue("legacy-local-session");

    renderAuth();

    expect(await screen.findByText(/SSO browser sign-in is not available yet/i)).toBeInTheDocument();
    expect(setToken).toHaveBeenCalledWith(null);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("fails closed when public auth configuration is unavailable", async () => {
    vi.mocked(getAuthConfig).mockResolvedValue(AUTH_CONFIG_UNAVAILABLE);

    renderAuth();

    expect(await screen.findByText(/configuration could not be verified/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/Username/i)).toBeNull();
  });

  it("shows only a usable SSO option and never auto-redirects", async () => {
    vi.mocked(getAuthConfig).mockResolvedValue({
      ...stagedSsoConfig,
      keycloak: {
        enabled: true,
        browser_session_ready: true,
      },
      providers: [{
        ...stagedSsoConfig.providers[0],
        login_url: "/api/v1/auth/sso/workforce/login",
      }],
    });

    renderAuth();

    expect(await screen.findByRole("button", { name: /Sign in with Company SSO/i })).toBeInTheDocument();
    expect(screen.queryByLabelText(/Username/i)).toBeNull();
    expect(screen.queryByRole("button", { name: /^Sign in with SSO$/i })).toBeNull();
    expect(navigate).not.toHaveBeenCalled();
  });

  // The SSO callback is reached by a browser following a redirect, so it cannot
  // answer with an error body — whatever it returns IS the page. It sends the
  // person back here with an allowlisted reason code, and this is where that
  // code has to become a sentence. Measured before the fix: an invited person
  // waiting for approval saw a serialized error object on a blank white page.
  it("tells an invited person waiting for approval what is actually happening", async () => {
    vi.mocked(getAuthConfig).mockResolvedValue(stagedSsoConfig);
    window.history.replaceState({}, "", "/auth?sso_error=membership_required");

    renderAuth();

    expect(await screen.findByText(/not a member of this workspace yet/i)).toBeInTheDocument();
    expect(screen.getByText(/administrator has to admit you/i)).toBeInTheDocument();
  });

  it("says something generic for every other refusal, and never echoes the code", async () => {
    vi.mocked(getAuthConfig).mockResolvedValue(stagedSsoConfig);
    // The server only ever sends an allowlisted code; the page must still not
    // put an arbitrary one on screen, because that is what makes the allowlist
    // the only thing standing between a query string and rendered text.
    window.history.replaceState({}, "", "/auth?sso_error=<script>alert(1)</script>");

    renderAuth();

    expect(await screen.findByText(/did not complete/i)).toBeInTheDocument();
    expect(screen.queryByText(/alert\(1\)/)).toBeNull();
    expect(screen.queryByText(/not a member of this workspace yet/i)).toBeNull();
  });

  it("says nothing about SSO failures when the person simply arrived", async () => {
    vi.mocked(getAuthConfig).mockResolvedValue(stagedSsoConfig);
    window.history.replaceState({}, "", "/auth");

    renderAuth();

    // `browser_session_ready: false` in this fixture, so the page settles on the
    // staged-provider notice — enough to know it rendered before asserting absence.
    await screen.findByText(/SSO browser sign-in is not available yet/i);
    expect(screen.queryByText(/did not complete/i)).toBeNull();
    expect(screen.queryByText(/not a member of this workspace yet/i)).toBeNull();
  });

  it("shows setup pending rather than a local fallback when no IdP is enabled", async () => {
    vi.mocked(getAuthConfig).mockResolvedValue({
      ...stagedSsoConfig,
      providers: [],
    });

    renderAuth();

    expect(await screen.findByText(/No SSO providers are enabled/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/Username/i)).toBeNull();
  });
});
