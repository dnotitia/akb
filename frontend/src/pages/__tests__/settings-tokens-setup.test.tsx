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

describe("Tokens setup guide smart default", () => {
  it("opens by default when user has zero PATs", async () => {
    const { listPATs, getMe } = await import("@/lib/api");
    (getMe as any).mockResolvedValue({ user_id: "u1", username: "u", email: "u@x", is_admin: false });
    (listPATs as any).mockResolvedValue({ tokens: [] });
    render(wrap());
    expect(await screen.findByText("Step 01")).toBeVisible();
    expect(screen.getByText(/Mint a token/i)).toBeVisible();
  });

  it("closes by default when user has at least one PAT", async () => {
    const { listPATs, getMe } = await import("@/lib/api");
    (getMe as any).mockResolvedValue({ user_id: "u1", username: "u", email: "u@x", is_admin: false });
    (listPATs as any).mockResolvedValue({
      tokens: [{ token_id: "t1", name: "claude", prefix: "akb_xyz", created_at: "2026-05-19", last_used_at: null }],
    });
    render(wrap());
    expect(await screen.findByText(/claude/)).toBeVisible();
    expect(screen.queryByText(/Mint a token/i)).toBeNull();
  });

  it("respects localStorage override", async () => {
    localStorage.setItem("akb:tokens-setup-open", "true");
    const { listPATs, getMe } = await import("@/lib/api");
    (getMe as any).mockResolvedValue({ user_id: "u1", username: "u", email: "u@x", is_admin: false });
    (listPATs as any).mockResolvedValue({
      tokens: [{ token_id: "t1", name: "claude", prefix: "akb_xyz", created_at: "2026-05-19", last_used_at: null }],
    });
    render(wrap());
    expect(await screen.findByText(/Mint a token/i)).toBeVisible();
  });
});
