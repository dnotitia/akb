import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { AUTH_CONFIG_UNAVAILABLE, getAuthConfig } from "@/lib/api";
import AuthForgotPage from "../auth-forgot";


vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    getToken: vi.fn(() => null),
    getAuthConfig: vi.fn(),
  };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("AuthForgotPage", () => {
  it("renders password guidance only for validated local mode", async () => {
    vi.mocked(getAuthConfig).mockResolvedValue({
      available: true,
      schema_version: 1,
      auth_mode: "local",
      local_auth: { enabled: true },
      keycloak: {
        enabled: false,
        browser_session_ready: false,
        login_url: null,
      },
      mcp_oauth: { enabled: false },
    });

    render(<MemoryRouter><AuthForgotPage /></MemoryRouter>);

    expect(await screen.findByRole("heading", { name: /forgot your password/i })).toBeInTheDocument();
    expect(screen.getByText(/contact your administrator/i)).toBeInTheDocument();
  });

  it.each([
    ["sso", {
      available: true,
      schema_version: 1 as const,
      auth_mode: "sso" as const,
      local_auth: { enabled: false },
      keycloak: {
        enabled: true,
        browser_session_ready: false,
        login_url: null,
      },
      mcp_oauth: { enabled: false },
    }],
    ["unavailable", AUTH_CONFIG_UNAVAILABLE],
  ])("does not render local password guidance in %s state", async (_name, config) => {
    vi.mocked(getAuthConfig).mockResolvedValue(config);

    render(<MemoryRouter><AuthForgotPage /></MemoryRouter>);

    expect(await screen.findByRole("heading", { name: /password recovery unavailable/i })).toBeInTheDocument();
    expect(screen.queryByText(/temporary password/i)).toBeNull();
  });
});
