import { act, cleanup, render, screen, fireEvent, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import SettingsDialog from "../settings/settings-dialog";
import * as api from "@/lib/api";
import { ProfileSection } from "../settings/profile-section";
import { CurrentUserProvider } from "@/contexts/current-user-context";
import { ResourceNavigationProvider } from "@/contexts/resource-navigation-context";
import { Profiler } from "react";

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
  authSessionSnapshot: vi.fn(() => ({ generation: 0, token: "fake-jwt" })),
  isCurrentAuthSession: vi.fn(() => true),
  clearCompletedAccountSession: vi.fn(() => false),
  beginAccountLifecycle: vi.fn(() => vi.fn()),
  updateProfile: vi.fn(),
  getAuthConfig: vi.fn(),
}));

// Bypass the theme hook — it pokes localStorage / matchMedia at mount.
vi.mock("@/hooks/use-theme", () => ({
  useTheme: () => ({ theme: "light", setTheme: vi.fn() }),
}));

function renderSettings(initialPath = "/settings?tab=profile") {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <SettingsDialog initialTab={new URLSearchParams(initialPath.split("?")[1]).get("tab") ?? "profile"} onClose={vi.fn()} />
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

const LOCAL_AUTH_CONFIG = {
  available: true,
  schema_version: 2,
  auth_mode: "local",
  local_auth: { enabled: true },
  keycloak: {
    enabled: false,
    browser_session_ready: false,
  },
  providers: [],
  mcp_oauth: { enabled: false },
};

const SSO_AUTH_CONFIG = {
  available: true,
  schema_version: 2,
  auth_mode: "sso",
  local_auth: { enabled: false },
  keycloak: {
    enabled: true,
    browser_session_ready: false,
  },
  providers: [{
    provider_type: "keycloak-oidc",
    alias: "workforce",
    display_name: "Company SSO",
    login_url: null,
  }],
  mcp_oauth: { enabled: true },
};

function InternalNavigation() {
  const navigate = useNavigate();
  return <><button onClick={() => navigate("/vault")}>Navigate internally</button><output aria-label="Current route">{useLocation().pathname}</output></>;
}

describe("settings — profile edit", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.getMe as any).mockResolvedValue(USER);
    (api.getAuthConfig as any).mockResolvedValue(LOCAL_AUTH_CONFIG);
    (api.updateProfile as any).mockResolvedValue({
      updated: true,
      username: USER.username,
      display_name: "Alice Renamed",
      email: USER.email,
    });
  });

  afterEach(() => cleanup());

  it.each(["Display name", "Current password"])("publishes the %s dirty guard in the edited commit, before passive effects", label => {
    const onDirtyChange = vi.fn();
    const committed: boolean[] = [];
    render(<Profiler id="profile-guard" onRender={() => {
      // Profiler observes committed DOM after child layout effects and before
      // passive effects. Do not inject a second event into an unfinished render.
      const input = document.getElementById(label === "Display name" ? "profile-display-name" : "pw-current") as HTMLInputElement | null;
      if (input?.value === "Immediate draft") committed.push(onDirtyChange.mock.lastCall?.[0] === true);
    }}><ProfileSection user={USER} localPasswordEnabled localProfileEditingEnabled onUserUpdate={vi.fn()} onDirtyChange={onDirtyChange} /></Profiler>);
    fireEvent.change(screen.getByLabelText(label), { target: { value: "Immediate draft" } });
    expect(committed.length).toBeGreaterThan(0);
    expect(committed.every(Boolean)).toBe(true);
  });

  it("publishes the busy guard in the saving commit, before passive effects", async () => {
    let finish!: (value: Awaited<ReturnType<typeof api.updateProfile>>) => void;
    vi.mocked(api.updateProfile).mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    const onBusyChange = vi.fn();
    const committed: boolean[] = [];
    render(<Profiler id="profile-busy-guard" onRender={() => {
      if (document.querySelector('form[aria-busy="true"]')) committed.push(onBusyChange.mock.lastCall?.[0] === true);
    }}><ProfileSection user={USER} localPasswordEnabled localProfileEditingEnabled onUserUpdate={vi.fn()} onBusyChange={onBusyChange} /></Profiler>);
    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "Alice Renamed" } });
    fireEvent.click(screen.getByRole("button", { name: "Save profile" }));
    expect(committed.length).toBeGreaterThan(0);
    expect(committed.every(Boolean)).toBe(true);
    await act(async () => finish({ updated: true, username: USER.username, display_name: "Alice Renamed", email: USER.email }));
  });

  it("shows typed wrong-current-password feedback on that field without clearing any draft", async () => {
    vi.mocked(api.changePassword).mockRejectedValue(Object.assign(new Error("Current password is incorrect"), { detail: { code: "current_password_incorrect" } }));
    renderSettings();
    await screen.findByLabelText("Current password");
    fireEvent.change(screen.getByLabelText("Current password"), { target: { value: "wrong" } });
    fireEvent.change(screen.getByLabelText("New password"), { target: { value: "new-password" } });
    fireEvent.change(screen.getByLabelText("Confirm new password"), { target: { value: "new-password" } });
    fireEvent.click(screen.getByRole("button", { name: "Change password" }));
    await screen.findByText("Current password is incorrect");
    expect(screen.getByLabelText("Current password")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText("Current password")).toHaveFocus();
    expect(screen.getByLabelText("New password")).toHaveValue("new-password");
    fireEvent.change(screen.getByLabelText("Current password"), { target: { value: "corrected" } });
    expect(screen.getByLabelText("Current password")).not.toHaveAttribute("aria-invalid");
    expect(screen.queryByText("Current password is incorrect")).not.toBeInTheDocument();
  });

  it("ends only the submitted login generation after a verified password change", async () => {
    vi.mocked(api.changePassword).mockResolvedValue({ ok: true });
    vi.mocked(api.clearCompletedAccountSession).mockReturnValueOnce(true);
    const completed = vi.fn();
    render(<ProfileSection user={USER} localPasswordEnabled localProfileEditingEnabled onUserUpdate={vi.fn()} onPasswordChanged={completed} />);
    fireEvent.change(screen.getByLabelText("Current password"), { target: { value: "old" } });
    fireEvent.change(screen.getByLabelText("New password"), { target: { value: "new-password" } });
    fireEvent.change(screen.getByLabelText("Confirm new password"), { target: { value: "new-password" } });
    fireEvent.click(screen.getByRole("button", { name: "Change password" }));
    await waitFor(() => expect(completed).toHaveBeenCalledOnce());
    expect(api.clearCompletedAccountSession).toHaveBeenCalledWith({ generation: 0, token: "fake-jwt" });
    expect(screen.queryByText("Current session stays active")).not.toBeInTheDocument();
  });

  it("masks same-account profile/password drafts during proof and preserves them after refresh", async () => {
    const view = (checking: boolean, revision: number) => <MemoryRouter><CurrentUserProvider user={{ ...USER, auth_method: "local", key_class: null }} checking={checking} revision={revision}><SettingsDialog onClose={vi.fn()} /></CurrentUserProvider></MemoryRouter>;
    const { rerender } = render(view(false, 0));
    const name = await screen.findByLabelText("Display name");
    fireEvent.change(name, { target: { value: "Active draft" } });
    fireEvent.change(screen.getByLabelText("Current password"), { target: { value: "private draft" } });
    rerender(view(true, 0));
    expect(name).not.toBeVisible();
    expect(screen.getByLabelText("Current password")).not.toBeVisible();
    rerender(view(false, 1));
    await waitFor(() => expect(name).toBeVisible());
    expect(name).toHaveValue("Active draft");
    expect(screen.getByLabelText("Current password")).toHaveValue("private draft");
  });

  it("keeps a hidden write guarded until its success is accepted", async () => {
    let resolve!: (value: Awaited<ReturnType<typeof api.updateProfile>>) => void;
    vi.mocked(api.updateProfile).mockReturnValueOnce(new Promise(r => { resolve = r; }));
    const close = vi.fn();
    const view = (checking: boolean, revision: number) => <MemoryRouter initialEntries={["/search"]}><CurrentUserProvider user={{ ...USER, auth_method: "local", key_class: null }} checking={checking} revision={revision}><ResourceNavigationProvider><InternalNavigation /><SettingsDialog onClose={close} /></ResourceNavigationProvider></CurrentUserProvider></MemoryRouter>;
    const { rerender } = render(view(false, 0));
    fireEvent.change(await screen.findByLabelText("Display name"), { target: { value: "Submitted name" } });
    fireEvent.click(screen.getByRole("button", { name: "Save profile" }));
    await waitFor(() => expect(api.updateProfile).toHaveBeenCalled());
    rerender(view(true, 0));
    fireEvent.click(screen.getByText("Navigate internally"));
    expect(screen.getByLabelText("Current route")).toHaveTextContent("/search");
    expect(close).not.toHaveBeenCalled();
    vi.mocked(api.getMe).mockResolvedValue({ ...USER, display_name: "Submitted name", auth_method: "local", key_class: null });
    await act(async () => resolve({ updated: true, username: USER.username, display_name: "Submitted name", email: USER.email }));
    rerender(view(false, 1));
    await waitFor(() => expect(screen.getByLabelText("Display name")).toBeVisible());
    expect(screen.getByLabelText("Display name")).toHaveValue("Submitted name");
    fireEvent.click(screen.getByText("Navigate internally"));
    expect(screen.getByLabelText("Current route")).toHaveTextContent("/vault");
    expect(screen.queryByRole("dialog", { name: "Discard unsaved changes?" })).not.toBeInTheDocument();
  });

  it("hydrates the form with the current user payload", async () => {
    renderSettings();
    expect(await screen.findByDisplayValue("Alice Original")).toBeTruthy();
    expect(screen.getByDisplayValue("alice@example.com")).toBeTruthy();
    expect(
      await screen.findByText("Change password", { selector: "#change-pw-heading" }),
    ).toBeInTheDocument();
  });

  it("hides the local password lifecycle in SSO mode", async () => {
    (api.getAuthConfig as any).mockResolvedValue(SSO_AUTH_CONFIG);

    renderSettings();

    await screen.findByDisplayValue("Alice Original");
    await waitFor(() => expect(api.getAuthConfig as any).toHaveBeenCalledOnce());
    expect(screen.queryByText("Change password", { selector: "#change-pw-heading" })).toBeNull();
    expect(screen.queryByLabelText(/current password/i)).toBeNull();
    expect(screen.getByLabelText(/display name/i)).toBeDisabled();
    expect(screen.getByLabelText(/email/i)).toBeDisabled();
    expect(screen.queryByRole("button", { name: /save profile/i })).toBeNull();
    expect(screen.getByText(/managed by your identity provider/i)).toBeInTheDocument();
  });

  it("'Save profile' is disabled when nothing changed", async () => {
    renderSettings();
    await screen.findByDisplayValue("Alice Original");
    // Save is dirty-gated: with no edits it's disabled (not a no-op click that
    // surfaces a "nothing to save" notice), so it never submits.
    expect(screen.getByRole("button", { name: /save profile/i })).toBeDisabled();
    expect(api.updateProfile as any).not.toHaveBeenCalled();
  });

  it("does not mark formatting-only surrounding whitespace as unsaved", async () => {
    const onDirtyChange = vi.fn();
    render(<ProfileSection user={USER} localPasswordEnabled localProfileEditingEnabled onUserUpdate={vi.fn()} onDirtyChange={onDirtyChange} />);
    const name = await screen.findByDisplayValue("Alice Original");

    fireEvent.change(name, { target: { value: "  Alice Original  " } });

    await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(false));
    expect(screen.getByRole("button", { name: /save profile/i })).toBeDisabled();
    expect(api.updateProfile as any).not.toHaveBeenCalled();
  });

  it("does not mark legacy stored whitespace as an edit when settings opens", async () => {
    const onDirtyChange = vi.fn();
    const legacyUser = {
      ...USER,
      display_name: "  Alice Original  ",
      email: "  alice@example.com  ",
    };

    render(<ProfileSection user={legacyUser} localPasswordEnabled localProfileEditingEnabled onUserUpdate={vi.fn()} onDirtyChange={onDirtyChange} />);

    expect(await screen.findByDisplayValue("Alice Original")).toBeInTheDocument();
    expect(screen.getByDisplayValue("alice@example.com")).toBeInTheDocument();
    await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(false));
    expect(screen.getByRole("button", { name: /save profile/i })).toBeDisabled();
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

  it("reports profile changes and clears the guard when reverted or unmounted", () => {
    const onDirtyChange = vi.fn();
    const { unmount } = render(<ProfileSection user={USER} localPasswordEnabled localProfileEditingEnabled onUserUpdate={vi.fn()} onDirtyChange={onDirtyChange} />);
    expect(onDirtyChange).toHaveBeenLastCalledWith(false);
    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "Changed" } });
    expect(onDirtyChange).toHaveBeenLastCalledWith(true);
    const event = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: USER.display_name } });
    expect(onDirtyChange).toHaveBeenLastCalledWith(false);
    fireEvent.change(screen.getByLabelText("Email address"), { target: { value: "other@example.com" } });
    unmount();
    expect(onDirtyChange).toHaveBeenLastCalledWith(false);
    const cleanEvent = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(cleanEvent);
    expect(cleanEvent.defaultPrevented).toBe(false);
  });

  it("guards password-only work and clears it after a successful password change", async () => {
    vi.mocked(api.changePassword).mockResolvedValue({ ok: true });
    const onDirtyChange = vi.fn();
    render(<ProfileSection user={USER} localPasswordEnabled localProfileEditingEnabled onUserUpdate={vi.fn()} onDirtyChange={onDirtyChange} />);
    fireEvent.change(screen.getByLabelText("Current password"), { target: { value: "oldpassword" } });
    expect(onDirtyChange).toHaveBeenLastCalledWith(true);
    const event = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
    fireEvent.change(screen.getByLabelText("New password"), { target: { value: "newpassword" } });
    fireEvent.change(screen.getByLabelText("Confirm new password"), { target: { value: "newpassword" } });
    fireEvent.click(screen.getByRole("button", { name: "Change password" }));
    await screen.findByText("Password changed. Sign in again with your new password.");
    expect(api.changePassword).toHaveBeenCalledWith("oldpassword", "newpassword");
    // The flash render does not guarantee the dirty-effect propagation has
    // settled (#553): wait for the final state instead of reading a
    // timing-dependent last-call order.
    await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(false));
    expect(screen.getByLabelText("Current password")).toHaveValue("");
  });

  it("keeps an unsaved profile guarded after saving the password", async () => {
    vi.mocked(api.changePassword).mockResolvedValue({ ok: true });
    const onDirtyChange = vi.fn();
    render(<ProfileSection user={USER} localPasswordEnabled localProfileEditingEnabled onUserUpdate={vi.fn()} onDirtyChange={onDirtyChange} />);
    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "Not saved" } });
    for (const label of ["Current password", "New password", "Confirm new password"]) {
      fireEvent.change(screen.getByLabelText(label), { target: { value: "password123" } });
    }
    fireEvent.click(screen.getByRole("button", { name: "Change password" }));
    await screen.findByText("Password changed. Sign in again with your new password.");
    await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(true));
  });

  it("keeps the modal busy until both profile and password writes finish", async () => {
    let finishProfile!: () => void;
    let finishPassword!: () => void;
    vi.mocked(api.updateProfile).mockImplementation(async () => {
      await new Promise<void>(resolve => { finishProfile = resolve; });
      return { updated: true, username: USER.username, display_name: "Alice Renamed", email: USER.email };
    });
    vi.mocked(api.changePassword).mockImplementation(async () => {
      await new Promise<void>(resolve => { finishPassword = resolve; });
      return { ok: true };
    });
    const onBusyChange = vi.fn();
    render(<ProfileSection user={USER} localPasswordEnabled localProfileEditingEnabled onUserUpdate={vi.fn()} onBusyChange={onBusyChange} />);
    expect(onBusyChange).toHaveBeenLastCalledWith(false);

    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "Alice Renamed" } });
    fireEvent.click(screen.getByRole("button", { name: "Save profile" }));
    expect(onBusyChange).toHaveBeenLastCalledWith(true);
    expect(screen.getByLabelText("Display name")).toBeDisabled();
    for (const label of ["Current password", "New password", "Confirm new password"]) {
      fireEvent.change(screen.getByLabelText(label), { target: { value: "password123" } });
    }
    fireEvent.click(screen.getByRole("button", { name: "Change password" }));
    expect(screen.getByLabelText("Current password")).toBeDisabled();

    await act(async () => { finishProfile(); });
    expect(screen.getByLabelText("Display name")).toBeEnabled();
    expect(onBusyChange).toHaveBeenLastCalledWith(true);
    await act(async () => { finishPassword(); });
    expect(onBusyChange).toHaveBeenLastCalledWith(false);
    expect(screen.getByLabelText("Current password")).toBeEnabled();
  });

  it("clears the busy guard on failure and unmount", async () => {
    let failProfile!: (reason: Error) => void;
    vi.mocked(api.updateProfile).mockImplementation(() => new Promise((_, reject) => { failProfile = reject; }));
    const onBusyChange = vi.fn();
    const { unmount } = render(<ProfileSection user={USER} localPasswordEnabled localProfileEditingEnabled onUserUpdate={vi.fn()} onBusyChange={onBusyChange} />);
    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "Alice Renamed" } });
    fireEvent.click(screen.getByRole("button", { name: "Save profile" }));
    expect(onBusyChange).toHaveBeenLastCalledWith(true);
    await act(async () => { failProfile(new Error("Save unavailable")); });
    expect(screen.getByRole("alert")).toHaveTextContent("Save unavailable");
    expect(onBusyChange).toHaveBeenLastCalledWith(false);

    fireEvent.click(screen.getByRole("button", { name: "Save profile" }));
    expect(onBusyChange).toHaveBeenLastCalledWith(true);
    unmount();
    expect(onBusyChange).toHaveBeenLastCalledWith(false);
    await act(async () => { failProfile(new Error("Save unavailable")); });
  });

  it("keeps the compact profile identity once and omits outer cards", () => {
    const { container } = render(<ProfileSection user={USER} localPasswordEnabled localProfileEditingEnabled onUserUpdate={vi.fn()} />);
    expect(screen.getAllByText("@alice")).toHaveLength(1);
    expect(screen.getByRole("region", { name: "Profile" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Change password" })).toBeInTheDocument();
    expect(container.querySelector('[data-slot="panel"]')).toBeNull();
    expect(screen.getByLabelText("New password")).toHaveAccessibleDescription("Use at least 8 characters.");
  });
});
