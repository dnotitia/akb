// RTL coverage for the AuthPage `sso_only` mode: when the backend
// advertises `keycloak.sso_only = true`, the page must redirect to
// Keycloak immediately without rendering the local username/password
// form. `?local=1` is the escape hatch that keeps the form alive.
//
// Both checks pin down the documented contract — drift here would
// either trap admins (no escape) or strand SSO-only deployments with
// a confusing local form they were configured to hide.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import AuthPage from "../auth";
import { getAuthConfig } from "@/lib/api";

vi.mock("@/lib/api", () => ({
  authLogin: vi.fn(),
  authRegister: vi.fn(),
  setToken: vi.fn(),
  getToken: vi.fn(() => null),
  getAuthConfig: vi.fn(),
  clearSsoSession: vi.fn(),
}));

const navigate = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>(
    "react-router-dom",
  );
  return {
    ...actual,
    useNavigate: () => navigate,
  };
});

// jsdom's `window.location.href` setter triggers a real "navigation"
// that aborts the test. Stub it so we can ASSERT the destination
// without taking the navigation. The `search` getter reads a mutable
// outer variable, so a test can set the query string AFTER beforeEach
// (which is when `?local=1` cases naturally do so).
const originalLocation = window.location;
let hrefAssignments: string[] = [];
let stubbedSearch = "";

beforeEach(() => {
  hrefAssignments = [];
  stubbedSearch = "";
  Object.defineProperty(window, "location", {
    configurable: true,
    value: {
      ...originalLocation,
      pathname: "/auth",
      get search() {
        return stubbedSearch;
      },
      get href() {
        return originalLocation.href;
      },
      set href(v: string) {
        hrefAssignments.push(v);
      },
    },
  });
});

afterEach(() => {
  Object.defineProperty(window, "location", {
    configurable: true,
    value: originalLocation,
  });
  cleanup();
  navigate.mockReset();
});

function renderAuth() {
  return render(
    <MemoryRouter>
      <AuthPage />
    </MemoryRouter>,
  );
}

describe("AuthPage — SSO-only mode", () => {
  it("redirects to Keycloak immediately and never renders the form", async () => {
    vi.mocked(getAuthConfig).mockResolvedValue({
      keycloak: {
        enabled: true,
        login_url: "/api/v1/auth/keycloak/login",
        sso_only: true,
      },
    });

    renderAuth();

    await waitFor(() => expect(hrefAssignments.length).toBeGreaterThan(0));
    expect(hrefAssignments[0]).toMatch(
      /\/api\/v1\/auth\/keycloak\/login\?redirect=/,
    );
    // Form is never rendered — `Sign in` button (which the form owns)
    // must not appear, and the "Redirecting to SSO…" placeholder must.
    expect(screen.queryByRole("button", { name: /^Sign in$/ })).toBeNull();
    expect(screen.getByText(/Redirecting to SSO/i)).toBeInTheDocument();
  });

  it("`?local=1` keeps the local form visible even when sso_only is on", async () => {
    stubbedSearch = "?local=1";
    vi.mocked(getAuthConfig).mockResolvedValue({
      keycloak: {
        enabled: true,
        login_url: "/api/v1/auth/keycloak/login",
        sso_only: true,
      },
    });

    renderAuth();

    // Form renders with both Username + Sign-in-with-SSO option.
    await waitFor(() =>
      expect(screen.getByLabelText(/Username/i)).toBeInTheDocument(),
    );
    expect(
      screen.getByRole("button", { name: /Sign in with SSO/i }),
    ).toBeInTheDocument();
    // No redirect dispatched.
    expect(hrefAssignments).toEqual([]);
  });

  it("`?sso_error=…` suppresses the auto-redirect even in sso_only mode", async () => {
    // Without this guard an SSO-only deployment whose callback fails
    // (realm misconfig, IdP downtime, email_verified rejection) would
    // loop forever: /auth?sso_error → auto-redirect → IdP same fail →
    // /auth?sso_error → … The user can never see the error or escape.
    stubbedSearch = "?sso_error=invalid_state";
    vi.mocked(getAuthConfig).mockResolvedValue({
      keycloak: {
        enabled: true,
        login_url: "/api/v1/auth/keycloak/login",
        sso_only: true,
      },
    });

    renderAuth();

    // Form renders with the error; the SSO button stays available
    // for a manual retry.
    await waitFor(() =>
      expect(screen.getByLabelText(/Username/i)).toBeInTheDocument(),
    );
    expect(screen.getByText(/SSO login failed/i)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Sign in with SSO/i }),
    ).toBeInTheDocument();
    // Critical: no auto-redirect was dispatched.
    expect(hrefAssignments).toEqual([]);
  });

  it("does not redirect when sso_only is false (hybrid mode)", async () => {
    vi.mocked(getAuthConfig).mockResolvedValue({
      keycloak: {
        enabled: true,
        login_url: "/api/v1/auth/keycloak/login",
        sso_only: false,
      },
    });

    renderAuth();

    await waitFor(() =>
      expect(screen.getByLabelText(/Username/i)).toBeInTheDocument(),
    );
    expect(hrefAssignments).toEqual([]);
  });
});
