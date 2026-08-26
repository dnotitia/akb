import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Layout } from "../layout";
import * as api from "@/lib/api";

// Mock api so getToken() can be flipped between tests, and the health
// hook's network call never fires. UserMenu (rendered by Layout) calls
// getMe() in an effect; stub it to resolve null so the component doesn't
// throw inside React's commit phase.
vi.mock("@/lib/api", () => ({
  getToken: vi.fn(),
  getAuthConfig: vi.fn(),
  getMe: vi.fn(),
  searchDocs: vi.fn(),
  logoutOrdinarySession: vi.fn(),
  clearPrivateAssetCache: vi.fn(),
}));

vi.mock("@/hooks/use-health", () => ({
  useHealth: () => ({ data: undefined, isLoading: false, error: null }),
}));

vi.mock("@/hooks/use-measured-height", () => ({
  // Return the same shape `[ref, number]` the real hook gives.
  useMeasuredHeight: () => [vi.fn(), 0],
}));

function renderAt(path: string, queryClient = new QueryClient()) {
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/" element={<div data-testid="home" />} />
            <Route path="/search" element={<div data-testid="search-page" />} />
            <Route
              path="/vault/:name/settings"
              element={<div data-testid="vault-settings" />}
            />
          </Route>
          <Route path="/auth" element={<div data-testid="auth-page" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Layout — auth gate", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    vi.mocked(api.getAuthConfig).mockResolvedValue({
      available: true,
      schema_version: 2,
      auth_mode: "local",
      local_auth: { enabled: true },
      keycloak: { enabled: false, browser_session_ready: false },
      providers: [],
      mcp_oauth: { enabled: false },
    });
    vi.mocked(api.getMe).mockResolvedValue({
      user_id: "user-1",
      username: "alice",
      email: "alice@example.com",
      display_name: "Alice",
      is_admin: false,
      auth_method: "jwt",
      key_class: null,
    });
  });
  afterEach(() => cleanup());

  it("redirects to /auth when local mode has no token", async () => {
    vi.mocked(api.getToken).mockReturnValue(null);
    renderAt("/");
    expect(await screen.findByTestId("auth-page")).toBeTruthy();
    expect(screen.queryByTestId("home")).toBeNull();
  });

  it("renders the outlet only after the local token is verified", async () => {
    vi.mocked(api.getToken).mockReturnValue("fake-jwt");
    renderAt("/");
    expect(await screen.findByTestId("home")).toBeTruthy();
    expect(api.getMe).toHaveBeenCalledWith({ redirectOnUnauthorized: false });
    expect(screen.queryByTestId("auth-page")).toBeNull();
  });

  it("keeps the global header full-width without page-level responsive gutters", async () => {
    vi.mocked(api.getToken).mockReturnValue("fake-jwt");
    renderAt("/");

    expect(await screen.findByTestId("home")).toBeTruthy();
    const headerRow = screen.getByRole("banner").firstElementChild;
    expect(headerRow).toHaveClass("w-full");
    expect(headerRow).not.toHaveClass("px-3");
    expect(headerRow).not.toHaveClass("xl:px-12", "2xl:px-16");
  });

  it("moves desktop entry points into an expanded workspace sidebar", async () => {
    vi.mocked(api.getToken).mockReturnValue("fake-jwt");
    renderAt("/");

    expect(await screen.findByTestId("home")).toBeTruthy();
    const sidebar = screen.getByTestId("app-sidebar");
    const navigation = within(sidebar).getByRole("navigation", {
      name: "Workspace navigation",
    });

    expect(sidebar).toHaveAttribute("data-compact", "false");
    expect(sidebar).toHaveClass("lg:w-52");
    expect(
      within(navigation).getByRole("link", { name: "Home" }),
    ).toHaveAttribute("aria-current", "page");
    expect(
      within(navigation).getByRole("link", { name: "Vaults" }),
    ).toHaveAttribute("href", "/vault");
    expect(
      within(navigation).getByRole("link", { name: "Search" }),
    ).toHaveAttribute("href", "/search");
    expect(
      screen.getByRole("button", { name: "Collapse sidebar" }),
    ).toHaveAttribute("aria-expanded", "true");
  });

  it("collapses the workspace sidebar and remembers the preference", async () => {
    vi.mocked(api.getToken).mockReturnValue("fake-jwt");
    renderAt("/");

    expect(await screen.findByTestId("home")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Collapse sidebar" }));

    const sidebar = screen.getByTestId("app-sidebar");
    expect(sidebar).toHaveAttribute("data-compact", "true");
    expect(sidebar).toHaveClass("lg:w-14");
    expect(
      screen.getByRole("button", { name: "Expand sidebar" }),
    ).toHaveAttribute("aria-expanded", "false");
    expect(localStorage.getItem("akb_app_sidebar_compact")).toBe("true");
  });

  it("uses equal desktop content gutters outside the sidebar", async () => {
    vi.mocked(api.getToken).mockReturnValue("fake-jwt");
    renderAt("/");

    expect(await screen.findByTestId("home")).toBeTruthy();
    expect(screen.getByRole("main").firstElementChild).toHaveClass(
      "lg:px-8",
      "xl:px-12",
      "2xl:px-36",
    );
  });

  it("does not reserve a second root scrollbar gutter for vault workspaces", async () => {
    vi.mocked(api.getToken).mockReturnValue("fake-jwt");
    renderAt("/vault/demo/settings");

    expect(await screen.findByTestId("vault-settings")).toBeTruthy();
    expect(document.documentElement).toHaveClass("vault-workspace-scroll-lock");
    expect(screen.getByTestId("app-sidebar")).toHaveAttribute(
      "data-compact",
      "true",
    );
    expect(screen.getByTestId("app-sidebar")).toHaveClass("lg:w-14");
  });

  it("accepts a verified SSO cookie session without any local token", async () => {
    vi.mocked(api.getAuthConfig).mockResolvedValue({
      available: true,
      schema_version: 2,
      auth_mode: "sso",
      local_auth: { enabled: false },
      keycloak: { enabled: true, browser_session_ready: true },
      providers: [],
      mcp_oauth: { enabled: true },
    });
    vi.mocked(api.getToken).mockReturnValue(null);

    renderAt("/");

    expect(await screen.findByTestId("home")).toBeTruthy();
    expect(api.getMe).toHaveBeenCalledWith({ redirectOnUnauthorized: false });
  });

  it("rechecks a foreground SSO identity before clearing prior private queries", async () => {
    vi.mocked(api.getAuthConfig).mockResolvedValue({
      available: true,
      schema_version: 2,
      auth_mode: "sso",
      local_auth: { enabled: false },
      keycloak: { enabled: true, browser_session_ready: true },
      providers: [],
      mcp_oauth: { enabled: true },
    });
    vi.mocked(api.getToken).mockReturnValue(null);
    vi.mocked(api.getMe)
      .mockResolvedValueOnce({
        user_id: "user-1",
        username: "alice",
        email: "alice@example.com",
        display_name: "Alice",
        is_admin: false,
        auth_method: "browser_session",
        key_class: null,
      })
      .mockResolvedValueOnce({
        user_id: "user-2",
        username: "bob",
        email: "bob@example.com",
        display_name: "Bob",
        is_admin: false,
        auth_method: "browser_session",
        key_class: null,
      });
    const queryClient = new QueryClient();
    queryClient.setQueryData(["private", "alice"], { secret: true });

    renderAt("/", queryClient);
    expect(await screen.findByTestId("home")).toBeTruthy();

    window.dispatchEvent(new Event("focus"));

    expect(await screen.findByText("Verifying session…")).toBeTruthy();
    await waitFor(() => expect(api.getMe).toHaveBeenCalledTimes(2));
    expect(await screen.findByTestId("home")).toBeTruthy();
    expect(queryClient.getQueryData(["private", "alice"])).toBeUndefined();
    expect(api.clearPrivateAssetCache).toHaveBeenCalled();
  });
});
