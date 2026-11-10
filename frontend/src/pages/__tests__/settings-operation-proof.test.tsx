import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";
import { CurrentUserProvider } from "@/contexts/current-user-context";
import { ResourceNavigationProvider } from "@/contexts/resource-navigation-context";
import { configureAuthTransport, getToken, setToken } from "@/lib/api";
import SettingsDialog from "../settings/settings-dialog";

const uid = "00000000-0000-0000-0000-000000000001", oldId = "00000000-0000-0000-0000-000000000002", newId = "00000000-0000-0000-0000-000000000003";
const USER = { user_id: uid, username: "alice", email: "a@example.test", display_name: "Alice", is_admin: false, auth_method: "local", key_class: null };
const config = { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } };
const caps = { contract_version: 1, user_id: uid, name_max_length: 255, permission_presets: [["read"], ["read", "write"]], expiration_modes: ["none", "days", "absolute"], vault_scope_semantics: "write_restriction_sql_read_write" };
const original = { token_id: oldId, name: "original", prefix: "akb_old", key_class: "pat", scopes: ["read", "write"], vault_scope: null, expires_at: null };
const receipt = { ...original, contract_version: 1, user_id: uid, token_id: newId, token: "akb_hidden_receipt_secret", prefix: "akb_hidden", issued_at: "2030-01-01T00:00:00Z" };
const preview = { schema_version: 1, user_id: uid, username: "alice", revoke_sessions: { supported: true, scope: "local_sessions", includes_current: true, affects_pats: false, reason: null }, deletion: { supported: true, allowed: true, reason: null, confirmation: "username_and_current_password", blockers: [], effects: {} } };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const fetchMock = vi.fn();
function read(url: string) {
  if (url.endsWith("/auth/config")) return config;
  if (url.endsWith("/auth/tokens/capabilities")) return caps;
  if (url.endsWith("/auth/tokens")) return { tokens: [original] };
  if (url.endsWith("/my/account/lifecycle")) return preview;
  return USER;
}
function CurrentLocation() { const location = useLocation(); return <output aria-label="Current route">{location.pathname}{location.search}</output>; }
function mount(tab: string, initialUser = USER) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = (checking = false, revision = 0, user = initialUser) => <QueryClientProvider client={client}><MemoryRouter initialEntries={[tab === "tokens" ? "/settings?tab=tokens&connectionView=keys" : "/"]}><CurrentUserProvider user={user} checking={checking} revision={revision}><ResourceNavigationProvider><CurrentLocation /><SettingsDialog initialTab={tab} onClose={vi.fn()} /></ResourceNavigationProvider></CurrentUserProvider></MemoryRouter></QueryClientProvider>;
  return { ...render(view()), view };
}
beforeEach(() => {
  configureAuthTransport("local"); setToken("session-a"); fetchMock.mockReset();
  fetchMock.mockImplementation((url: string) => Promise.resolve(json(read(url)))); vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => { cleanup(); setToken(null); configureAuthTransport(null); vi.unstubAllGlobals(); });

it("delivers a replacement's one-time receipt completed while the real Settings Activity is hidden", async () => {
  const { rerender, view } = mount("tokens");
  fireEvent.click(await screen.findByRole("button", { name: "Replace key original" }));
  const dialog = screen.getByRole("dialog", { name: "Replace key original" });
  fireEvent.click(within(dialog).getByRole("button", { name: "Continue with Claude Code" }));
  await waitFor(() => expect(within(dialog).getByRole("button", { name: "Create replacement key" })).toBeEnabled());
  let finish!: (response: Response) => void;
  fetchMock.mockImplementation((url: string, init: RequestInit) => init?.method === "POST" ? new Promise<Response>(resolve => { finish = resolve; }) : Promise.resolve(json(read(url))));
  fireEvent.click(within(dialog).getByRole("button", { name: "Create replacement key" }));
  rerender(view(true));
  expect(dialog).not.toBeVisible();
  expect(screen.queryByRole("dialog", { name: "Replace key original" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Close settings" })).toBeDisabled();
  expect(document.body).toHaveAttribute("data-scroll-locked", "1");
  await act(async () => finish(json(receipt)));
  rerender(view(false, 1));
  await waitFor(() => expect(screen.getByText("Connection key created")).toBeVisible());
  expect(document.body.textContent).toContain(receipt.token);
  expect(screen.getByRole("button", { name: "Close replacement" })).toBeEnabled();
  expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "Close replacement" }));
  expect(await screen.findByRole("dialog", { name: "Have you saved your replacement?" })).toBeVisible();
});

it("never delivers a hidden previous-account token receipt to a replacement account", async () => {
  const { rerender, view } = mount("tokens");
  fireEvent.click(await screen.findByRole("button", { name: "Replace key original" }));
  const dialog = screen.getByRole("dialog", { name: "Replace key original" });
  fireEvent.click(within(dialog).getByRole("button", { name: "Continue with Claude Code" }));
  await waitFor(() => expect(within(dialog).getByRole("button", { name: "Create replacement key" })).toBeEnabled());
  let finish!: (response: Response) => void;
  fetchMock.mockImplementation((url: string, init: RequestInit) => init?.method === "POST" ? new Promise<Response>(resolve => { finish = resolve; }) : Promise.resolve(json(read(url))));
  fireEvent.click(within(dialog).getByRole("button", { name: "Create replacement key" }));
  setToken("session-b"); rerender(view(true, 0, { ...USER, user_id: "b", username: "bob" }));
  await act(async () => finish(json(receipt)));
  expect(document.body.textContent).not.toContain(receipt.token);
  expect(getToken()).toBe("session-b");
});

it.each(["pending", "failure"])("preserves replacement ownership across proof with %s issuance", async result => {
  const { rerender, view } = mount("tokens");
  fireEvent.click(await screen.findByRole("button", { name: "Replace key original" }));
  const dialog = screen.getByRole("dialog", { name: "Replace key original" });
  fireEvent.click(within(dialog).getByRole("button", { name: "Continue with Claude Code" }));
  await waitFor(() => expect(within(dialog).getByRole("button", { name: "Create replacement key" })).toBeEnabled());
  let finish!: (response: Response) => void;
  fetchMock.mockImplementation((url: string, init: RequestInit) => init?.method === "POST" ? new Promise<Response>(resolve => { finish = resolve; }) : Promise.resolve(json(read(url))));
  fireEvent.click(within(dialog).getByRole("button", { name: "Create replacement key" }));
  rerender(view(true));
  if (result === "failure") await act(async () => finish(json({ detail: "Token creation rejected" }, 403)));
  rerender(view(false, 1));
  await waitFor(() => expect(dialog).toBeVisible());
  if (result === "pending") {
    expect(within(dialog).getByRole("button", { name: "Close replacement" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Close settings", hidden: true })).toBeDisabled();
    await act(async () => finish(json(receipt)));
    expect(await within(dialog).findByText("Connection key created")).toBeVisible();
  } else {
    expect(await within(dialog).findByText("Token creation rejected")).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "Create replacement key" })).toBeEnabled();
  }
  expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});

it.each(["success", "failure"])("retains a hidden Security mutation's %s", async result => {
  const { rerender, view } = mount("security");
  await waitFor(() => expect(screen.getByRole("button", { name: "Sign out all sessions" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Sign out all sessions" }));
  let finish!: (response: Response) => void;
  fetchMock.mockImplementation((url: string, init: RequestInit) => init?.method === "POST" ? new Promise<Response>(resolve => { finish = resolve; }) : Promise.resolve(json(read(url))));
  fireEvent.click(within(screen.getByRole("dialog", { name: "Sign out all sessions?" })).getByRole("button", { name: "Sign out all sessions" }));
  rerender(view(true));
  expect(screen.getByRole("button", { name: "Close settings" })).toBeDisabled();
  await act(async () => finish(result === "success" ? json({ user_id: uid, revoked_before: "2026-10-14T00:00:00Z" }) : json({}, 503)));
  if (result === "success") {
    await waitFor(() => expect(getToken()).toBeNull());
    expect(screen.getByLabelText("Current route")).toHaveTextContent("/auth?reason=sessions-revoked");
  } else {
    rerender(view(false, 1));
    await waitFor(() => expect(screen.getByText(/We could not verify the result/)).toBeVisible());
    expect(getToken()).toBe("session-a");
    expect(screen.getByRole("button", { name: "Close settings" })).toBeEnabled();
  }
});

it.each(["success", "failure", "account replacement"])("keeps Admin reset ownership across proof: %s", async result => {
  const admin = { ...USER, is_admin: true };
  const reader = (url: string) => url.endsWith("/auth/me") ? admin : url.endsWith("/admin/users") ? { users: [{ id: "other", username: "bob", display_name: "Bob", email: "b@example.test", is_admin: false, created_at: "2026-10-01T00:00:00Z", owned_vaults: 0 }] } : read(url);
  fetchMock.mockImplementation((url: string) => Promise.resolve(json(reader(url))));
  const { rerender, view } = mount("admin", admin);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Actions for bob" }));
  await user.click(screen.getByRole("menuitem", { name: "Reset password for bob" }));
  const dialog = screen.getByRole("dialog", { name: "Reset password" });
  let finish!: (response: Response) => void;
  fetchMock.mockImplementation((url: string, init: RequestInit) => init?.method === "POST" ? new Promise<Response>(resolve => { finish = resolve; }) : Promise.resolve(json(reader(url))));
  await user.click(within(dialog).getByRole("button", { name: "Generate" }));
  rerender(view(true));
  await waitFor(() => expect(dialog).not.toBeVisible());
  if (result === "account replacement") { setToken("session-b"); rerender(view(true, 0, { ...admin, user_id: "b", username: "bob" })); }
  await act(async () => finish(result === "failure" ? json({ detail: "Reset was rejected" }, 403) : json({ temporary_password: "one-time-reset", username: "bob" })));
  if (result === "account replacement") {
    expect(document.body.textContent).not.toContain("one-time-reset");
    expect(getToken()).toBe("session-b");
  } else {
    rerender(view(false, 1));
    if (result === "success") expect(await screen.findByTestId("temp-password")).toHaveTextContent("one-time-reset");
    else expect(await screen.findByText("Reset was rejected")).toBeVisible();
    expect(within(screen.getByRole("dialog", { name: "Reset password" })).getByRole("button", { name: result === "success" ? "Done" : "Generate" })).toBeEnabled();
  }
});
