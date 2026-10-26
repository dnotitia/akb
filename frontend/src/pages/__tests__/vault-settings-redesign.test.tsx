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
import { BrowserRouter, MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
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

function renderSettings(hash = "") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/vault/platform-docs/settings${hash}`]}>
        <ResourceNavigationProvider>
        <BackButton />
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

  it("preserves a same-account Guide draft and OCC pin across proof but drops another account's buffer", async () => {
    const user = userEvent.setup();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = (id: string, checking = false, revision = 0) => <QueryClientProvider client={client}><MemoryRouter initialEntries={["/vault/platform-docs/settings#skill"]}><CurrentUserProvider user={{ user_id: id, username: id, email: `${id}@x.test`, display_name: null, is_admin: false, auth_method: "local", key_class: null }} checking={checking} revision={revision}><Routes><Route path="/vault/:name/settings" element={<VaultSettingsPage />} /></Routes></CurrentUserProvider></MemoryRouter></QueryClientProvider>;
    const { rerender } = render(view("a"));
    await user.click(await screen.findByRole("button", { name: "Edit guide" }));
    fireEvent.change(screen.getByLabelText("Vault guide body"), { target: { value: "Private draft A" } });
    rerender(view("a", true));
    expect(screen.queryByLabelText("Vault guide body")).not.toBeInTheDocument();
    vi.mocked(getDocument).mockResolvedValue({ content: "New server body", current_commit: "new-server-commit" });
    rerender(view("a", false, 1));
    await waitFor(() => expect(screen.getByLabelText("Vault guide body")).toBeVisible());
    expect(screen.getByLabelText("Vault guide body")).toHaveValue("Private draft A");
    vi.mocked(updateDocument).mockResolvedValue({ current_commit: "saved" });
    await user.click(screen.getByRole("button", { name: "Save guide" }));
    expect(updateDocument).toHaveBeenCalledWith("platform-docs", "overview/vault-skill.md", { content: "Private draft A", expected_commit: "original-commit" });
    rerender(view("b", false, 2));
    await screen.findByText("New server body");
    expect(screen.queryByDisplayValue("Private draft A")).not.toBeInTheDocument();
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
    await user.click(await screen.findByRole("radio", { name: "Everyone can view" }));
    await user.click(scope("Access permissions").getByRole("button", { name: "Save changes" }));
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
    expect(screen.getByRole("radio", { name: "Everyone can view" })).toBeChecked();
    await user.click(screen.getByRole("button", { name: destination }));
    await user.click(screen.getByRole("button", { name: "Keep editing" }));
    expect(screen.getByRole("radio", { name: "Everyone can view" })).toBeChecked();
    expect(screen.getByRole("alert")).toHaveTextContent("Access save failed");
  });

  it.each(["general", "access"])("protects the %s draft when the Vault breadcrumb opens Overview", async scope => {
    const user = userEvent.setup();
    renderSettings(`#${scope}`);
    if (scope === "general") {
      fireEvent.change(await screen.findByLabelText("Description"), { target: { value: "Unsaved description" } });
    } else {
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
    await user.click(screen.getByRole("radio", { name: "Everyone can view" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: "Back to platform-docs" }));
    await user.click(screen.getByRole("button", { name: "Keep editing" }));
    expect(screen.getByRole("radio", { name: "Everyone can view" })).toBeChecked();
    expect(screen.getByLabelText("Description")).toHaveValue("Unsaved description");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: "Back to platform-docs" }));
    await user.click(screen.getByRole("button", { name: "Discard changes and leave" }));
    expect(await screen.findByRole("heading", { name: "Vault overview" })).toBeVisible();
  });

  it("uses one leave confirmation for settings and Guide drafts and keeps every draft on cancel", async () => {
    const user = userEvent.setup();
    renderSettings();
    fireEvent.change(await screen.findByLabelText("Description"), { target: { value: "Unsaved description" } });
    await user.click(screen.getByRole("radio", { name: "Everyone can view" }));
    await user.click(await screen.findByRole("button", { name: "Edit guide" }));
    fireEvent.change(screen.getByLabelText("Vault guide body"), { target: { value: "Unsaved guide" } });
    await user.click(screen.getByRole("link", { name: "Back to platform-docs" }));
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Keep editing" }));
    expect(screen.getByLabelText("Vault guide body")).toHaveValue("Unsaved guide");
    expect(screen.getByLabelText("Description")).toHaveValue("Unsaved description");
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
      await user.click(await screen.findByRole("button", { name: "Edit guide" }));
      fireEvent.change(screen.getByLabelText("Vault guide body"), { target: { value: "Unsaved guide" } });
      pop(3, "/vault/platform-docs/settings#access");
      expect(screen.getByRole("region", { name: "Access permissions" })).toBeVisible();
      expect(go).not.toHaveBeenCalled();
      await user.click(screen.getByRole("radio", { name: "Everyone can view" }));
      pop(2, "/vault/platform-docs/members");
      await waitFor(() => expect(go).toHaveBeenLastCalledWith(1));
      pop(3, "/vault/platform-docs/settings#access");
      await user.click(screen.getByRole("button", { name: "Keep editing" }));
      expect(screen.getByLabelText("Description")).toHaveValue("Unsaved description");
      expect(screen.getByLabelText("Vault guide body")).toHaveValue("Unsaved guide");
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
    await user.click(screen.getByRole("radio", { name: "Invited members only" }));
    await user.click(screen.getByRole("link", { name: "Back to platform-docs" }));
    expect(screen.queryByRole("heading", { name: "Vault overview" })).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Access permissions" })).toBeVisible();
    await act(async () => { finishSave({ ok: true }); });
    await user.click(screen.getByRole("link", { name: "Back to platform-docs" }));
    expect(await screen.findByRole("heading", { name: "Vault overview" })).toBeVisible();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("blocks departure while a Guide save is pending and keeps failed Guide and settings drafts", async () => {
    const user = userEvent.setup();
    let failSave!: (error: Error) => void;
    vi.mocked(updateDocument).mockReturnValueOnce(new Promise((_resolve, reject) => { failSave = reject; }));
    renderSettings();
    fireEvent.change(await screen.findByLabelText("Description"), { target: { value: "Unsaved description" } });
    await user.click(await screen.findByRole("button", { name: "Edit guide" }));
    fireEvent.change(screen.getByLabelText("Vault guide body"), { target: { value: "Unsaved guide" } });
    await user.click(screen.getByRole("button", { name: "Save guide" }));
    await user.click(screen.getByRole("link", { name: "Back to platform-docs" }));
    expect(screen.queryByRole("heading", { name: "Vault overview" })).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await act(async () => { failSave(new Error("Guide save failed")); });
    await user.click(screen.getByRole("link", { name: "Back to platform-docs" }));
    await user.click(screen.getByRole("button", { name: "Keep editing" }));
    expect(screen.getByLabelText("Description")).toHaveValue("Unsaved description");
    expect(screen.getByLabelText("Vault guide body")).toHaveValue("Unsaved guide");
    expect(screen.getByRole("alert")).toHaveTextContent("Guide save failed");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("dialog", { name: "Discard unsaved guide changes?" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Keep editing" }));
    expect(screen.getByLabelText("Vault guide body")).toHaveValue("Unsaved guide");
  });

  it("shows all tasks together with a copyable address but no redundant details, inventory, or tabs", async () => {
    renderSettings();
    await screen.findByRole("textbox", { name: "Description" });
    const general = screen.getByRole("region", { name: "Vault details" });
    expect(within(screen.getByTestId("vault-settings-header")).getByRole("heading", { level: 2, name: "Settings" })).toBeVisible();
    expect(within(general).getByText("akb://platform-docs")).toBeVisible();
    expect(within(general).getByRole("button", { name: "Copy vault address" })).toBeVisible();
    expect(screen.queryByLabelText("Vault ownership and dates")).not.toBeInTheDocument();
    expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
    expect(within(general).getByRole("heading", { name: "Vault details" })).toBeVisible();
    expect(screen.queryByRole("link", { name: "Manage members" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Public links" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tablist")).not.toBeInTheDocument();
    expect(screen.queryByRole("tabpanel")).not.toBeInTheDocument();
    for (const category of ["Vault details", "Access permissions", "Vault guide", "Danger zone"]) {
      const section = screen.getByRole("region", { name: category });
      expect(section).toBeVisible();
      for (const label of ["Collections", "Documents", "Tables", "Files", "Graph links"]) {
        expect(within(section).queryByText(label)).not.toBeInTheDocument();
      }
    }
    expect(screen.queryByRole("button", { name: "Save changes" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Discard" })).not.toBeInTheDocument();
  });

  it("keeps local hash navigation in the same settings sheet and restores it with Back", async () => {
    const user = userEvent.setup();
    renderSettings();
    await screen.findByRole("textbox", { name: "Description" });
    fireEvent.change(screen.getByLabelText("Description"), { target: { value: "Preserve across hash changes" } });
    await user.click(screen.getByRole("button", { name: "Jump to access" }));
    expect(screen.getByRole("region", { name: "Access permissions" })).toBeVisible();
    expect(screen.getByLabelText("Description")).toHaveValue("Preserve across hash changes");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Browser back" }));
    expect(screen.getByRole("region", { name: "Vault details" })).toBeVisible();
    expect(screen.getByLabelText("Description")).toHaveValue("Preserve across hash changes");
  });

  it("retains legacy deep-link anchors without hiding the other settings", async () => {
    renderSettings("#access");
    await screen.findByLabelText("Description");
    for (const [id, label] of [["general", "Vault details"], ["access", "Access permissions"], ["skill", "Vault guide"], ["danger", "Danger zone"]]) {
      expect(screen.getByRole("region", { name: label })).toHaveAttribute("id", id);
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
      renderSettings();

      const message = await screen.findByText(
        "Read only — only the vault owner can change settings.",
      );
      expect(message).toBeVisible();
      expect(
        screen.getByRole("textbox", { name: "Description" }),
      ).toHaveAttribute("readonly");
      expect(
        screen.queryByRole("button", { name: "Save changes" }),
      ).not.toBeInTheDocument();
      expect(screen.getByRole("radio", { name: "Invited members only" })).toBeDisabled();
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

    await user.click(screen.getByRole("radio", { name: "Everyone can view" }));
    await user.click(scope("Access permissions").getByRole("button", { name: "Save changes" }));

    await waitFor(() =>
      expect(updateVaultMock).toHaveBeenCalledWith("platform-docs", {
        public_access: "reader",
      }),
    );
  });

  it("retains description, access, and guide drafts while working across the sheet", async () => {
    const user = userEvent.setup();
    renderSettings();
    const description = await screen.findByRole("textbox", {
      name: "Description",
    });
    await user.clear(description);
    await user.type(description, "Unsaved description");
    await user.click(screen.getByRole("radio", { name: "Everyone can view" }));
    await user.click(await screen.findByRole("button", { name: "Edit guide" }));
    await user.type(
      screen.getByRole("textbox", { name: "Vault guide body" }),
      " with edits",
    );
    await user.click(description);
    expect(screen.getByRole("textbox", { name: "Description" })).toHaveValue(
      "Unsaved description",
    );
    expect(screen.getByRole("radio", { name: "Everyone can view" })).toBeChecked();
    expect(
      screen.getByRole("textbox", { name: "Vault guide body" }),
    ).toHaveValue("Original guide with edits");
    await user.click(screen.getByRole("button", { name: "Save guide" }));
    await waitFor(() =>
      expect(updateDocument).toHaveBeenCalledWith(
        "platform-docs",
        "overview/vault-skill.md",
        {
          content: "Original guide with edits",
          expected_commit: "original-commit",
        },
      ),
    );
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
    await user.click(screen.getByRole("radio", { name: "Invited members only" }));
    expect(scope("Access permissions").queryByRole("alert")).not.toBeInTheDocument();
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
    await user.click(
      await screen.findByRole("radio", { name: "Everyone can view" }),
    );
    await user.click(screen.getByRole("button", { name: "Save changes" }));
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
        await user.click(
          await screen.findByRole("radio", { name: "Everyone can view" }),
        );
      }
      await user.click(screen.getByRole("button", { name: "Save changes" }));
      await user.click(screen.getByRole("button", { name: "Guide options" }));
      await user.click(screen.getByRole("menuitem", { name: "Agent view" }));
      const otherSetting = screen.getByRole("button", { name: "Close agent view" });
      otherSetting.focus();
      expect(otherSetting).toHaveFocus();
      await act(async () => {
        finishSave({ ok: true });
        await new Promise((resolve) => requestAnimationFrame(resolve));
      });
      expect(screen.getByText("Saved")).toBeInTheDocument();
      expect(otherSetting).toHaveFocus();
    },
  );

  it.each(["general", "access", "guide"])("serializes %s saves against other settings, Guide, and lifecycle writes", async saving => {
    const user = userEvent.setup();
    let finish!: (result: { ok: boolean; current_commit: string }) => void;
    const pending = new Promise<{ ok: boolean; current_commit: string }>(resolve => { finish = resolve; });
    if (saving === "guide") vi.mocked(updateDocument).mockReturnValueOnce(pending);
    else updateVaultMock.mockReturnValueOnce(pending);
    renderSettings();
    fireEvent.change(await screen.findByLabelText("Description"), { target: { value: "Unsaved description" } });
    await user.click(screen.getByRole("radio", { name: "Everyone can view" }));
    await user.click(await screen.findByRole("button", { name: "Edit guide" }));
    fireEvent.change(screen.getByLabelText("Vault guide body"), { target: { value: "Unsaved guide" } });
    const generalSave = scope("Vault details").getByRole("button", { name: "Save changes" });
    const accessSave = scope("Access permissions").getByRole("button", { name: "Save changes" });
    const guideSave = screen.getByRole("button", { name: "Save guide" });
    await user.click(saving === "general" ? generalSave : saving === "access" ? accessSave : guideSave);
    expect(generalSave).toBeDisabled();
    expect(accessSave).toBeDisabled();
    expect(guideSave).toBeDisabled();
    expect(screen.getByRole("button", { name: "Archive" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Delete vault" })).toBeDisabled();
    expect(updateVault).toHaveBeenCalledTimes(saving === "guide" ? 0 : 1);
    expect(updateDocument).toHaveBeenCalledTimes(saving === "guide" ? 1 : 0);
    await act(async () => { finish({ ok: true, current_commit: "saved-commit" }); });
    if (saving !== "general") {
      expect(screen.getByLabelText("Description")).toHaveValue("Unsaved description");
      expect(scope("Vault details").getByRole("button", { name: "Save changes" })).toBeEnabled();
    }
    if (saving !== "access") {
      expect(screen.getByRole("radio", { name: "Everyone can view" })).toBeChecked();
      expect(scope("Access permissions").getByRole("button", { name: "Save changes" })).toBeEnabled();
    }
    if (saving !== "guide") {
      expect(screen.getByLabelText("Vault guide body")).toHaveValue("Unsaved guide");
      expect(screen.getByRole("button", { name: "Save guide" })).toBeEnabled();
    }
    expect(screen.getByRole("button", { name: "Archive" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Delete vault" })).toBeEnabled();
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
    expect(screen.getByText(/All vault data will be removed/)).not.toHaveTextContent(/0 documents|0 tables|0 files/);
  });

  it("retains the unload guard while another setting has focus and clears it after discard", async () => {
    const user = userEvent.setup();
    renderSettings();
    await user.type(await screen.findByRole("textbox", { name: "Description" }), " draft");
    await user.click(screen.getByRole("button", { name: "Guide options" }));
    await user.click(screen.getByRole("menuitem", { name: "Agent view" }));
    const dirtyEvent = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(dirtyEvent);
    expect(dirtyEvent.defaultPrevented).toBe(true);
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
    await user.click(await screen.findByRole("button", { name: "Guide options" }));
    expect(screen.getByRole("menuitem", { name: "Agent view" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit guide" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("menuitem", { name: "Reset to template" }),
    ).not.toBeInTheDocument();
  });
});
