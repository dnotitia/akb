import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import SettingsDialog from "../settings/settings-dialog";

vi.mock("@/lib/api", async () => ({
  ...await vi.importActual<typeof import("@/lib/api")>("@/lib/api"),
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
        <SettingsDialog initialTab="tokens" onClose={vi.fn()} />
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
    await screen.findByRole("heading", { name: "AI connections" });
    expect(screen.getAllByRole("heading", { level: 3 })).toHaveLength(1);
    await userEvent.setup().click(screen.getByRole("tab", { name: "Connection keys" }));
    expect(await screen.findByText("No connection keys yet")).toBeVisible();
    await userEvent.setup().click(screen.getByRole("tab", { name: "Connection setup" }));
    expect(document.getElementById("setup-guide-body")).not.toHaveAttribute("hidden");
    expect(screen.getByRole("heading", { name: "1. Choose your AI tool" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "3. Check the connection" })).toBeNull();
  });

  it("keeps setup and key management separately available with an existing PAT", async () => {
    const { listPATs, getMe } = await import("@/lib/api");
    (getMe as any).mockResolvedValue({ user_id: "u1", username: "u", email: "u@x", is_admin: false });
    (listPATs as any).mockResolvedValue({
      tokens: [{ token_id: "t1", name: "claude", prefix: "akb_xyz", created_at: "2026-05-19", last_used_at: null }],
    });
    render(wrap());
    await userEvent.setup().click(await screen.findByRole("tab", { name: "Connection keys" }));
    expect(await screen.findByText(/claude/)).toBeVisible();
    await userEvent.setup().click(screen.getByRole("tab", { name: "Connection setup" }));
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
    await screen.findByRole("heading", { name: "AI connections" });
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

    await userEvent.setup().click(await screen.findByRole("tab", { name: "Connection keys" }));
    expect(await screen.findByText("automation")).toBeVisible();
    expect(screen.getByRole("button", { name: /revoke key automation/i })).toBeVisible();
  });
});
