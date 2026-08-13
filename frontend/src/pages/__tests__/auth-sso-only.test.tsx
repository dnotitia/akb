import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { AUTH_CONFIG_UNAVAILABLE, getAuthConfig } from "@/lib/api";
import AuthPage from "../auth";


vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    authLogin: vi.fn(),
    authRegister: vi.fn(),
    setToken: vi.fn(),
    getToken: vi.fn(() => null),
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
  schema_version: 1 as const,
  auth_mode: "local" as const,
  local_auth: { enabled: true },
  keycloak: {
    enabled: false,
    browser_session_ready: false,
    login_url: null,
  },
  mcp_oauth: { enabled: false },
};

const stagedSsoConfig = {
  available: true,
  schema_version: 1 as const,
  auth_mode: "sso" as const,
  local_auth: { enabled: false },
  keycloak: {
    enabled: true,
    browser_session_ready: false,
    login_url: null,
  },
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
    expect(screen.queryByRole("button", { name: /Sign in with SSO/i })).toBeNull();
  });

  it("does not treat ?local=1 as an SSO-mode escape", async () => {
    window.history.replaceState({}, "", "/auth?local=1");
    vi.mocked(getAuthConfig).mockResolvedValue(stagedSsoConfig);

    renderAuth();

    expect(await screen.findByText(/SSO browser sign-in is not available yet/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/Username/i)).toBeNull();
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
        login_url: "/api/v1/auth/keycloak/login",
      },
    });

    renderAuth();

    expect(await screen.findByRole("button", { name: /Sign in with SSO/i })).toBeInTheDocument();
    expect(screen.queryByLabelText(/Username/i)).toBeNull();
    expect(navigate).not.toHaveBeenCalled();
  });
});
