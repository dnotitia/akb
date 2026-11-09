import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import VaultSettingsPage from "@/pages/vault-settings";
import { CurrentUserProvider } from "@/contexts/current-user-context";
import { ResourceNavigationProvider } from "@/contexts/resource-navigation-context";
import { AppPageLocation } from "@/components/app-page-location";
import { installDocumentHistoryGuard } from "@/hooks/use-document-history-guard";

// These tests exercise Settings state, identity and writes. Markdown rendering
// has real-renderer coverage in guide-preview.test.tsx and vault-guide-flow.test.tsx.
vi.mock("@/components/skill/guide-preview", () => ({
  default: ({ content }: { content: string }) => <div role="region" aria-label="Vault guide preview">{content}</div>,
}));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/api")>(),
  archiveVault: vi.fn(),
  authenticatedFetch: vi.fn().mockResolvedValue({ ok: false }),
  deleteVaultPermanent: vi.fn(),
  getDocument: vi.fn().mockRejectedValue(new Error("missing guide")),
  getSkillTemplate: vi.fn(),
  getVaultInfo: vi.fn(),
  getVaultSkillPreview: vi.fn(),
  unarchiveVault: vi.fn(),
  updateDocument: vi.fn(),
  updateVault: vi.fn(),
}));

import {
  archiveVault,
  deleteVaultPermanent,
  getDocument,
  getVaultInfo,
  unarchiveVault,
  updateDocument,
  updateVault,
} from "@/lib/api";

const getVaultInfoMock = getVaultInfo as unknown as ReturnType<typeof vi.fn>;
const updateVaultMock = updateVault as unknown as ReturnType<typeof vi.fn>;

const VAULT_INFO = {
  name: "platform-docs",
  description: "Product and platform knowledge.",
  role: "owner",
  public_access: "none",
  owner_display_name: "Vault Owner",
  created_at: "2026-08-20T10:00:00Z",
  last_activity: "2026-08-24T10:00:00Z",
  member_count: 4,
  collection_count: 3,
  document_count: 18,
  table_count: 2,
  file_count: 5,
  edge_count: 9,
};

function BackButton() {
  const navigate = useNavigate();
  return <><button onClick={() => navigate(-1)}>Browser back</button>
    <button onClick={() => navigate("/vault")}>Leave settings</button>
    <button onClick={() => navigate("/vault/another/settings")}>Another vault</button>
    <button onClick={() => navigate("#access")}>Jump to access</button>
    <button onClick={() => navigate("#skill")}>Jump to guide</button></>;
}

function scope(name: "Vault details" | "Access permissions" | "Vault guide" | "Danger zone") {
  return within(screen.getByRole("region", { name }));
}

async function selectTab(user: ReturnType<typeof userEvent.setup>, name: string) {
  const tab = await screen.findByRole("tab", { name });
  await waitFor(() => expect(tab).toBeEnabled());
  if (tab.getAttribute("aria-selected") !== "true") await user.click(tab);
}

function SettingsLocationProbe() {
  const location = useLocation();
  return <><output data-testid="settings-location">{location.pathname + location.search + location.hash}</output>
    <output data-testid="settings-preview-state">{JSON.stringify(location.state)}</output></>;
}

