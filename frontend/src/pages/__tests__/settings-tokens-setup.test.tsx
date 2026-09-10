import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import SettingsPage from "../settings";

vi.mock("@/lib/api", () => ({
  getMe: vi.fn().mockResolvedValue({ user_id: "u1", username: "u", email: "u@x", is_admin: false }),
  listPATs: vi.fn(),
  adminListUsers: vi.fn(),
  // stubs for other api calls used by settings.tsx
  getToken: vi.fn(() => "fake-jwt"),
  setToken: vi.fn(),
  createPAT: vi.fn(),
  revokePAT: vi.fn(),
  adminDeleteUser: vi.fn(),
  changePassword: vi.fn(),
  updateProfile: vi.fn(),
  // tokens-section calls this on mount to decide whether to show the
  // OAuth toggle alongside the PAT snippet. Mock as "OAuth off" so
  // these tests stay focused on the PAT-mint UX they were written for.
  getAuthConfig: vi.fn().mockResolvedValue({
    available: true,
    schema_version: 2,
    auth_mode: "local",
    local_auth: { enabled: true },
    keycloak: {
      enabled: false,
      browser_session_ready: false,
    },
    providers: [],
    mcp_oauth: { enabled: false },
  }),
}));

vi.mock("@/hooks/use-theme", () => ({
  useTheme: () => ({ theme: "light", setTheme: vi.fn() }),
}));

function wrap() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/settings?tab=tokens"]}>
        <SettingsPage />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
});

describe("Shared connection setup", () => {
  it("offers setup without forcing credential creation when no PATs exist", async () => {
    const { listPATs, getMe } = await import("@/lib/api");
    (getMe as any).mockResolvedValue({ user_id: "u1", username: "u", email: "u@x", is_admin: false });
    (listPATs as any).mockResolvedValue({ tokens: [] });
    render(wrap());
    expect(await screen.findByText("No tokens yet")).toBeVisible();
    expect(document.getElementById("setup-guide-body")).not.toHaveAttribute("hidden");
    expect(screen.getByRole("heading", { name: "1. Prepare access" })).toBeVisible();
    expect(screen.getByRole("heading", { name: /2. Configure/ })).toBeVisible();
    expect(screen.getByRole("heading", { name: "3. Try it in your agent" })).toBeVisible();
  });

  it("keeps all setup phases visible even when the user has a PAT", async () => {
    const { listPATs, getMe } = await import("@/lib/api");
    (getMe as any).mockResolvedValue({ user_id: "u1", username: "u", email: "u@x", is_admin: false });
    (listPATs as any).mockResolvedValue({
      tokens: [{ token_id: "t1", name: "claude", prefix: "akb_xyz", created_at: "2026-05-19", last_used_at: null }],
    });
    render(wrap());
    expect(await screen.findByText(/claude/)).toBeVisible();
    expect(document.getElementById("setup-guide-body")).not.toHaveAttribute("hidden");
  });

  it("does not hide the setup entry behind an old browser preference", async () => {
    localStorage.setItem("akb:tokens-setup-open", "true");
    const { listPATs, getMe } = await import("@/lib/api");
    (getMe as any).mockResolvedValue({ user_id: "u1", username: "u", email: "u@x", is_admin: false });
    (listPATs as any).mockResolvedValue({
      tokens: [{ token_id: "t1", name: "claude", prefix: "akb_xyz", created_at: "2026-05-19", last_used_at: null }],
    });
    render(wrap());
    await screen.findByRole("heading", { name: "Connect an agent" });
    expect(document.getElementById("setup-guide-body")).not.toHaveAttribute("hidden");
    expect(screen.queryByText(/akb_xyz…/)).not.toBeInTheDocument();
  });

  it("keeps PAT management available in SSO mode", async () => {
    const { getAuthConfig, getMe, listPATs } = await import("@/lib/api");
    (getAuthConfig as any).mockResolvedValue({
      available: true,
      schema_version: 2,
      auth_mode: "sso",
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
      mcp_oauth: { enabled: true },
    });
    (getMe as any).mockResolvedValue({ user_id: "u1", username: "u", email: "u@x", is_admin: false });
    (listPATs as any).mockResolvedValue({
      tokens: [{ token_id: "t1", name: "automation", prefix: "akb_real", created_at: "2026-08-13", last_used_at: null }],
    });

    render(wrap());

    expect(await screen.findByText("automation")).toBeVisible();
    expect(screen.getByRole("button", { name: /revoke token automation/i })).toBeVisible();
  });
});
