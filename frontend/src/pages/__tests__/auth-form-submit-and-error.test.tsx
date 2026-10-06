// RTL coverage for AuthPage. Auth is the single funnel — every user
// arrives via /auth — yet shipped with zero UI tests for the most
// common failure modes.
//
// Guards:
//   - register submits the right fields to authRegister + then logs in
//   - login submits to authLogin + stores the token + navigates
//   - server errors render in the role=alert region
//   - switching tabs clears the previous error message
//
// We mock @/lib/api at the module boundary; the wire-level fetch
// contract lives in lib/__tests__/api-search-contract.test.ts (and a
// similar pattern for auth could be added there as it grows).

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

import AuthPage from "../auth";
import { authLogin, authRegister, getMe, getToken, setToken } from "@/lib/api";

vi.mock("@/lib/api", () => ({
  authLogin: vi.fn(),
  authRegister: vi.fn(),
  clearLegacySsoSession: vi.fn(),
  setToken: vi.fn(),
  // null → not signed in, so AuthPage's authed-guard doesn't redirect on mount.
  getToken: vi.fn(() => null),
  getMe: vi.fn(),
  // Optional Keycloak SSO probe — default disabled so the SSO button stays
  // hidden and AuthPage's mount effect resolves cleanly.
  getAuthConfig: vi.fn().mockResolvedValue({
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
  }),
}));

const mockedLogin = vi.mocked(authLogin);
const mockedRegister = vi.mocked(authRegister);
const mockedSetToken = vi.mocked(setToken);
const mockedGetMe = vi.mocked(getMe);

const navigate = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>(
    "react-router-dom",
  );
  return {
    ...actual,
    useNavigate: () => navigate,
  };
});

afterEach(cleanup);
beforeEach(() => {
  mockedLogin.mockReset();
  mockedRegister.mockReset();
  mockedSetToken.mockReset();
  mockedGetMe.mockReset();
  mockedGetMe.mockResolvedValue({
    user_id: "user-1",
    username: "alice",
    email: "alice@example.com",
    display_name: "Alice",
    is_admin: false,
    auth_method: "jwt",
    key_class: null,
  });
  navigate.mockReset();
});

function renderAuth() {
  return render(
    <MemoryRouter initialEntries={["/auth"]}>
      <AuthPage />
    </MemoryRouter>,
  );
}

describe("AuthPage · login happy path", () => {
  it("submits username+password, stores token, and navigates to /", async () => {
    mockedLogin.mockResolvedValue({ token: "tok-xyz" });
    const u = userEvent.setup();
    renderAuth();
    await u.type(await screen.findByLabelText("Username"), "alice");
    await u.type(screen.getByLabelText("Password"), "pw-1234");
    await u.click(screen.getByRole("button", { name: /sign in/i }));
    await waitFor(() =>
      expect(mockedLogin).toHaveBeenCalledWith("alice", "pw-1234"),
    );
    expect(mockedSetToken).toHaveBeenCalledWith("tok-xyz");
    expect(navigate).toHaveBeenCalledWith("/");
  });
});

describe("AuthPage · register flow", () => {
  it("calls authRegister with (username, email, password, displayName), then logs in", async () => {
    mockedRegister.mockResolvedValue({});
    mockedLogin.mockResolvedValue({ token: "tok-new" });
    const u = userEvent.setup();
    renderAuth();
    await u.click(await screen.findByRole("tab", { name: "Create account" }));
    await u.type(screen.getByLabelText("Username"), "bob");
    await u.type(screen.getByLabelText("Email"), "bob@x.test");
    // 8+ chars — register enforces a client-side minimum (matching the
    // backend change_password rule) so accounts can't get an unchangeable pw.
    await u.type(screen.getByLabelText("Password"), "pw-12345");
    await u.type(screen.getByLabelText("Confirm password"), "pw-12345");
    await u.click(screen.getByRole("button", { name: /create account/i }));
    await waitFor(() =>
      expect(mockedRegister).toHaveBeenCalledWith(
        "bob",
        "bob@x.test",
        "pw-12345",
        undefined,
      ),
    );
    await waitFor(() =>
      expect(mockedLogin).toHaveBeenCalledWith("bob", "pw-12345"),
    );
    expect(mockedSetToken).toHaveBeenCalledWith("tok-new");
  });
});

