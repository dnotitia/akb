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
import { authLogin, authRegister, getToken, setToken } from "@/lib/api";

vi.mock("@/lib/api", () => ({
  authLogin: vi.fn(),
  authRegister: vi.fn(),
  setToken: vi.fn(),
  // null → not signed in, so AuthPage's authed-guard doesn't redirect on mount.
  getToken: vi.fn(() => null),
  // Optional Keycloak SSO probe — default disabled so the SSO button stays
  // hidden and AuthPage's mount effect resolves cleanly.
  getAuthConfig: vi.fn().mockResolvedValue({
    keycloak: { enabled: false, login_url: null },
  }),
  clearSsoSession: vi.fn(),
}));

const mockedLogin = vi.mocked(authLogin);
const mockedRegister = vi.mocked(authRegister);
const mockedSetToken = vi.mocked(setToken);

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
    await u.type(screen.getByLabelText("Username"), "alice");
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
    // Radix TabsTrigger renders as a button — getByText is the
    // narrowest selector that works in jsdom (Radix's tab role
    // assignment doesn't surface here consistently).
    await u.click(screen.getByText("Register"));
    await u.type(screen.getByLabelText("Username"), "bob");
    await u.type(screen.getByLabelText("Email"), "bob@x.test");
    // 8+ chars — register now enforces a client-side minimum (matching the
    // backend change_password rule) so accounts can't get an unchangeable pw.
    await u.type(screen.getByLabelText("Password"), "pw-12345");
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
    await u.type(screen.getByLabelText("Username"), "alice");
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
    await u.type(screen.getByLabelText("Username"), "alice");
    await u.type(screen.getByLabelText("Password"), "wrong");
    await u.click(screen.getByRole("button", { name: /sign in/i }));
    await screen.findByRole("alert");
    // Radix TabsTrigger renders as a button — getByText is the
    // narrowest selector that works in jsdom (Radix's tab role
    // assignment doesn't surface here consistently).
    await u.click(screen.getByText("Register"));
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

  it("rejects a register password under 8 chars without calling the API", async () => {
    const u = userEvent.setup();
    renderAuth();
    await u.click(screen.getByText("Register"));
    await u.type(screen.getByLabelText("Username"), "bob");
    await u.type(screen.getByLabelText("Email"), "bob@x.test");
    await u.type(screen.getByLabelText("Password"), "short");
    await u.click(screen.getByRole("button", { name: /create account/i }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/at least 8 characters/i);
    expect(mockedRegister).not.toHaveBeenCalled();
  });

  it("treats a token-less 200 login as an error (no token, no navigate)", async () => {
    mockedLogin.mockResolvedValue({});
    const u = userEvent.setup();
    renderAuth();
    await u.type(screen.getByLabelText("Username"), "alice");
    await u.type(screen.getByLabelText("Password"), "pw-1234");
    await u.click(screen.getByRole("button", { name: /sign in/i }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/no token/i);
    expect(mockedSetToken).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
  });
});
