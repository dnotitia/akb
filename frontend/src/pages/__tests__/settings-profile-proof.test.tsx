import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { CurrentUserProvider } from "@/contexts/current-user-context";
import { ResourceNavigationProvider } from "@/contexts/resource-navigation-context";
import { configureAuthTransport, getMe, getToken, setToken, type CurrentUser } from "@/lib/api";
import SettingsDialog from "../settings/settings-dialog";

const USER: CurrentUser = { user_id: "a", username: "alice", email: "alice@example.test", display_name: "Alice", is_admin: false, auth_method: "local", key_class: null };
const config = { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const fetchMock = vi.fn();
let account = USER;
function CurrentLocation() { const location = useLocation(); return <output aria-label="Current route">{location.pathname}{location.search}</output>; }
function view(checking = false, revision = 0, user = USER) {
  return <MemoryRouter><CurrentUserProvider user={user} checking={checking} revision={revision}><ResourceNavigationProvider><CurrentLocation /><SettingsDialog onClose={vi.fn()} /></ResourceNavigationProvider></CurrentUserProvider></MemoryRouter>;
}
function fillPassword() {
  fireEvent.change(screen.getByLabelText("Current password"), { target: { value: "old-password" } });
  fireEvent.change(screen.getByLabelText("New password"), { target: { value: "new-password" } });
  fireEvent.change(screen.getByLabelText("Confirm new password"), { target: { value: "new-password" } });
  fireEvent.click(screen.getByRole("button", { name: "Change password" }));
}
beforeEach(() => {
  configureAuthTransport("local"); setToken("session-a"); account = USER;
  fetchMock.mockReset();
  fetchMock.mockImplementation((url: string) => Promise.resolve(json(url.endsWith("/auth/config") ? config : account)));
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => { cleanup(); setToken(null); configureAuthTransport(null); vi.unstubAllGlobals(); });

it("finishes a hidden password receipt even when foreground proof already sees revocation", async () => {
  const { rerender } = render(view());
  await screen.findByLabelText("Current password");
  let finish!: (response: Response) => void;
  fetchMock.mockImplementation((url: string) => url.endsWith("/change-password") ? new Promise<Response>(resolve => { finish = resolve; }) : Promise.resolve(json({}, 401)));
  fillPassword();
  rerender(view(true));
  expect(screen.getByLabelText("Current password")).not.toBeVisible();
  expect(screen.getByRole("button", { name: "Close settings" })).toBeDisabled();
  await expect(getMe({ redirectOnUnauthorized: false })).rejects.toMatchObject({ name: "DeferredSessionError" });
  expect(getToken()).toBe("session-a");
  await act(async () => finish(json({ ok: true })));
  await waitFor(() => expect(getToken()).toBeNull());
  expect(screen.getByLabelText("Current route")).toHaveTextContent("/auth?reason=password-changed");
  expect(screen.getByLabelText("Current password")).toHaveValue("");
});

it.each(["success", "failure"])("retains a hidden profile %s without abandoning its job", async result => {
  const { rerender } = render(view());
  fireEvent.change(await screen.findByLabelText("Display name"), { target: { value: "  " } });
  let finish!: (response: Response) => void;
  fetchMock.mockImplementation((url: string, init: RequestInit) => init?.method === "PATCH" ? new Promise<Response>(resolve => { finish = resolve; }) : Promise.resolve(json(url.endsWith("/auth/config") ? config : account)));
  fireEvent.click(screen.getByRole("button", { name: "Save profile" }));
  rerender(view(true));
  expect(screen.getByLabelText("Display name")).not.toBeVisible();
  expect(screen.getByRole("button", { name: "Close settings" })).toBeDisabled();
  if (result === "success") account = { ...USER, display_name: null };
  await act(async () => finish(result === "success" ? json({ updated: true, ...account }) : json({ detail: "Profile save unavailable" }, 503)));
  expect(screen.getByRole("button", { name: "Close settings" })).toBeEnabled();
  rerender(view(false, 1));
  if (result === "success") {
    await waitFor(() => expect(screen.getByLabelText("Display name")).toBeVisible());
    expect(screen.getByLabelText("Display name")).toHaveValue("");
    expect(screen.getByRole("button", { name: "Save profile" })).toBeDisabled();
    expect(screen.getByRole("region", { name: "Profile" })).toHaveTextContent("@alice");
  } else {
    await waitFor(() => expect(screen.getByText("Profile save unavailable")).toBeVisible());
    expect(screen.getByLabelText("Display name")).toHaveValue("  ");
    expect(screen.getByRole("button", { name: "Save profile" })).toBeEnabled();
  }
});

it("retains hidden wrong-password feedback and releases busy while keeping the draft", async () => {
  const { rerender } = render(view()); await screen.findByLabelText("Current password");
  let finish!: (response: Response) => void;
  fetchMock.mockImplementation((url: string) => url.endsWith("/change-password") ? new Promise<Response>(resolve => { finish = resolve; }) : Promise.resolve(json(url.endsWith("/auth/config") ? config : account)));
  fillPassword(); rerender(view(true));
  await act(async () => finish(json({ detail: { code: "current_password_incorrect", message: "Current password is incorrect" } }, 400)));
  expect(getToken()).toBe("session-a");
  expect(screen.getByRole("button", { name: "Close settings" })).toBeEnabled();
  rerender(view(false, 1));
  // React Activity restores the existing hidden DOM asynchronously. Finding
  // the retained error text alone does not prove that proof has revealed it.
  await waitFor(() => expect(screen.getByText("Current password is incorrect")).toBeVisible());
  await waitFor(() => expect(screen.getByLabelText("Current password")).toHaveFocus());
  expect(screen.getByLabelText("New password")).toHaveValue("new-password");
});

it("still expires a genuinely unauthorized password session after releasing the pending lifecycle", async () => {
  render(view()); await screen.findByLabelText("Current password");
  fetchMock.mockImplementation(() => Promise.resolve(json({}, 401)));
  fillPassword();
  await waitFor(() => expect(getToken()).toBeNull());
  expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/auth/me"))).toHaveLength(3);
});

it("really discards Profile when a confirmed section change leaves its owner", async () => {
  const user = userEvent.setup();
  render(view());
  fireEvent.change(await screen.findByLabelText("Display name"), { target: { value: "Discard this draft" } });
  await user.click(screen.getByRole("tab", { name: "Appearance" }));
  await user.click(await screen.findByRole("button", { name: "Discard changes" }));
  await user.click(screen.getByRole("tab", { name: "Profile" }));
  expect(await screen.findByLabelText("Display name")).toHaveValue("Alice");
});

it.each(["account replacement", "true unmount"])("ignores a late password receipt after %s", async exit => {
  const { rerender, unmount } = render(view()); await screen.findByLabelText("Current password");
  let finish!: (response: Response) => void;
  fetchMock.mockImplementation((url: string) => url.endsWith("/change-password") ? new Promise<Response>(resolve => { finish = resolve; }) : Promise.resolve(json(url.endsWith("/auth/config") ? config : account)));
  fillPassword();
  if (exit === "true unmount") unmount();
  else { setToken("session-b"); account = { ...USER, user_id: "b", username: "bob" }; rerender(view(true, 0, account)); }
  await act(async () => finish(json({ ok: true })));
  expect(getToken()).toBe(exit === "true unmount" ? "session-a" : "session-b");
  if (exit !== "true unmount") expect(screen.getByLabelText("Current route")).not.toHaveTextContent("/auth");
});
