import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { SecuritySection } from "../settings/security-section";
import { configureAuthTransport, getToken, setToken } from "@/lib/api";

const user = { user_id: "a", username: "alice", email: "a@x.test", display_name: "Alice", is_admin: false };
const preview = { schema_version: 1, user_id: "a", username: "alice", revoke_sessions: { supported: true, scope: "local_sessions", includes_current: true, affects_pats: false, reason: null }, deletion: { supported: true, allowed: true, reason: null, confirmation: "username_and_current_password", blockers: [], effects: { active_pats_revoked: 2, shared_publications_preserved: 1 } } };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const fetchMock = vi.fn();
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter><SecuritySection user={user} onBusyChange={vi.fn()} /></MemoryRouter></QueryClientProvider>);
  return client;
}
beforeEach(() => { configureAuthTransport("local"); setToken("token-a"); fetchMock.mockReset(); fetchMock.mockImplementation(() => Promise.resolve(json(preview))); vi.stubGlobal("fetch", fetchMock); });
afterEach(() => { cleanup(); setToken(null); configureAuthTransport(null); vi.unstubAllGlobals(); });
const posts = () => fetchMock.mock.calls.filter(c => c[1]?.method === "POST");

it("requires a current password and exact username before deleting", async () => {
  mount();
  await waitFor(() => expect(screen.getByRole("button", { name: "Review account deletion" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Review account deletion" }));
  await screen.findByRole("dialog");
  const next = screen.getByRole("button", { name: "Continue to confirmation" });
  expect(next).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Current password"), { target: { value: "secret" } });
  fireEvent.click(next);
  const confirm = screen.getByRole("button", { name: "Permanently delete account" });
  expect(confirm).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Type your username to confirm"), { target: { value: "Alice" } });
  expect(confirm).toBeDisabled(); expect(posts()).toHaveLength(0);
  fireEvent.change(screen.getByLabelText("Type your username to confirm"), { target: { value: "alice" } });
  fetchMock.mockImplementation(() => Promise.resolve(json({ detail: { code: "reauthentication_failed", message: "Current password is incorrect." } }, 403)));
  fireEvent.click(confirm);
  await screen.findByText("Current password is incorrect.");
  expect(screen.getByLabelText("Current password")).toHaveValue("");
  expect(getToken()).toBe("token-a"); expect(posts()).toHaveLength(1);
});

it("never exposes destructive execution on old servers", async () => {
  fetchMock.mockImplementation(() => Promise.resolve(new Response("Not found", { status: 404 })));
  mount();
  await screen.findByText(/does not support this action/);
  expect(screen.getByRole("button", { name: "Review account deletion" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Sign out all sessions" })).toBeDisabled();
  expect(posts()).toHaveLength(0); expect(getToken()).toBe("token-a");
});

it("blocks deletion when an owned vault was added before review", async () => {
  mount(); await screen.findByRole("button", { name: "Review account deletion" });
  await waitFor(() => expect(screen.getByRole("button", { name: "Review account deletion" })).toBeEnabled());
  fetchMock.mockImplementation((url: string) => Promise.resolve(json(url.includes("deletion-blockers") ? { user_id: "a", owned_vaults: [{ id: "v", name: "Team vault" }], total: 1, next_cursor: null } : { ...preview, deletion: { ...preview.deletion, allowed: false, blockers: [{ code: "owned_vaults", count: 1 }] } })));
  fireEvent.click(screen.getByRole("button", { name: "Review account deletion" }));
  await screen.findByRole("dialog");
  expect(screen.getByRole("button", { name: "Continue to confirmation" })).toBeDisabled();
  expect(posts()).toHaveLength(0);
});

it("keeps authentication and cached data until a verified response; suppresses duplicate clicks", async () => {
  const client = mount(); client.setQueryData(["private"], "secret-data");
  await waitFor(() => expect(screen.getByRole("button", { name: "Sign out all sessions" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Sign out all sessions" }));
  let resolve!: (response: Response) => void;
  fetchMock.mockImplementation(() => new Promise<Response>(r => { resolve = r; }));
  const buttons = screen.getAllByRole("button", { name: "Sign out all sessions" });
  fireEvent.click(buttons[buttons.length - 1]); fireEvent.click(buttons[buttons.length - 1]);
  expect(posts()).toHaveLength(1); expect(getToken()).toBe("token-a"); expect(client.getQueryData(["private"])).toBe("secret-data");
  resolve(json({ user_id: "a", revoked_before: "2026-09-15T00:00:00Z" }));
  await waitFor(() => expect(getToken()).toBeNull());
  expect(client.getQueryData(["private"])).toBeUndefined();
});

it("invalidates an open confirmation after another tab signs in", async () => {
  mount(); await waitFor(() => expect(screen.getByRole("button", { name: "Sign out all sessions" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Sign out all sessions" }));
  localStorage.setItem("akb_token", "token-b");
  fireEvent(window, new Event("storage"));
  await screen.findByText(/Your sign-in session changed/);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument(); expect(posts()).toHaveLength(0);
  expect(getToken()).toBe("token-b");
});

it.each(["network", "malformed", "unauthorized"])("keeps an honest unconfirmed result for %s without clearing private data", async failure => {
  const client = mount(); client.setQueryData(["private"], "data");
  await waitFor(() => expect(screen.getByRole("button", { name: "Sign out all sessions" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Sign out all sessions" }));
  fetchMock.mockImplementation(() => failure === "network" ? Promise.reject(new TypeError("Network error")) : Promise.resolve(json(failure === "malformed" ? {} : { detail: "Unauthorized" }, failure === "unauthorized" ? 401 : 200)));
  const buttons = screen.getAllByRole("button", { name: "Sign out all sessions" });
  fireEvent.click(buttons[buttons.length - 1]);
  await screen.findByText(failure === "unauthorized" ? /Your session could not be verified/ : /We could not verify the result/);
  expect(getToken()).toBe("token-a"); expect(client.getQueryData(["private"])).toBe("data"); expect(posts()).toHaveLength(1);
});

it("keeps organization-managed lifecycle actions unavailable", async () => {
  fetchMock.mockImplementation(() => Promise.resolve(json({ ...preview, revoke_sessions: { ...preview.revoke_sessions, supported: false, reason: "managed_account" }, deletion: { ...preview.deletion, supported: false, allowed: false, reason: "managed_account" } })));
  mount(); await screen.findAllByText(/Your organization manages this account/);
  expect(screen.getByRole("button", { name: "Sign out all sessions" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Review account deletion" })).toBeDisabled();
  expect(posts()).toHaveLength(0);
});

it("does not let a stale sign-in-again prompt clear a replacement session", async () => {
  fetchMock.mockImplementation(() => Promise.resolve(json({}, 401)));
  mount();
  const signIn = await screen.findByRole("button", { name: "Sign in again" });
  setToken("token-b");
  fireEvent.click(signIn);
  expect(getToken()).toBe("token-b");
  await screen.findByText(/Your sign-in session changed/);
});

it("shows the server's latest ownership blocker if deletion loses a race", async () => {
  mount(); await waitFor(() => expect(screen.getByRole("button", { name: "Review account deletion" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Review account deletion" }));
  await screen.findByRole("dialog");
  fireEvent.change(screen.getByLabelText("Current password"), { target: { value: "secret" } });
  fireEvent.click(screen.getByRole("button", { name: "Continue to confirmation" }));
  fireEvent.change(screen.getByLabelText("Type your username to confirm"), { target: { value: "alice" } });
  fetchMock.mockImplementation(() => Promise.resolve(json({ detail: { code: "account_deletion_blocked", message: "Deletion is blocked.", details: { blockers: [{ code: "owned_vaults", count: 1 }] } } }, 409)));
  fireEvent.click(screen.getByRole("button", { name: "Permanently delete account" }));
  await screen.findByText(/Deletion is blocked. You own 1 vault/);
  expect(screen.getByLabelText("Current password")).toHaveValue("");
  expect(getToken()).toBe("token-a");
});

it("offers reauthentication inside the deletion dialog after a 401", async () => {
  mount(); await waitFor(() => expect(screen.getByRole("button", { name: "Review account deletion" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Review account deletion" }));
  await screen.findByRole("dialog");
  fireEvent.change(screen.getByLabelText("Current password"), { target: { value: "secret" } });
  fireEvent.click(screen.getByRole("button", { name: "Continue to confirmation" }));
  fireEvent.change(screen.getByLabelText("Type your username to confirm"), { target: { value: "alice" } });
  fetchMock.mockImplementation(() => Promise.resolve(json({}, 401)));
  fireEvent.click(screen.getByRole("button", { name: "Permanently delete account" }));
  await screen.findByText(/Your session could not be verified/);
  expect(screen.getByRole("dialog").querySelector("button")?.textContent).toBe("Sign in again");
  expect(getToken()).toBe("token-a");
});
