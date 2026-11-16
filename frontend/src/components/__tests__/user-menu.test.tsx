import { beforeEach, describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Link, MemoryRouter, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { UserMenu } from "../user-menu";
import { getMe, logoutOrdinarySession, type CurrentUser } from "@/lib/api";
import { ResourceNavigationProvider, useResourceNavigationGuard } from "@/contexts/resource-navigation-context";
import { CurrentUserProvider } from "@/contexts/current-user-context";

vi.mock("@/lib/api", () => ({
  authSessionSnapshot: vi.fn(() => ({ generation: 1, token: "alice", mode: "local", csrfToken: null })),
  isCurrentAuthSession: vi.fn(() => true),
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
    vi.mocked(getMe).mockClear();
    vi.mocked(logoutOrdinarySession).mockResolvedValue({ mode: "local", logout_url: "/auth" });
  });

  it("consumes current shared metadata without another identity request", () => {
    const original: CurrentUser = { user_id: "alice", username: "alice", email: "alice@example.com", display_name: "Original name", is_admin: false, auth_method: "jwt", key_class: null };
    const view = render(<QueryClientProvider client={new QueryClient()}><MemoryRouter><CurrentUserProvider user={original}><UserMenu initialUser={original} /></CurrentUserProvider></MemoryRouter></QueryClientProvider>);
    expect(screen.getByRole("button", { name: /Account menu/ })).toHaveTextContent("Original name");
    view.rerender(<QueryClientProvider client={new QueryClient()}><MemoryRouter><CurrentUserProvider user={{ ...original, display_name: "Updated name" }} revision={1}><UserMenu initialUser={original} /></CurrentUserProvider></MemoryRouter></QueryClientProvider>);
    expect(screen.getByRole("button", { name: /Account menu/ })).toHaveTextContent("Updated name");
    expect(getMe).not.toHaveBeenCalled();
  });

  it("bypasses the dirty editor only after confirmed logout, not for an ordinary auth link", async () => {
    const guard = vi.fn(() => false);
    function Editor() { useResourceNavigationGuard(guard); return <output aria-label="Route">{useLocation().pathname}</output>; }
    const user = userEvent.setup();
    render(<QueryClientProvider client={new QueryClient()}><MemoryRouter initialEntries={["/doc"]}><ResourceNavigationProvider><Editor /><Link to="/auth">Ordinary sign-in link</Link><UserMenu /></ResourceNavigationProvider></MemoryRouter></QueryClientProvider>);
    await user.click(screen.getByRole("link", { name: "Ordinary sign-in link" }));
    expect(guard).toHaveBeenCalledOnce();
    expect(screen.getByLabelText("Route")).toHaveTextContent("/doc");
    await user.click(screen.getByLabelText(/Account menu/));
    await user.click(screen.getByText("Sign out"));
    await waitFor(() => expect(screen.getByLabelText("Route")).toHaveTextContent("/auth"));
    expect(guard).toHaveBeenCalledOnce();
  });

  it("does not render a Profile menu item — Settings is the sole entry", async () => {
    await open();
    await waitFor(() => expect(screen.queryByText("Settings")).toBeTruthy());
    expect(screen.queryByText("Profile")).toBeNull();
  });

  it("Settings item is present and links to /settings", async () => {
    await open();
    await waitFor(() => expect(screen.getByText("Settings")).toBeTruthy());
    expect(screen.getByText("@alice")).toBeInTheDocument();
  });

  it("opens without locking the document scrollbar", async () => {
    await open();

    expect(document.body).not.toHaveAttribute("data-scroll-locked");
    expect(document.body.style.pointerEvents).toBe("");
  });

  it("links to product administration only for a verified administrator", async () => {
    const admin: CurrentUser = { user_id: "admin", username: "admin", email: "", display_name: "Admin", is_admin: true, auth_method: "jwt", key_class: null };
    const client = new QueryClient();
    const menu = (user: CurrentUser, checking = false) =>
      <QueryClientProvider client={client}><MemoryRouter><CurrentUserProvider user={user} checking={checking}><UserMenu /></CurrentUserProvider></MemoryRouter></QueryClientProvider>;
    const view = render(menu(admin));
    await userEvent.click(screen.getByRole("button", { name: /Account menu/ }));
    const link = () => screen.queryByRole("menuitem", { name: "Product administration" });
    expect(link()).toHaveAttribute("href", "/admin");
    view.rerender(menu(admin, true));
    expect(link()).not.toBeInTheDocument();
    view.rerender(menu({ ...admin, is_admin: false }));
    expect(link()).not.toBeInTheDocument();
  });

  it("keeps a rejected sign-out visible inside the open menu", async () => {
    vi.mocked(logoutOrdinarySession).mockRejectedValueOnce(new Error("Sign-out unavailable"));
    const user = await open();

    await user.click(screen.getByText("Sign out"));

    expect(await screen.findByRole("alert")).toHaveTextContent("Sign-out unavailable");
    expect(screen.getByText("Sign out")).toBeTruthy();
  });
});
