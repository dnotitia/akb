import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
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

vi.mock("@/lib/api", async () => ({
  ApiError: (await vi.importActual<typeof import("@/lib/api")>("@/lib/api")).ApiError,
  createPAT: vi.fn(),
  createVault: vi.fn(),
  listVaultTemplates: vi.fn().mockResolvedValue([]),
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
    listPATsMock.mockResolvedValue({ tokens: [] });
    renderPage();

    expect(
      await screen.findByRole("heading", { level: 1, name: "Home" }),
    ).toHaveClass("sr-only");
    expect(screen.queryByText("Return to your documents and catch up on changes across your vaults.")).not.toBeInTheDocument();
    expect(screen.queryByRole("searchbox")).not.toBeInTheDocument();
    expect(screen.queryByText("Find what the team already knows.")).not.toBeInTheDocument();

    expect(screen.getByRole("heading", { name: "Your vaults" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Recent updates" })).toBeInTheDocument();
    expect(await screen.findByText(/Mina Park/)).toBeInTheDocument();
    expect(screen.getByText(/Deployment checks, rollback signals/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Hide connection guide" })).toBeInTheDocument();
    expect(screen.queryByText(/of 3 complete/)).not.toBeInTheDocument();
    expect(screen.queryByText("Knowledge index")).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Workspace" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Finish setup" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Set up a connection" })).toBeInTheDocument();

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
      await screen.findByRole("heading", { name: "Recently viewed" }),
    ).toBeInTheDocument();
    expect(screen.getByText("On this browser")).toBeInTheDocument();
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

    expect(await screen.findByRole("button", { name: "Connect an agent" })).toBeInTheDocument();
    expect(screen.queryByText("Agent connection active")).not.toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByRole("heading", { name: "Use AKB with your AI tools" })).not.toBeInTheDocument();
    });
  });

  it("lets a first-run user hide onboarding without opening a forced dialog", async () => {
    const user = userEvent.setup();
    listVaultsMock.mockResolvedValue({ vaults: [] });
    getRecentMock.mockResolvedValue({ changes: [] });
    listPATsMock.mockResolvedValue({ tokens: [] });

    renderPage();

    expect(await screen.findByRole("button", { name: "Create a vault" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Hide connection guide" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Hide connection guide" }));
    expect(screen.queryByRole("heading", { name: "Use AKB with your AI tools" })).not.toBeInTheDocument();
    expect(localStorage.getItem("akb.homeConnectionGuideDismissed:user-home")).toBe("1");
    await waitFor(() => expect(screen.getByRole("button", { name: "Show connection guide" })).toHaveFocus());

    await user.click(screen.getByRole("button", { name: "Show connection guide" }));
    expect(screen.getByRole("button", { name: "Hide connection guide" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Hide connection guide" })).toHaveFocus());
    expect(localStorage.getItem("akb.homeConnectionGuideDismissed:user-home")).toBeNull();
  });

  it("caps a large favorite set and restores focus if an unpinned card leaves Home", async () => {
    const vaults = Array.from({ length: 8 }, (_, i) => ({ id: `v${i}`, name: `vault-${i}`, role: "reader" }));
    listVaultsMock.mockResolvedValue({ vaults });
    localStorage.setItem("akb-vault-favorites:v2:user-home", JSON.stringify(vaults.map(v => v.id)));
    renderPage();
    const section = screen.getByRole("region", { name: "Your vaults" });
    await screen.findByRole("heading", { name: "vault-0" });
    expect(within(section).getAllByRole("heading", { level: 3 })).toHaveLength(4);
    expect(screen.getByRole("link", { name: /View all vaults \(8\)/ })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Remove vault-0 from favorites" }));
    expect(screen.queryByRole("heading", { name: "vault-0" })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("link", { name: /View all vaults/ })).toHaveFocus());
    expect(within(section).getAllByRole("heading", { level: 3 })).toHaveLength(4);
  });

  it("does not invent absent counts or repeat generic descriptions", async () => {
    listVaultsMock.mockResolvedValue({ vaults: [{ id: "v1", name: "legacy", role: "reader" }] });
    getVaultInfoMock.mockResolvedValue({ document_count: 3 });
    renderPage();
    await screen.findByText("Documents");
    expect(screen.queryByText("Tables")).not.toBeInTheDocument();
    expect(screen.queryByText("Files")).not.toBeInTheDocument();
    expect(screen.queryByText(/A shared knowledge space for your team/)).not.toBeInTheDocument();
    expect(screen.getByText("Read only")).toBeInTheDocument();
    expect(getVaultInfoMock).toHaveBeenCalledTimes(1);
  });

  it("does not label a failed connection lookup as incomplete setup", async () => {
    listPATsMock.mockRejectedValue(new Error("Offline"));
    renderPage();
    expect(await screen.findByRole("button", { name: "Connect an agent" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Use AKB with your AI tools" })).not.toBeInTheDocument();
  });

  it("opens the existing vault creation dialog from the first-run action", async () => {
    listVaultsMock.mockResolvedValue({ vaults: [] });
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Create a vault" }));
    expect(screen.getByRole("dialog", { name: "Create a vault" })).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.getByRole("button", { name: "Create a vault" })).toHaveFocus());
  });
});
