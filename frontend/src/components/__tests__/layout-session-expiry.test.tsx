import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "@/lib/api";
import { Layout } from "../layout";

// Keep getMe, authenticatedFetch and session generations real. Only public
// configuration and unrelated shell chrome are replaced by isolated fixtures.
vi.mock("@/lib/api", async importOriginal => ({
  ...await importOriginal<typeof import("@/lib/api")>(),
  getAuthConfig: vi.fn(),
}));
vi.mock("../user-menu", () => ({ UserMenu: () => null }));
vi.mock("../app-sidebar", () => ({ AppSidebar: () => null }));
vi.mock("../notification-bell", () => ({ NotificationBell: () => null }));
vi.mock("../header-indexing-status", () => ({ HeaderIndexingStatus: () => null }));
vi.mock("../global-search-dialog", () => ({ GlobalSearchDialog: () => null }));

const user: api.CurrentUser = {
  user_id: "expiry-fixture", username: "reader", email: "reader@example.invalid",
  display_name: "Reader", is_admin: false, auth_method: "browser_session", key_class: null,
};
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
let identityReads = 0;

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(["private-fixture"], "private data");
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={["/vault/team/settings"]}>
    <Routes><Route element={<Layout />}><Route path="/vault/:name/settings" element={<div>Protected workspace</div>} /></Route>
      <Route path="/auth" element={<div>Sign in fixture</div>} /></Routes>
  </MemoryRouter></QueryClientProvider>);
  return client;
}

beforeEach(() => {
  localStorage.clear();
  api.setToken(null);
  api.configureAuthTransport("sso");
  identityReads = 0;
  vi.mocked(api.getAuthConfig).mockResolvedValue({
    available: true, schema_version: 3, auth_mode: "sso", local_auth: { enabled: false },
    keycloak: { enabled: true, browser_session_ready: true }, providers: [], mcp_oauth: { enabled: false }, personal_access_tokens: { enabled: true }, mcp_endpoint_url: "https://akb.example.test/mcp/",
  });
});
afterEach(() => {
  cleanup();
  api.setToken(null);
  api.configureAuthTransport(null);
  vi.unstubAllGlobals();
});

function serveIdentity(read: () => Response | Promise<Response>) {
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/auth/me")) {
      identityReads += 1;
      // Bound the pre-fix infinite retry failure so it cannot starve the runner.
      if (identityReads > 3) return new Promise<Response>(() => {});
      return Promise.resolve(read());
    }
    return Promise.resolve(json({ vaults: [], items: [] }));
  }));
}

it.each(["sso", "local"] as const)("ends initial %s proof on a real current-session 401", async mode => {
  if (mode === "local") {
    api.configureAuthTransport("local");
    api.setToken("expired-test-session");
    vi.mocked(api.getAuthConfig).mockResolvedValue({
      available: true, schema_version: 3, auth_mode: "local", local_auth: { enabled: true },
      keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false }, personal_access_tokens: { enabled: true }, mcp_endpoint_url: "https://akb.example.test/mcp/",
    });
  }
  const before = api.authSessionSnapshot();
  serveIdentity(() => json({}, 401));
  mount();
  expect(await screen.findByText("Sign in fixture")).toBeVisible();
  expect(screen.queryByText("Protected workspace")).not.toBeInTheDocument();
  expect(api.isCurrentAuthSession(before)).toBe(false);
  expect(identityReads).toBe(1);
});

it("ends expired foreground SSO proof even when another focus request was queued", async () => {
  let finish!: (response: Response) => void;
  serveIdentity(() => identityReads === 1 ? json(user) : new Promise(resolve => { finish = resolve; }));
  const client = mount();
  await screen.findByText("Protected workspace");
  fireEvent(window, new Event("focus"));
  await waitFor(() => expect(identityReads).toBe(2));
  fireEvent(window, new Event("focus"));
  await act(async () => finish(json({}, 401)));
  expect(await screen.findByText("Sign in fixture")).toBeVisible();
  expect(client.getQueryData(["private-fixture"])).toBeUndefined();
  expect(identityReads).toBe(2);
});

it("retries a stale 401 for the new carrier instead of expiring the replacement account", async () => {
  let finish!: (response: Response) => void;
  serveIdentity(() => identityReads === 1 ? new Promise(resolve => { finish = resolve; }) : json(user));
  mount();
  await waitFor(() => expect(identityReads).toBe(1));
  api.configureAuthTransport("local");
  api.setToken("replacement-test-session");
  await act(async () => finish(json({}, 401)));
  expect(await screen.findByText("Protected workspace")).toBeVisible();
  expect(api.getToken()).toBe("replacement-test-session");
  expect(screen.queryByText("Sign in fixture")).not.toBeInTheDocument();
  expect(identityReads).toBe(2);
});

it("preserves a lifecycle-deferred foreground 401 until its mutation owns the receipt", async () => {
  serveIdentity(() => identityReads === 1 ? json(user) : json({}, 401));
  mount();
  await screen.findByText("Protected workspace");
  const before = api.authSessionSnapshot();
  const finishLifecycle = api.beginAccountLifecycle(before);
  try {
    await act(async () => fireEvent(window, new Event("focus")));
    expect(screen.getByText("Protected workspace")).toBeVisible();
    expect(screen.queryByText("Sign in fixture")).not.toBeInTheDocument();
    expect(api.isCurrentAuthSession(before)).toBe(true);
    expect(identityReads).toBe(2);
  } finally {
    finishLifecycle();
  }
});