function renderSettings(hash = "") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/vault/platform-docs/settings${hash}`]}>
        <ResourceNavigationProvider>
        <BackButton />
        <SettingsLocationProbe />
        <AppPageLocation />
        <Routes>
          <Route path="/vault/:name/settings" element={<VaultSettingsPage />} />
          <Route path="/vault/:name" element={<h1>Vault overview</h1>} />
          <Route path="/vault/:name/members" element={<h1>Vault members</h1>} />
          <Route path="/vault" element={<h1>Vault directory</h1>} />
        </Routes>
        </ResourceNavigationProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function renderBrowserSettings() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}><BrowserRouter><ResourceNavigationProvider>
    <AppPageLocation />
    <Routes>
      <Route path="/vault/:name/settings" element={<VaultSettingsPage />} />
      <Route path="/vault/:name" element={<h1>Vault overview</h1>} />
          <Route path="/vault/:name/members" element={<h1>Vault members</h1>} />
    </Routes>
  </ResourceNavigationProvider></BrowserRouter></QueryClientProvider>);
}

beforeEach(() => {
  vi.clearAllMocks();
  getVaultInfoMock.mockResolvedValue(VAULT_INFO);
  updateVaultMock.mockResolvedValue({ ok: true });
  vi.mocked(getDocument).mockResolvedValue({
    content: "Original guide",
    current_commit: "original-commit",
  });
});

afterEach(cleanup);

describe("Vault Settings redesign", () => {
  it("accepts assistive click activation without a pointer-down sequence", async () => {
    renderSettings();
    await screen.findByRole("textbox", { name: "Description" });
    const accessTab = screen.getByRole("tab", { name: "Access permissions" });
    fireEvent.click(accessTab);
    expect(accessTab).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel", { name: "Access permissions" })).toBeVisible();
    expect(screen.queryByRole("textbox", { name: "Description" })).not.toBeInTheDocument();
  });

  it("allows guarded departure while access proof is pending or fails without losing a cancelled draft", async () => {
    const user = userEvent.setup();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const account = { user_id: "a", username: "a", email: "a@x.test", display_name: null, is_admin: false, auth_method: "local", key_class: null };
    const view = (checking = false, revision = 0) => <QueryClientProvider client={client}><MemoryRouter initialEntries={["/vault/platform-docs/settings"]}><CurrentUserProvider user={account} checking={checking} revision={revision}><ResourceNavigationProvider>
      <AppPageLocation />
      <Routes><Route path="/vault/:name/settings" element={<VaultSettingsPage />} /><Route path="/vault/:name" element={<h1>Vault overview</h1>} />
          <Route path="/vault/:name/members" element={<h1>Vault members</h1>} /></Routes>
    </ResourceNavigationProvider></CurrentUserProvider></MemoryRouter></QueryClientProvider>;
    const { rerender } = render(view());
    fireEvent.change(await screen.findByLabelText("Description"), { target: { value: "Private unsaved description" } });
    rerender(view(true));
    await user.click(screen.getByRole("link", { name: "Back to platform-docs" }));
    expect(screen.getByRole("dialog", { name: "Discard unsaved settings changes?" })).not.toHaveTextContent("Private unsaved description");
    await user.click(screen.getByRole("button", { name: "Keep editing" }));
    expect(screen.getByLabelText("Description")).toHaveValue("Private unsaved description");
    getVaultInfoMock.mockRejectedValueOnce(new Error("Access could not be verified"));
    rerender(view(false, 1));
    expect(await screen.findByRole("alert")).toHaveTextContent("Access could not be verified");
    await user.click(screen.getByRole("link", { name: "Back to platform-docs" }));
    await user.click(screen.getByRole("button", { name: "Keep editing" }));
    expect(screen.getByLabelText("Description")).toHaveValue("Private unsaved description");
    await user.click(screen.getByRole("link", { name: "Back to platform-docs" }));
    await user.click(screen.getByRole("button", { name: "Discard changes and leave" }));
    expect(await screen.findByRole("heading", { name: "Vault overview" })).toBeVisible();
    expect(updateVault).not.toHaveBeenCalled();
  });

  it.each(["Open guide", "Edit guide"])("routes %s through the same retained document preview", async entry => {
    const user = userEvent.setup();
    renderSettings("#skill");
    await user.click(await screen.findByRole(entry === "Open guide" ? "link" : "button", { name: entry }));
    expect(screen.getByTestId("settings-location")).toHaveTextContent(`/vault/platform-docs/doc/overview%2Fvault-skill.md${entry === "Edit guide" ? "?view=edit" : ""}`);
    const state = JSON.parse(screen.getByTestId("settings-preview-state").textContent!);
    expect(state).toMatchObject({ documentPreview: true, backgroundLocation: { pathname: "/vault/platform-docs/settings", hash: "#skill" } });
    expect(state.returnFocusId).toEqual(expect.any(String));
    expect(updateDocument).not.toHaveBeenCalled();
  });

  it("distinguishes a failed Guide GET from a confirmed 404 and supports retry", async () => {
    const user = userEvent.setup();
    vi.mocked(getDocument).mockRejectedValueOnce(Object.assign(new Error("Service unavailable"), { status: 503 }));
    renderSettings("#skill");
    expect(await screen.findByText("Couldn't load the vault guide")).toBeVisible();
    expect(screen.queryByText(/restored automatically/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Original guide")).toBeVisible();
    expect(screen.queryByText("Couldn't load the vault guide")).not.toBeInTheDocument();
  });

  it("shows the missing Guide notice only after a 404", async () => {
    vi.mocked(getDocument).mockRejectedValueOnce(Object.assign(new Error("Not found"), { status: 404 }));
    renderSettings("#skill");
    expect(await screen.findByText(/restored automatically/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
  });
  it.each(["Leave settings", "Another vault"])("blocks %s while a save is pending and retains a failed draft", async (destination) => {
    const user = userEvent.setup();
    let rejectSave!: (error: Error) => void;
    updateVaultMock.mockReturnValueOnce(new Promise((_resolve, reject) => { rejectSave = reject; }));
    renderSettings("#access");
    await selectTab(user, "Access permissions");
    await user.click(await screen.findByRole("radio", { name: "Everyone can view" }));
    await user.click(scope("Access permissions").getByRole("button", { name: "Save changes" }));
    await selectTab(user, "Vault details");
    await user.click(screen.getByLabelText("Description"));
    await user.click(screen.getByRole("button", { name: destination }));
    expect(screen.getByLabelText("Description")).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Vault directory" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Discard changes and leave" })).not.toBeInTheDocument();
    const savingEvent = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(savingEvent);
    expect(savingEvent.defaultPrevented).toBe(true);
    await act(async () => { rejectSave(new Error("Access save failed")); });
    expect(screen.getByRole("alert")).toHaveTextContent("Access save failed");
    await selectTab(user, "Access permissions");
    expect(screen.getByRole("radio", { name: "Everyone can view" })).toBeChecked();
    await user.click(screen.getByRole("button", { name: destination }));
    await user.click(screen.getByRole("button", { name: "Keep editing" }));
    await selectTab(user, "Access permissions");
    expect(screen.getByRole("radio", { name: "Everyone can view" })).toBeChecked();
    expect(screen.getByRole("alert")).toHaveTextContent("Access save failed");
  });

  it.each(["general", "access"])("protects the %s draft when the Vault breadcrumb opens Overview", async scope => {
    const user = userEvent.setup();
    renderSettings(`#${scope}`);
    if (scope === "general") {
      fireEvent.change(await screen.findByLabelText("Description"), { target: { value: "Unsaved description" } });
    } else {
      await selectTab(user, "Access permissions");
      await user.click(await screen.findByRole("radio", { name: "Everyone can view" }));
    }
    await user.click(screen.getByRole("link", { name: "Back to platform-docs" }));
    const confirmation = screen.getByRole("dialog", { name: "Discard unsaved settings changes?" });
    expect(screen.queryByRole("menu", { name: "Vault pages" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Vault overview" })).not.toBeInTheDocument();
    await user.click(within(confirmation).getByRole("button", { name: "Keep editing" }));
    await waitFor(() => expect(screen.getByRole("link", { name: "Back to platform-docs" })).toHaveFocus());
    if (scope === "general") expect(screen.getByLabelText("Description")).toHaveValue("Unsaved description");
    else expect(screen.getByRole("radio", { name: "Everyone can view" })).toBeChecked();
    await user.click(screen.getByRole("link", { name: "Back to platform-docs" }));
    await user.click(screen.getByRole("button", { name: "Discard changes and leave" }));
    expect(await screen.findByRole("heading", { name: "Vault overview" })).toBeVisible();
    expect(updateVault).not.toHaveBeenCalled();
  });

  it("guards the parent Vault link and preserves both settings drafts on cancel", async () => {
    const user = userEvent.setup();
    renderSettings();
    fireEvent.change(await screen.findByLabelText("Description"), { target: { value: "Unsaved description" } });
    await selectTab(user, "Access permissions");
    await user.click(screen.getByRole("radio", { name: "Everyone can view" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: "Back to platform-docs" }));
    await user.click(screen.getByRole("button", { name: "Keep editing" }));
    await selectTab(user, "Access permissions");
    expect(screen.getByRole("radio", { name: "Everyone can view" })).toBeChecked();
    expect(screen.getByLabelText("Description")).toHaveValue("Unsaved description");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: "Back to platform-docs" }));
    await user.click(screen.getByRole("button", { name: "Discard changes and leave" }));
    expect(await screen.findByRole("heading", { name: "Vault overview" })).toBeVisible();
  });

  it("uses one leave confirmation for multiple Settings drafts and keeps them on cancel", async () => {
    const user = userEvent.setup();
    renderSettings();
    fireEvent.change(await screen.findByLabelText("Description"), { target: { value: "Unsaved description" } });
    await selectTab(user, "Access permissions");
    await user.click(screen.getByRole("radio", { name: "Everyone can view" }));
    await user.click(screen.getByRole("link", { name: "Back to platform-docs" }));
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Keep editing" }));
    expect(screen.getByLabelText("Description")).toHaveValue("Unsaved description");
    await selectTab(user, "Access permissions");
    expect(screen.getByRole("radio", { name: "Everyone can view" })).toBeChecked();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: "Back to platform-docs" }));
    await user.click(screen.getByRole("button", { name: "Discard changes and leave" }));
    expect(await screen.findByRole("heading", { name: "Vault overview" })).toBeVisible();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("allows dirty same-page hash history and confirms actual Back and Forward departures", async () => {
    const user = userEvent.setup();
    window.history.replaceState({ idx: 4, key: "entry-4" }, "", "/vault/platform-docs/settings#general");
    const uninstall = installDocumentHistoryGuard();
    const go = vi.spyOn(window.history, "go").mockImplementation(() => {});
    const pop = (index: number, path: string) => act(() => {
      window.history.replaceState({ idx: index, key: `entry-${index}` }, "", path);
      window.dispatchEvent(new PopStateEvent("popstate", { state: window.history.state }));
    });
    try {
      renderBrowserSettings();
      fireEvent.change(await screen.findByLabelText("Description"), { target: { value: "Unsaved description" } });
      pop(3, "/vault/platform-docs/settings#access");
      expect(screen.getByRole("region", { name: "Access permissions" })).toBeVisible();
      expect(go).not.toHaveBeenCalled();
      await selectTab(user, "Access permissions");
      await user.click(screen.getByRole("radio", { name: "Everyone can view" }));
      pop(2, "/vault/platform-docs/members");
      await waitFor(() => expect(go).toHaveBeenLastCalledWith(1));
      pop(3, "/vault/platform-docs/settings#access");
      await user.click(screen.getByRole("button", { name: "Keep editing" }));
      expect(screen.getByLabelText("Description")).toHaveValue("Unsaved description");
        await selectTab(user, "Access permissions");
    expect(screen.getByRole("radio", { name: "Everyone can view" })).toBeChecked();
      expect(screen.queryByRole("heading", { name: "Vault members" })).not.toBeInTheDocument();
      pop(4, "/vault/platform-docs/members");
      await waitFor(() => expect(go).toHaveBeenLastCalledWith(-1));
      pop(3, "/vault/platform-docs/settings#access");
      const corrections = go.mock.calls.length;
      pop(4, "/vault/platform-docs/members");
      await waitFor(() => expect(go).toHaveBeenCalledTimes(corrections + 1));
      pop(3, "/vault/platform-docs/settings#access");
      await user.click(screen.getByRole("button", { name: "Discard changes and leave" }));
      expect(go).toHaveBeenLastCalledWith(1);
      pop(4, "/vault/platform-docs/members");
      expect(await screen.findByRole("heading", { name: "Vault members" })).toBeVisible();
    } finally {
      cleanup();
      uninstall();
      go.mockRestore();
      window.history.replaceState(null, "", "/");
    }
  });

  it("blocks breadcrumb departure during a save and allows it after success", async () => {
    const user = userEvent.setup();
    let finishSave!: (value: { ok: boolean }) => void;
    updateVaultMock.mockReturnValueOnce(new Promise(resolve => { finishSave = resolve; }));
    renderSettings();
    fireEvent.change(await screen.findByLabelText("Description"), { target: { value: "Saved description" } });
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await selectTab(user, "Access permissions");
    await user.click(screen.getByRole("radio", { name: "Invited members only" }));
    await user.click(screen.getByRole("link", { name: "Back to platform-docs" }));
    expect(screen.queryByRole("heading", { name: "Vault overview" })).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Access permissions" })).toBeVisible();
    await act(async () => { finishSave({ ok: true }); });
    await user.click(screen.getByRole("link", { name: "Back to platform-docs" }));
    expect(await screen.findByRole("heading", { name: "Vault overview" })).toBeVisible();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("blocks the Guide edit entry while a Settings save is pending and retains failed Settings work", async () => {
    const user = userEvent.setup();
    let failSave!: (error: Error) => void;
    updateVaultMock.mockReturnValueOnce(new Promise((_resolve, reject) => { failSave = reject; }));
    renderSettings();
    fireEvent.change(await screen.findByLabelText("Description"), { target: { value: "Unsaved description" } });
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await selectTab(user, "Vault guide");
    expect(screen.getByRole("button", { name: "Edit guide" })).toBeDisabled();
    expect(screen.getByRole("link", { name: "Open guide" })).toBeVisible();
    await act(async () => { failSave(new Error("Settings save failed")); });
    expect(screen.getByRole("alert")).toHaveTextContent("Settings save failed");
    expect(screen.getByLabelText("Description")).toHaveValue("Unsaved description");
    await selectTab(user, "Vault guide");
    expect(screen.getByRole("button", { name: "Edit guide" })).toBeEnabled();
    expect(updateDocument).not.toHaveBeenCalled();
  });

  it("shows one accessible panel with matching tab controls and keeps the other drafts mounted", async () => {
    const user = userEvent.setup();
    renderSettings();
    await screen.findByRole("textbox", { name: "Description" });
    expect(within(screen.getByTestId("vault-settings-header")).getByRole("heading", { level: 1, name: "Vault settings" })).toBeVisible();
    const tabs = screen.getByRole("tablist", { name: "Vault settings sections" });
    expect(within(tabs).getAllByRole("tab")).toHaveLength(4);
    for (const [tabName, regionName] of [["Vault details", "Vault details"], ["Access permissions", "Access permissions"], ["Vault guide", "Vault guide"], ["Lifecycle actions", "Danger zone"]]) {
      await selectTab(user, tabName);
      expect(screen.getAllByRole("tabpanel")).toHaveLength(1);
      const panel = screen.getByRole("tabpanel", { name: tabName });
      const tab = within(tabs).getByRole("tab", { name: tabName });
      expect(tab).toHaveAttribute("aria-selected", "true");
      expect(tab).toHaveAttribute("aria-controls", panel.id);
      expect(within(panel).getByRole("region", { name: regionName })).toBeVisible();
      for (const label of ["Collections", "Documents", "Tables", "Files", "Graph links"]) expect(within(panel).queryByText(label)).not.toBeInTheDocument();
    }
    for (const id of ["general", "access", "skill"]) {
      const panel = document.getElementById(id)!.closest('[role="tabpanel"]');
      expect(panel).toHaveAttribute("hidden");
      expect(panel).toHaveAttribute("inert");
    }
    await selectTab(user, "Access permissions");
    expect(screen.getByRole("link", { name: "Manage members" })).toHaveAttribute("href", "/vault/platform-docs/members");
    await selectTab(user, "Vault details");
    expect(screen.getByRole("button", { name: "Copy vault address" })).toBeVisible();
    expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save changes" })).not.toBeInTheDocument();
  });

  it("keeps local hash navigation in the same settings sheet and restores it with Back", async () => {
    const user = userEvent.setup();
    renderSettings();
    await screen.findByRole("textbox", { name: "Description" });
    fireEvent.change(screen.getByLabelText("Description"), { target: { value: "Preserve across hash changes" } });
    act(() => {
      const access = screen.getByRole("tab", { name: "Access permissions" });
      fireEvent.mouseDown(access, { button: 0, ctrlKey: false });
      fireEvent.click(access);
    });
    expect(screen.getByRole("region", { name: "Access permissions" })).toBeVisible();
    expect(screen.getByLabelText("Description")).toHaveValue("Preserve across hash changes");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Browser back" }));
    expect(screen.getByRole("region", { name: "Vault details" })).toBeVisible();
    expect(screen.getByLabelText("Description")).toHaveValue("Preserve across hash changes");
  });

  it("selects the legacy deep-linked tab and hides the other settings", async () => {
    renderSettings("#access");
    await screen.findByLabelText("Description");
    for (const [id, label] of [["general", "Vault details"], ["access", "Access permissions"], ["skill", "Vault guide"], ["danger", "Danger zone"]]) {
      const section = document.getElementById(id)!;
      expect(section).toBeInTheDocument();
      if (id === "access") expect(screen.getByRole("region", { name: label })).toBeVisible();
      else expect(section).not.toBeVisible();
    }
    expect(screen.queryByRole("complementary", { name: "Vault settings context" })).not.toBeInTheDocument();
  });

  it.each(["reader", "admin"])(
    "keeps %s settings read-only in every category",
    async (role) => {
      getVaultInfoMock.mockResolvedValue({
        ...VAULT_INFO,
        role,
        role_source: "member",
      });
      renderSettings("#danger");

      const message = await screen.findByText(
        "Read only — only the vault owner can change settings.",
      );
      expect(message).toBeVisible();
      expect(screen.getByRole("tab", { name: "Vault details" })).toHaveAttribute("aria-selected", "true");
      expect(screen.queryByRole("tab", { name: "Lifecycle actions" })).not.toBeInTheDocument();
      expect(
        screen.getByRole("textbox", { name: "Description" }),
      ).toHaveAttribute("readonly");
      expect(
        screen.queryByRole("button", { name: "Save changes" }),
      ).not.toBeInTheDocument();
      const user = userEvent.setup();
      await selectTab(user, "Access permissions");
      expect(screen.getByRole("radio", { name: "Invited members only" })).toBeDisabled();
      await selectTab(user, "Vault guide");
      expect(
        screen.queryByRole("button", { name: "Edit guide" }),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "Delete vault" }),
      ).not.toBeInTheDocument();
    },
  );

  it("saves General and Access as independent settings", async () => {
    const user = userEvent.setup();
    renderSettings();

    const description = await screen.findByLabelText("Description");
    await user.clear(description);
    await user.type(description, "A concise operating knowledge base.");
    await user.click(
      scope("Vault details").getByRole("button", { name: "Save changes" }),
    );

    await waitFor(() =>
      expect(updateVaultMock).toHaveBeenCalledWith("platform-docs", {
        description: "A concise operating knowledge base.",
      }),
    );

    await selectTab(user, "Access permissions");
    await user.click(screen.getByRole("radio", { name: "Everyone can view" }));
    await user.click(scope("Access permissions").getByRole("button", { name: "Save changes" }));

    await waitFor(() =>
      expect(updateVaultMock).toHaveBeenCalledWith("platform-docs", {
        public_access: "reader",
      }),
    );
  });

  it("retains description and access drafts while browsing Guide and Lifecycle", async () => {
    const user = userEvent.setup();
    renderSettings();
    fireEvent.change(await screen.findByLabelText("Description"), { target: { value: "Unsaved description" } });
    await selectTab(user, "Access permissions");
    await user.click(screen.getByRole("radio", { name: "Everyone can view" }));
    await selectTab(user, "Vault guide");
    expect(await screen.findByText("Original guide")).toBeVisible();
    expect(screen.queryByRole("textbox", { name: "Vault guide body" })).not.toBeInTheDocument();
    await selectTab(user, "Lifecycle actions");
    expect(screen.getByRole("button", { name: "Archive" })).toBeVisible();
    await selectTab(user, "Vault details");
    expect(screen.getByLabelText("Description")).toHaveValue("Unsaved description");
    await selectTab(user, "Access permissions");
    expect(screen.getByRole("radio", { name: "Everyone can view" })).toBeChecked();
    expect(updateVault).not.toHaveBeenCalled();
    expect(updateDocument).not.toHaveBeenCalled();
  });

  it("keeps Current on the applied policy until the selected draft is saved", async () => {
    const user = userEvent.setup();
    let finish!: () => void;
    updateVaultMock.mockReturnValueOnce(new Promise<void>(resolve => { finish = resolve; }));
    renderSettings("#access");
    const members = (await screen.findByRole("radio", { name: "Invited members only" })).closest("label")!;
    const reader = screen.getByRole("radio", { name: "Everyone can view" }).closest("label")!;
    expect(within(members).getByText("Current")).toBeVisible();
    await user.click(within(reader).getByRole("radio"));
    expect(within(reader).getByText("Not saved")).toBeVisible();
    expect(within(members).getByText("Current")).toBeVisible();
    await user.click(scope("Access permissions").getByRole("button", { name: "Save changes" }));
    expect(within(members).getByText("Current")).toBeVisible();
    await act(async () => { finish(); });
    await waitFor(() => expect(within(reader).getByText("Current")).toBeVisible());
    expect(within(members).queryByText("Current")).not.toBeInTheDocument();
    expect(within(reader).queryByText("Not saved")).not.toBeInTheDocument();
    expect(updateVaultMock).toHaveBeenCalledWith("platform-docs", { public_access: "reader" });
  });

  it("explains the signed-in audience and deletion risk before allowing everyone to edit", async () => {
    const user = userEvent.setup();
    renderSettings("#access");
    await selectTab(user, "Access permissions");
    await user.click(
      await screen.findByRole("radio", { name: "Everyone can edit" }),
    );
    expect(screen.getByText("Sign-in required")).toBeVisible();
    expect(screen.getByRole("radiogroup", { name: "Who can access" })).toHaveAccessibleDescription(
      "Signed-in AKB users can add, edit and delete content.",
    );
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(updateVault).not.toHaveBeenCalled();
    const confirmation = screen.getByRole("dialog", { name: 'Let everyone edit "platform-docs"?' });
    expect(confirmation).toHaveTextContent("Anyone signed in to this AKB will be able to add, edit, and delete content, even if they aren’t a member.");
    expect(confirmation).not.toHaveTextContent("world-writable");
    await user.click(
      screen.getByRole("button", { name: "Allow editing for everyone" }),
    );
    await waitFor(() =>
      expect(updateVault).toHaveBeenCalledWith("platform-docs", {
        public_access: "writer",
      }),
    );
  });

  it("retains a failed edit and its scoped error until discard", async () => {
    const user = userEvent.setup();
    updateVaultMock.mockRejectedValueOnce(
      new Error("Permission changed on server"),
    );
    renderSettings();
    await user.type(
      await screen.findByRole("textbox", { name: "Description" }),
      " changed",
    );
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Permission changed on server",
    );
    await selectTab(user, "Access permissions");
    await user.click(screen.getByRole("radio", { name: "Invited members only" }));
    expect(scope("Access permissions").queryByRole("alert")).not.toBeInTheDocument();
    await selectTab(user, "Vault details");
    expect(screen.getByRole("textbox", { name: "Description" })).toHaveValue(
      "Product and platform knowledge. changed",
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Permission changed on server",
    );
    await user.click(screen.getByRole("button", { name: "Discard" }));
    expect(screen.getByRole("textbox", { name: "Description" })).toHaveValue(
      VAULT_INFO.description,
    );
  });

  it("focuses the scoped Access error when its save fails after focus moved elsewhere", async () => {
    const user = userEvent.setup();
    let rejectSave!: (error: Error) => void;
    updateVaultMock.mockReturnValueOnce(
      new Promise((_resolve, reject) => {
        rejectSave = reject;
      }),
    );
    renderSettings("#access");
    await selectTab(user, "Access permissions");
    await user.click(
      await screen.findByRole("radio", { name: "Everyone can view" }),
    );
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await selectTab(user, "Vault details");
    await user.click(screen.getByLabelText("Description"));
    await act(async () => {
      rejectSave(new Error("Access save failed"));
    });
    const error = scope("Access permissions").getByRole("alert");
    expect(error).toHaveTextContent("Access save failed");
    expect(error.parentElement).toHaveFocus();
  });

  it.each(["general", "access"])(
    "announces a %s save without stealing focus from another setting",
    async (scope) => {
      const user = userEvent.setup();
      let finishSave!: (value: { ok: boolean }) => void;
      updateVaultMock.mockReturnValueOnce(
        new Promise((resolve) => {
          finishSave = resolve;
        }),
      );
      renderSettings(`#${scope}`);
      if (scope === "general") {
        await user.type(
          await screen.findByRole("textbox", { name: "Description" }),
          " changed",
        );
      } else {
        await selectTab(user, "Access permissions");
        await user.click(
          await screen.findByRole("radio", { name: "Everyone can view" }),
        );
      }
      await user.click(screen.getByRole("button", { name: "Save changes" }));
      await selectTab(user, "Vault guide");
      await user.click(screen.getByRole("button", { name: "Guide options" }));
      await user.click(screen.getByRole("menuitem", { name: "Agent view" }));
      const otherSetting = screen.getByRole("button", { name: "Close agent view" });
      otherSetting.focus();
      expect(otherSetting).toHaveFocus();
      await act(async () => {
        finishSave({ ok: true });
        await new Promise((resolve) => requestAnimationFrame(resolve));
      });
      expect(screen.getByText(scope === "general" ? "Vault details saved." : "Access permissions saved.")).toBeInTheDocument();
      expect(otherSetting).toHaveFocus();
    },
  );

  it.each(["general", "access"])("serializes %s saves against other Settings writes and the Guide edit entry", async saving => {
    const user = userEvent.setup();
    let finish!: (result: { ok: boolean }) => void;
    updateVaultMock.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    renderSettings();
    fireEvent.change(await screen.findByLabelText("Description"), { target: { value: "Unsaved description" } });
    await selectTab(user, "Access permissions");
    await user.click(screen.getByRole("radio", { name: "Everyone can view" }));
    await selectTab(user, saving === "general" ? "Vault details" : "Access permissions");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    const savingTab = saving === "general" ? "Vault details" : "Access permissions";
    for (const [tabName, buttonName] of [["Vault details", "Save changes"], ["Access permissions", "Save changes"], ["Vault guide", "Edit guide"], ["Lifecycle actions", "Archive"]]) {
      await selectTab(user, tabName);
      expect(screen.getByRole("button", { name: tabName === savingTab ? "Saving…" : buttonName })).toBeDisabled();
    }
    expect(screen.getByRole("button", { name: "Delete vault" })).toBeDisabled();
    await act(async () => { finish({ ok: true }); });
    await selectTab(user, "Vault guide");
    expect(screen.getByRole("button", { name: "Edit guide" })).toBeEnabled();
    expect(updateVault).toHaveBeenCalledTimes(1);
    expect(updateDocument).not.toHaveBeenCalled();
    await selectTab(user, saving === "general" ? "Access permissions" : "Vault details");
    expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled();
  });

  it("keeps the legacy danger link at the lifecycle actions without extra operations information", async () => {
    renderSettings("#danger");
    expect(await screen.findByRole("button", { name: "Archive" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Delete vault" })).toBeVisible();
    expect(screen.getByRole("region", { name: "Danger zone" })).toHaveAttribute("id", "danger");
    expect(screen.queryByRole("heading", { name: "Operations" })).not.toBeInTheDocument();
  });

  it("keeps archive and restore behind their existing confirmations", async () => {
    const user = userEvent.setup();
    renderSettings("#danger");
    await user.click(await screen.findByRole("button", { name: "Archive" }));
    expect(archiveVault).not.toHaveBeenCalled();
    getVaultInfoMock.mockResolvedValue({ ...VAULT_INFO, is_archived: true });
    await user.click(screen.getByRole("button", { name: "Archive vault" }));
    await waitFor(() =>
      expect(archiveVault).toHaveBeenCalledWith("platform-docs"),
    );
    await user.click(await screen.findByRole("button", { name: "Unarchive" }));
    expect(unarchiveVault).not.toHaveBeenCalled();
    getVaultInfoMock.mockResolvedValue(VAULT_INFO);
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: "Unarchive",
      }),
    );
    await waitFor(() =>
      expect(unarchiveVault).toHaveBeenCalledWith("platform-docs"),
    );
    expect(
      await screen.findByRole("button", { name: "Archive" }),
    ).toBeVisible();
  });

  it("requires the exact name before deleting and returns to the directory", async () => {
    const user = userEvent.setup();
    renderSettings("#danger");
    await user.click(
      await screen.findByRole("button", { name: "Delete vault" }),
    );
    const dialog = screen.getByRole("dialog");
    expect(
      within(dialog).getByRole("button", { name: "Delete forever" }),
    ).toBeDisabled();
    await user.type(within(dialog).getByRole("textbox"), "platform-docs");
    await user.click(
      within(dialog).getByRole("button", { name: "Delete forever" }),
    );
    await waitFor(() =>
      expect(deleteVaultPermanent).toHaveBeenCalledWith("platform-docs"),
    );
    expect(
      await screen.findByRole("heading", { name: "Vault directory" }),
    ).toBeInTheDocument();
  });

  it("opens an unknown hash safely without adding ownership metadata or inventing inventory totals", async () => {
    getVaultInfoMock.mockResolvedValue({
      name: "platform-docs",
      role: "owner",
      document_count: null,
      table_count: null,
      file_count: null,
    });
    renderSettings("#unknown");
    expect(await screen.findByRole("textbox", { name: "Description" })).toBeVisible();
    expect(screen.queryByLabelText("Vault ownership and dates")).not.toBeInTheDocument();
    expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
    const user = userEvent.setup();
    await selectTab(user, "Lifecycle actions");
    expect(screen.getByText(/All vault data will be removed/)).not.toHaveTextContent(/0 documents|0 tables|0 files/);
  });

  it("retains the unload guard while another setting has focus and clears it after discard", async () => {
    const user = userEvent.setup();
    renderSettings();
    await user.type(await screen.findByRole("textbox", { name: "Description" }), " draft");
    await selectTab(user, "Vault guide");
    await user.click(screen.getByRole("button", { name: "Guide options" }));
    await user.click(screen.getByRole("menuitem", { name: "Agent view" }));
    const dirtyEvent = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(dirtyEvent);
    expect(dirtyEvent.defaultPrevented).toBe(true);
    await selectTab(user, "Vault details");
    await user.click(screen.getByRole("button", { name: "Discard" }));
    const cleanEvent = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(cleanEvent);
    expect(cleanEvent.defaultPrevented).toBe(false);
  });

  it("recovers a failed initial load without losing the requested anchor", async () => {
    const user = userEvent.setup();
    getVaultInfoMock.mockRejectedValueOnce(new Error("Vault unavailable"));
    renderSettings("#access");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Vault unavailable",
    );
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("radio", { name: "Invited members only" })).toBeVisible();
    expect(screen.getByRole("region", { name: "Access permissions" })).toHaveAttribute("id", "access");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("opens the guide deep link without enabling archived guide edits", async () => {
    const user = userEvent.setup();
    getVaultInfoMock.mockResolvedValue({ ...VAULT_INFO, is_archived: true });
    renderSettings("#skill");
    await selectTab(user, "Vault guide");
    await user.click(await screen.findByRole("button", { name: "Guide options" }));
    expect(screen.getByRole("menuitem", { name: "Agent view" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit guide" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("menuitem", { name: "Reset to template" }),
    ).not.toBeInTheDocument();
  });
});
