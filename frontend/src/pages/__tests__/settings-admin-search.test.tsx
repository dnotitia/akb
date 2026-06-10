import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import SettingsPage from "../settings";

vi.mock("@/lib/api", () => ({
  getMe: vi.fn().mockResolvedValue({ user_id: "u1", username: "admin", email: "a@x", is_admin: true }),
  getToken: vi.fn(() => "fake-jwt"),
  setToken: vi.fn(),
  listPATs: vi.fn().mockResolvedValue({ tokens: [] }),
  createPAT: vi.fn(),
  revokePAT: vi.fn(),
  adminListUsers: vi.fn().mockResolvedValue({
    users: [
      { id: "u1", username: "alice", email: "alice@x", display_name: "Alice", is_admin: false, owned_vaults: 3, created_at: "2026-05-01T00:00:00Z" },
      { id: "u2", username: "bob", email: "bob@x", display_name: "Bob", is_admin: false, owned_vaults: 0, created_at: "2026-05-15T00:00:00Z" },
      { id: "u3", username: "carol", email: "carol@x", display_name: "Carol", is_admin: true, owned_vaults: 1, created_at: "2026-04-01T00:00:00Z" },
    ],
  }),
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
      <MemoryRouter initialEntries={["/settings?tab=admin"]}>
        <SettingsPage />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => vi.clearAllMocks());

describe("Admin tab search + sort", () => {
  it("filters users by name", async () => {
    const u = userEvent.setup();
    render(wrap());
    await screen.findByText("alice");
    await u.type(screen.getByPlaceholderText(/Search users/i), "bo");
    // Wait for debounce (200ms) to fire and re-render.
    await waitFor(() => expect(screen.queryByText("alice")).toBeNull(), { timeout: 1000 });
    expect(screen.getByText("bob")).toBeTruthy();
  });

  it("sorts by Most vaults", async () => {
    const u = userEvent.setup();
    render(wrap());
    await screen.findByText("alice");
    // Sort is now a themed dropdown (Radix), not a native <select>: open it and
    // pick the option instead of selectOptions.
    await u.click(screen.getByLabelText(/Sort/i));
    await u.click(await screen.findByRole("menuitemradio", { name: /Most vaults/i }));
    const usernames = screen.getAllByTestId("admin-user-row").map((el) =>
      el.querySelector("[data-testid='admin-user-name']")?.textContent
    );
    expect(usernames).toEqual(["alice", "carol", "bob"]); // 3, 1, 0
  });
});
