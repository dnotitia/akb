import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { UserMenu } from "../user-menu";

vi.mock("@/lib/api", () => ({
  getMe: vi.fn().mockResolvedValue({
    username: "alice",
    email: "alice@example.com",
    display_name: "alice",
    is_admin: false,
  }),
  setToken: vi.fn(),
  // SSO-aware sign-out helpers (default: not an SSO session → local logout).
  isSsoSession: vi.fn().mockReturnValue(false),
  clearSsoSession: vi.fn(),
  keycloakLogoutUrl: vi.fn().mockReturnValue("/api/v1/auth/keycloak/logout"),
}));

vi.mock("@/hooks/use-theme", () => ({
  useTheme: () => ({ theme: "system", setTheme: vi.fn() }),
}));

async function open() {
  const user = userEvent.setup();
  render(<MemoryRouter><UserMenu /></MemoryRouter>);
  await user.click(screen.getByLabelText(/Account menu/));
  return user;
}

describe("UserMenu", () => {
  it("does not render a Profile menu item — Settings is the sole entry", async () => {
    await open();
    await waitFor(() => expect(screen.queryByText("Settings")).toBeTruthy());
    expect(screen.queryByText("Profile")).toBeNull();
  });

  it("Settings item is present and links to /settings", async () => {
    await open();
    await waitFor(() => expect(screen.getByText("Settings")).toBeTruthy());
  });
});
