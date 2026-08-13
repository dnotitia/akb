import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import {
  adminLocalLogin,
  configureAdminSsoProvider,
  getAdminAuthConfig,
  getAdminSession,
  getAdminSsoCatalog,
  setAdminSsoProviderEnabled,
  setToken,
} from "@/lib/api";
import AdminPage from "../admin";


vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    getAdminAuthConfig: vi.fn(),
    getAdminSession: vi.fn(),
    adminLocalLogin: vi.fn(),
    adminLogout: vi.fn(),
    getAdminSsoCatalog: vi.fn(),
    configureAdminSsoProvider: vi.fn(),
    setAdminSsoProviderEnabled: vi.fn(),
    setToken: vi.fn(),
  };
});

vi.mock("@/hooks/use-theme", () => ({
  useTheme: () => ({ theme: "light", setTheme: vi.fn() }),
}));

const localConfig = {
  available: true,
  schema_version: 1 as const,
  auth_mode: "local" as const,
  local: {
    enabled: true,
    login_url: "/api/v1/admin/auth/local/login",
  },
  keycloak: { enabled: false, login_url: null },
};

const ssoConfig = {
  available: true,
  schema_version: 1 as const,
  auth_mode: "sso" as const,
  local: { enabled: false, login_url: null },
  keycloak: {
    enabled: true,
    login_url: "/api/v1/admin/auth/keycloak/login",
  },
};

const provider = {
  provider_type: "keycloak-oidc",
  alias: "workforce",
  display_name: "Company SSO",
  state: "enabled" as const,
  enabled: true,
  issuer: "https://accounts.example.com/realms/workforce",
  discovery_url: "https://accounts.example.com/realms/workforce/.well-known/openid-configuration",
  client_id: "akb-broker",
  client_secret_configured: true,
  redirect_uri: "https://auth.akb.example.com/realms/akb/broker/workforce/endpoint",
  capabilities: {
    supports_logout: true,
    supports_identity_migration: false,
  },
};