describe("AuthPage · error surface", () => {
  it("renders server error in the role=alert region", async () => {
    mockedLogin.mockResolvedValue({ error: "Bad credentials" });
    const u = userEvent.setup();
    renderAuth();
    await u.type(await screen.findByLabelText("Username"), "alice");
    await u.type(screen.getByLabelText("Password"), "wrong");
    await u.click(screen.getByRole("button", { name: /sign in/i }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/Bad credentials/);
    expect(mockedSetToken).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
  });

  it("clears the error message when switching tabs", async () => {
    mockedLogin.mockResolvedValue({ error: "Bad credentials" });
    const u = userEvent.setup();
    renderAuth();
    await u.type(await screen.findByLabelText("Username"), "alice");
    await u.type(screen.getByLabelText("Password"), "wrong");
    await u.click(screen.getByRole("button", { name: /sign in/i }));
    await screen.findByRole("alert");
    await u.click(screen.getByRole("tab", { name: "Create account" }));
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("AuthPage · guards & validation", () => {
  it("redirects (no form) when already signed in", async () => {
    vi.mocked(getToken).mockReturnValueOnce("existing-token");
    renderAuth();
    await waitFor(() =>
      expect(navigate).toHaveBeenCalledWith("/", { replace: true }),
    );
  });

  it("shows field-level errors instead of browser bubbles and focuses the first one", async () => {
    const u = userEvent.setup();
    renderAuth();
    await u.click(await screen.findByRole("tab", { name: "Create account" }));
    await u.type(screen.getByLabelText("Username"), "bob");
    await u.type(screen.getByLabelText("Email"), "not-an-email");
    await u.type(screen.getByLabelText("Password"), "short");
    await u.type(screen.getByLabelText("Confirm password"), "shorter");
    await u.click(screen.getByRole("button", { name: /create account/i }));
    const email = screen.getByLabelText("Email");
    expect(email).toHaveAttribute("aria-invalid", "true");
    expect(email).toHaveAccessibleDescription("Enter a valid email address.");
    expect(screen.getByLabelText("Password")).toHaveAccessibleDescription(/at least 8 characters/i);
    expect(screen.getByLabelText("Confirm password")).toHaveAccessibleDescription("Doesn't match the password.");
    expect(screen.getByLabelText("Username")).not.toHaveAttribute("aria-invalid");
    await waitFor(() => expect(email).toHaveFocus());
    expect(mockedRegister).not.toHaveBeenCalled();
  });

  it("keeps each tab's draft separate", async () => {
    const u = userEvent.setup();
    renderAuth();
    await u.click(await screen.findByRole("tab", { name: "Create account" }));
    await u.type(screen.getByLabelText("Username"), "bob");
    await u.type(screen.getByLabelText("Password"), "half");
    await u.click(screen.getByRole("tab", { name: "Sign in" }));
    expect(screen.getByLabelText("Username")).toHaveValue("");
    expect(screen.getByLabelText("Password")).toHaveValue("");
  });

  it("clears and refocuses the password after a failed sign-in", async () => {
    mockedLogin.mockResolvedValue({ error: "Invalid credentials" });
    const u = userEvent.setup();
    renderAuth();
    await u.type(await screen.findByLabelText("Username"), "alice");
    await u.type(screen.getByLabelText("Password"), "wrong-pw");
    await u.click(screen.getByRole("button", { name: /sign in/i }));
    await screen.findByRole("alert");
    expect(screen.getByLabelText("Username")).toHaveValue("alice");
    expect(screen.getByLabelText("Password")).toHaveValue("");
    await waitFor(() => expect(screen.getByLabelText("Password")).toHaveFocus());
  });

  it("moves to sign-in with an info notice when auto-login after registration fails", async () => {
    mockedRegister.mockResolvedValue({});
    mockedLogin.mockResolvedValue({ error: "temporarily unavailable" });
    const u = userEvent.setup();
    renderAuth();
    await u.click(await screen.findByRole("tab", { name: "Create account" }));
    await u.type(screen.getByLabelText("Username"), "bob");
    await u.type(screen.getByLabelText("Email"), "bob@x.test");
    await u.type(screen.getByLabelText("Password"), "pw-12345");
    await u.type(screen.getByLabelText("Confirm password"), "pw-12345");
    await u.click(screen.getByRole("button", { name: /create account/i }));
    expect(await screen.findByText("Account created. Sign in to continue.")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Sign in" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByLabelText("Username")).toHaveValue("bob");
    expect(screen.getByLabelText("Username")).not.toHaveAttribute("aria-invalid");
  });

  it("toggles password visibility", async () => {
    const u = userEvent.setup();
    renderAuth();
    const password = await screen.findByLabelText("Password");
    expect(password).toHaveAttribute("type", "password");
    await u.click(screen.getByRole("button", { name: "Show password" }));
    expect(password).toHaveAttribute("type", "text");
    expect(screen.getByRole("button", { name: "Hide password" })).toHaveAttribute("aria-pressed", "true");
  });

  it("treats a token-less 200 login as an error (no token, no navigate)", async () => {
    mockedLogin.mockResolvedValue({});
    const u = userEvent.setup();
    renderAuth();
    await u.type(await screen.findByLabelText("Username"), "alice");
    await u.type(screen.getByLabelText("Password"), "pw-1234");
    await u.click(screen.getByRole("button", { name: /sign in/i }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/no session/i);
    expect(mockedSetToken).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
  });
});
