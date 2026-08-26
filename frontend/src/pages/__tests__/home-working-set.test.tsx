import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";

import HomePage from "@/pages/home";
import { CurrentUserProvider } from "@/contexts/current-user-context";
import { recordRecentDocumentView } from "@/lib/recent-document-views";
import {
  createPAT,
  getAuthConfig,
  getRecent,
  getVaultInfo,
  listPATs,
  listVaults,
} from "@/lib/api";

vi.mock("@/lib/api", () => ({
  createPAT: vi.fn(),
  getAuthConfig: vi.fn(),
  getRecent: vi.fn(),
  getVaultInfo: vi.fn(),
  listPATs: vi.fn(),
  listVaults: vi.fn(),
}));

const createPATMock = vi.mocked(createPAT);
const getAuthConfigMock = vi.mocked(getAuthConfig);
const getRecentMock = vi.mocked(getRecent);
const getVaultInfoMock = vi.mocked(getVaultInfo);
const listPATsMock = vi.mocked(listPATs);
const listVaultsMock = vi.mocked(listVaults);

const VAULTS = [
  {
    id: "vault-1",
    name: "platform",
    description: "Platform decisions and runbooks.",
    role: "owner",
    status: "active",
  },
  {
    id: "vault-2",
    name: "research",
    description: "Shared research notes.",
    role: "reader",
    status: "active",
  },
] as const;

const CURRENT_USER = {
  user_id: "user-home",
  username: "home-user",
  email: "home@example.com",
  display_name: "Home User",
  is_admin: false,
  auth_method: "local",
  key_class: null,
};

function TestLayout() {
  return (
    <CurrentUserProvider user={CURRENT_USER}>
      <Outlet context={{ indexingStatus: null }} />
    </CurrentUserProvider>
  );
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <Routes>
        <Route element={<TestLayout />}>
          <Route path="/" element={<HomePage />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  createPATMock.mockResolvedValue({ token: "akb_pat_test" });
  getAuthConfigMock.mockResolvedValue({
    available: false,
    schema_version: null,
    auth_mode: null,
    local_auth: { enabled: false },
    keycloak: { enabled: false, browser_session_ready: false },
    providers: [],
    mcp_oauth: { enabled: false },
  });
  listVaultsMock.mockResolvedValue({ vaults: [...VAULTS] });
  getVaultInfoMock.mockResolvedValue({
    document_count: 3,
    table_count: 1,
    file_count: 0,
    last_activity: "2026-08-25T00:00:00Z",
  });
  getRecentMock.mockResolvedValue({
    changes: [
      {
        doc_id: "doc-1",
        vault: "platform",
        path: "runbooks/deploy.md",
        title: "Deploy runbook",
        type: "document",
        commit: "1234567890",
        changed_at: "2026-08-25T00:00:00Z",
        updated_by_name: "Mina Park",
        action: "update",
        excerpt: "Deployment checks, rollback signals, and the recovery sequence.",
      },
    ],
  });
  listPATsMock.mockResolvedValue({
    tokens: [
      {
        token_id: "token-1",
        name: "agent-token",
        prefix: "akb_pat",
        last_used_at: null,
      },
    ],
  });
});

afterEach(cleanup);

describe("Home working set", () => {
  it("keeps search global and presents Vaults, activity, and inline setup", async () => {
    renderPage();

    expect(
      await screen.findByRole("heading", { level: 1, name: "Your workspace" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("searchbox")).not.toBeInTheDocument();
    expect(screen.queryByText("Find what the team already knows.")).not.toBeInTheDocument();

    expect(screen.getByRole("heading", { name: "Your vaults" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Recent updates" })).toBeInTheDocument();
    expect(screen.getByText("Mina Park")).toBeInTheDocument();
    expect(screen.getByText(/Deployment checks, rollback signals/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Connect an agent" })).toBeInTheDocument();
    expect(screen.getByText("2 of 3 complete")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Finish setup" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Finish connection" })).toHaveAttribute(
      "href",
      "/settings?tab=tokens",
    );

    expect(screen.queryByText("Recent changes")).not.toBeInTheDocument();
    expect(screen.queryByText("Mint token")).not.toBeInTheDocument();
    expect(screen.queryByText("Client config")).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("offers a browser-local document as a compact continue-working entry", async () => {
    recordRecentDocumentView(CURRENT_USER.user_id, {
      vault: "platform",
      path: "incidents/august-review.md",
      title: "August incident review",
      type: "report",
      updatedAt: "2026-08-25T04:00:00Z",
    });

    renderPage();

    expect(
      await screen.findByRole("heading", { name: "Continue working" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Recent on this browser")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /August incident review/ }),
    ).toHaveAttribute(
      "href",
      "/vault/platform/doc/incidents%2Faugust-review.md",
    );
  });

  it("keeps recent updates compact when an older backend omits enrichment", async () => {
    getRecentMock.mockResolvedValue({
      changes: [
        {
          doc_id: "legacy-doc",
          vault: "platform",
          path: "legacy.md",
          title: "Legacy response",
          type: "note",
          commit: "abcdef0",
          changed_at: "2026-08-25T00:00:00Z",
        },
      ],
    });

    renderPage();

    expect(await screen.findByText("Legacy response")).toBeInTheDocument();
    expect(screen.queryByText(/updated this document/i)).toBeNull();
    expect(screen.queryByText(/^Created by /i)).toBeNull();
  });

  it("removes onboarding after an agent has actually used a token", async () => {
    listPATsMock.mockResolvedValue({
      tokens: [
        {
          token_id: "token-1",
          name: "agent-token",
          prefix: "akb_pat",
          last_used_at: "2026-08-25T01:00:00Z",
        },
      ],
    });

    renderPage();

    expect(await screen.findByText("Agent connection active")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByRole("heading", { name: "Finish setup" })).not.toBeInTheDocument();
    });
  });

  it("lets a first-run user hide onboarding without opening a forced dialog", async () => {
    const user = userEvent.setup();
    listVaultsMock.mockResolvedValue({ vaults: [] });
    getRecentMock.mockResolvedValue({ changes: [] });
    listPATsMock.mockResolvedValue({ tokens: [] });

    renderPage();

    expect(await screen.findByText("0 of 3 complete")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Connect an agent" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Hide" }));
    expect(screen.queryByRole("heading", { name: "Finish setup" })).not.toBeInTheDocument();
    expect(localStorage.getItem("akb.homeSetupDismissed")).toBe("1");

    await user.click(screen.getByRole("button", { name: "Show setup" }));
    expect(screen.getByRole("heading", { name: "Connect an agent" })).toBeInTheDocument();
    expect(localStorage.getItem("akb.homeSetupDismissed")).toBeNull();
  });
});