const directCatalog = {
  schema_version: 1 as const,
  auth_mode: "sso" as const,
  control_mode: "direct" as const,
  supported_provider_types: ["keycloak-oidc"],
  providers: [provider],
};

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/admin"]}>
        <AdminPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(getAdminSession).mockResolvedValue(null);
  vi.mocked(getAdminSsoCatalog).mockResolvedValue(directCatalog);
  vi.mocked(configureAdminSsoProvider).mockResolvedValue({
    ...provider,
    state: "configured_disabled",
    enabled: false,
  });
  vi.mocked(setAdminSsoProviderEnabled).mockResolvedValue(provider);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("AdminPage mode boundary", () => {
  it("shows only local admin credentials in local mode", async () => {
    vi.mocked(getAdminAuthConfig).mockResolvedValue(localConfig);
    renderPage();

    expect(await screen.findByLabelText("Username")).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /keycloak/i })).toBeNull();
  });

  it("shows only the dedicated Keycloak entry in SSO mode", async () => {
    vi.mocked(getAdminAuthConfig).mockResolvedValue(ssoConfig);
    renderPage();

    expect(await screen.findByRole("button", { name: /keycloak/i })).toBeInTheDocument();
    expect(screen.queryByLabelText("Username")).toBeNull();
    expect(screen.queryByRole("button", { name: /local/i })).toBeNull();
  });

  it("submits local credentials from the user action and stores the RS256 session", async () => {
    vi.mocked(getAdminAuthConfig).mockResolvedValue(localConfig);
    vi.mocked(adminLocalLogin).mockResolvedValue({ token: "local-admin-session" });
    const user = userEvent.setup();
    renderPage();

    await user.type(await screen.findByLabelText("Username"), "admin");
    await user.type(screen.getByLabelText("Password"), "password");
    await user.click(screen.getByRole("button", { name: /sign in/i }));

    await waitFor(() => {
      expect(adminLocalLogin).toHaveBeenCalledWith("admin", "password");
    });
    expect(setToken).toHaveBeenCalledWith("local-admin-session");
  });

  it("renders an authenticated AKB product admin without ordinary app layout", async () => {
    vi.mocked(getAdminAuthConfig).mockResolvedValue(ssoConfig);
    vi.mocked(getAdminSession).mockResolvedValue({
      schema_version: 1,
      auth_mode: "sso",
      user: {
        id: "11111111-1111-1111-1111-111111111111",
        username: "admin",
        email: "admin@example.com",
        display_name: "Admin",
        is_admin: true,
      },
    });
    renderPage();

    expect(await screen.findByText("Admin")).toBeInTheDocument();
    expect(screen.getByText("admin@example.com")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /sign out/i })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: /SSO providers/i })).toBeInTheDocument();
    expect(screen.getByText("Company SSO")).toBeInTheDocument();
  });

  it("toggles a configured provider without redeploying", async () => {
    vi.mocked(getAdminAuthConfig).mockResolvedValue(ssoConfig);
    vi.mocked(getAdminSession).mockResolvedValue({
      schema_version: 1,
      auth_mode: "sso",
      user: {
        id: "11111111-1111-1111-1111-111111111111",
        username: "admin",
        email: "admin@example.com",
        display_name: "Admin",
        is_admin: true,
      },
    });
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Disable" }));

    await waitFor(() => {
      expect(setAdminSsoProviderEnabled).toHaveBeenCalledWith("workforce", false);
    });
  });

  it("keeps emergency disable available for an enabled drifted provider", async () => {
    vi.mocked(getAdminAuthConfig).mockResolvedValue(ssoConfig);
    vi.mocked(getAdminSession).mockResolvedValue({
      schema_version: 1,
      auth_mode: "sso",
      user: {
        id: "11111111-1111-1111-1111-111111111111",
        username: "admin",
        email: "admin@example.com",
        display_name: "Admin",
        is_admin: true,
      },
    });
    vi.mocked(getAdminSsoCatalog).mockResolvedValue({
      ...directCatalog,
      providers: [{ ...provider, state: "configuration_error", enabled: true }],
    });
    const user = userEvent.setup();
    renderPage();

    const disable = await screen.findByRole("button", { name: "Disable" });
    expect(disable).toBeEnabled();
    await user.click(disable);

    await waitFor(() => {
      expect(setAdminSsoProviderEnabled).toHaveBeenCalledWith("workforce", false);
    });
  });

  it("derives discovery from issuer and sends the client secret only on save", async () => {
    vi.mocked(getAdminAuthConfig).mockResolvedValue(ssoConfig);
    vi.mocked(getAdminSession).mockResolvedValue({
      schema_version: 1,
      auth_mode: "sso",
      user: {
        id: "11111111-1111-1111-1111-111111111111",
        username: "admin",
        email: "admin@example.com",
        display_name: "Admin",
        is_admin: true,
      },
    });
    const user = userEvent.setup();
    renderPage();

    await user.clear(await screen.findByLabelText("Alias"));
    await user.type(screen.getByLabelText("Alias"), "partners");
    await user.type(screen.getByLabelText("Button label"), "Partner SSO");
    await user.type(screen.getByLabelText("Upstream issuer"), "https://id.example.com/realms/partners/");
    await user.type(screen.getByLabelText("Client ID"), "akb-partners");
    await user.type(screen.getByLabelText("Client secret"), "one-time-input");
    await user.click(screen.getByRole("button", { name: /Save disabled configuration/i }));

    await waitFor(() => {
      expect(configureAdminSsoProvider).toHaveBeenCalledWith("partners", {
        provider_type: "keycloak-oidc",
        display_name: "Partner SSO",
        issuer: "https://id.example.com/realms/partners",
        discovery_url: "https://id.example.com/realms/partners/.well-known/openid-configuration",
        client_id: "akb-partners",
        client_secret: "one-time-input", // pragma: allowlist secret
      });
    });
  });

  it("shows delegated ownership without rendering direct controls", async () => {
    vi.mocked(getAdminAuthConfig).mockResolvedValue(ssoConfig);
    vi.mocked(getAdminSession).mockResolvedValue({
      schema_version: 1,
      auth_mode: "sso",
      user: {
        id: "11111111-1111-1111-1111-111111111111",
        username: "admin",
        email: "admin@example.com",
        display_name: "Admin",
        is_admin: true,
      },
    });
    vi.mocked(getAdminSsoCatalog).mockResolvedValue({
      ...directCatalog,
      control_mode: "delegated",
      providers: [],
    });
    renderPage();

    expect(await screen.findByText(/managed by the deployment operator/i)).toBeInTheDocument();
    expect(screen.queryByLabelText("Client secret")).toBeNull();
  });
});
