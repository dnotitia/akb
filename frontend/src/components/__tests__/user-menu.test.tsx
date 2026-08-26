import { beforeEach, describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { UserMenu } from "../user-menu";
import { logoutOrdinarySession } from "@/lib/api";

vi.mock("@/lib/api", () => ({
  getMe: vi.fn().mockResolvedValue({
    username: "alice",
    email: "alice@example.com",
    display_name: "alice",
    is_admin: false,
  }),
  logoutOrdinarySession: vi.fn().mockResolvedValue({ mode: "local", logout_url: "/auth" }),
}));

vi.mock("@/hooks/use-theme", () => ({
  useTheme: () => ({ theme: "system", setTheme: vi.fn() }),
}));

async function open() {
  const user = userEvent.setup();
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter><UserMenu /></MemoryRouter>
    </QueryClientProvider>,
  );
  await user.click(screen.getByLabelText(/Account menu/));
  return user;
}

describe("UserMenu", () => {
  beforeEach(() => {
    vi.mocked(logoutOrdinarySession).mockResolvedValue({ mode: "local", logout_url: "/auth" });
  });

  it("does not render a Profile menu item — Settings is the sole entry", async () => {
    await open();
    await waitFor(() => expect(screen.queryByText("Settings")).toBeTruthy());
    expect(screen.queryByText("Profile")).toBeNull();
  });

  it("Settings item is present and links to /settings", async () => {
    await open();
    await waitFor(() => expect(screen.getByText("Settings")).toBeTruthy());
  });

  it("opens without locking the document scrollbar", async () => {
    await open();

    expect(document.body).not.toHaveAttribute("data-scroll-locked");
    expect(document.body.style.pointerEvents).toBe("");
  });

  it("keeps a rejected sign-out visible inside the open menu", async () => {
    vi.mocked(logoutOrdinarySession).mockRejectedValueOnce(new Error("Sign-out unavailable"));
    const user = await open();

    await user.click(screen.getByText("Sign out"));

    expect(await screen.findByRole("alert")).toHaveTextContent("Sign-out unavailable");
    expect(screen.getByText("Sign out")).toBeTruthy();
  });
});
