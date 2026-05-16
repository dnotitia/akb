import { cleanup, render, screen, fireEvent, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import SettingsPage from "../settings";
import * as api from "@/lib/api";

// Mock the entire api module so the test doesn't touch network. Only
// the call surface used by the Profile tab matters here; other tabs
// (PATs, admin, memory) get harmless stubs.
vi.mock("@/lib/api", () => ({
  getMe: vi.fn(),
  getToken: vi.fn(() => "fake-jwt"),
  setToken: vi.fn(),
  listPATs: vi.fn().mockResolvedValue({ tokens: [] }),
  createPAT: vi.fn(),
  revokePAT: vi.fn(),
  adminListUsers: vi.fn().mockResolvedValue({ users: [] }),
  adminDeleteUser: vi.fn(),
  changePassword: vi.fn(),
  updateProfile: vi.fn(),
}));

// Bypass the theme hook — it pokes localStorage / matchMedia at mount.
vi.mock("@/hooks/use-theme", () => ({
  useTheme: () => ({ theme: "light", setTheme: vi.fn() }),
}));

function renderSettings(initialPath = "/settings?tab=profile") {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <SettingsPage />
    </MemoryRouter>,
  );
}

const USER = {
  user_id: "u1",
  username: "alice",
  email: "alice@example.com",
  display_name: "Alice Original",
  is_admin: false,
};

describe("settings — profile edit", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.getMe as any).mockResolvedValue(USER);
    (api.updateProfile as any).mockResolvedValue({
      updated: true,
      username: USER.username,
      display_name: "Alice Renamed",
      email: USER.email,
    });
  });

  afterEach(() => cleanup());

  it("hydrates the form with the current user payload", async () => {
    renderSettings();
    expect(await screen.findByDisplayValue("Alice Original")).toBeTruthy();
    expect(screen.getByDisplayValue("alice@example.com")).toBeTruthy();
  });

  it("'Save profile' is blocked when nothing changed", async () => {
    renderSettings();
    await screen.findByDisplayValue("Alice Original");
    fireEvent.click(screen.getByRole("button", { name: /save profile/i }));
    await screen.findByText(/no changes to save/i);
    expect(api.updateProfile as any).not.toHaveBeenCalled();
  });

  it("sends only the changed fields and refreshes local state on success", async () => {
    renderSettings();
    const name = (await screen.findByDisplayValue("Alice Original")) as HTMLInputElement;
    fireEvent.change(name, { target: { value: "Alice Renamed" } });
    fireEvent.click(screen.getByRole("button", { name: /save profile/i }));

    await waitFor(() =>
      expect(api.updateProfile as any).toHaveBeenCalledWith({
        display_name: "Alice Renamed",
      }),
    );
    // Success indicator + the new value is now the rendered "current" value
    await screen.findByText(/saved/i);
    expect(screen.getByDisplayValue("Alice Renamed")).toBeTruthy();
  });

  it("surfaces backend errors instead of swallowing them", async () => {
    (api.updateProfile as any).mockRejectedValue(new Error("Email already in use"));
    renderSettings();
    const email = (await screen.findByDisplayValue("alice@example.com")) as HTMLInputElement;
    fireEvent.change(email, { target: { value: "taken@example.com" } });
    fireEvent.click(screen.getByRole("button", { name: /save profile/i }));
    await screen.findByText(/email already in use/i);
  });
});
