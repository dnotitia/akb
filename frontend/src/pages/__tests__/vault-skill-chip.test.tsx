import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { MemoryRouter, Outlet, Route, Routes, useLocation } from "react-router-dom";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import VaultPage from "@/pages/vault";
import { CurrentUserProvider } from "@/contexts/current-user-context";
import { DocumentCreateDialogProvider } from "@/contexts/document-create-dialog-context";
import { VaultRefreshProvider } from "@/contexts/vault-refresh-context";

vi.mock("@/lib/api", async (original) => ({
  ...await original<typeof import("@/lib/api")>(),
  authenticatedFetch: vi.fn(),
  browseVault: vi.fn(),
  createCollection: vi.fn(),
  createVaultTable: vi.fn(),
  getDocument: vi.fn(),
  getRecent: vi.fn(),
  getSkillTemplate: vi.fn(),
  getVaultActivity: vi.fn(),
  getVaultInfo: vi.fn(),
  importKnowledgeBundle: vi.fn(),
  uploadVaultFile: vi.fn(),
}));

import {
  authenticatedFetch,
  browseVault,
  createCollection,
  createVaultTable,
  getDocument,
  getRecent,
  getSkillTemplate,
  getVaultActivity,
  getVaultInfo,
  importKnowledgeBundle,
  uploadVaultFile,
} from "@/lib/api";

const authenticatedFetchMock = authenticatedFetch as unknown as ReturnType<
  typeof vi.fn
>;
const getDocumentMock = getDocument as unknown as ReturnType<typeof vi.fn>;
const getRecentMock = getRecent as unknown as ReturnType<typeof vi.fn>;
const getSkillTemplateMock = getSkillTemplate as unknown as ReturnType<
  typeof vi.fn
>;
const getVaultActivityMock = getVaultActivity as unknown as ReturnType<
  typeof vi.fn
>;
const getVaultInfoMock = getVaultInfo as unknown as ReturnType<typeof vi.fn>;
const importKnowledgeBundleMock =
  importKnowledgeBundle as unknown as ReturnType<typeof vi.fn>;
const uploadVaultFileMock = uploadVaultFile as unknown as ReturnType<
  typeof vi.fn
>;
const createVaultTableMock = createVaultTable as unknown as ReturnType<
  typeof vi.fn
>;
const createCollectionMock = createCollection as unknown as ReturnType<typeof vi.fn>;
const openCreateDocumentMock = vi.fn();
const refetchTreeMock = vi.fn();

// The endpoint serves the raw seed file (trailing newline intact); the stored
// body comes back frontmatter-parsed and whitespace-stripped. An untouched
// guide therefore differs from the template by exactly that newline.
const TEMPLATE = "# {vault} Guide\n\n(Describe what this vault is for.)\n";
const SEEDED_BODY = "# my-v Guide\n\n(Describe what this vault is for.)";

function PreviewProbe() {
  const location = useLocation();
  return <output data-testid="preview-state">{JSON.stringify(location.state)}</output>;
}

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="vault-location">{location.pathname + location.search}</output>;
}

async function chooseCreate(label: string) {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "New content" }));
  await user.click(screen.getByRole("menuitem", { name: label }));
}

function renderVault({ initialEntry = "/vault/my-v", vaultNavigationOpen = false } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <VaultRefreshProvider refetchVaults={() => {}} refetchTree={refetchTreeMock}>
      <DocumentCreateDialogProvider openCreateDocument={openCreateDocumentMock}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <LocationProbe />
        <Routes>
          <Route element={<Outlet context={{ vaultNavigationOpen }} />}>
            <Route path="/vault/:name" element={<VaultPage />} />
            <Route path="/vault/:name/doc/:id" element={<PreviewProbe />} />
            <Route path="/vault/:name/table/:id" element={<h1>Created table</h1>} />
            <Route path="/vault/:name/file/:id" element={<h1>Uploaded file</h1>} />
          </Route>
        </Routes>
      </MemoryRouter>
      </DocumentCreateDialogProvider>
      </VaultRefreshProvider>
    </QueryClientProvider>,
  );
}

const findChip = () => screen.findByRole("link", { name: "Open vault guide" });

it("scopes Overview local info and late Guide reads to the verified account/revision", async () => {
  let resolve!: (value: { content: string }) => void;
  getDocumentMock.mockReturnValueOnce(new Promise(r => { resolve = r; })).mockResolvedValue({ content: "New account guide" });
  getVaultInfoMock.mockResolvedValueOnce({
    name: "my-v", role: "owner", description: "Private account A description",
    public_access: "none", owner_display_name: "Account A owner", member_count: 7,
  }).mockResolvedValue({
    name: "my-v", role: "reader", description: "Account B description",
    public_access: "reader", owner_display_name: "Account B owner", member_count: 2,
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = (id: string, checking = false, revision = 0) => <QueryClientProvider client={client}><MemoryRouter initialEntries={["/vault/my-v"]}><CurrentUserProvider user={{ user_id: id, username: id, email: `${id}@x.test`, display_name: null, is_admin: false, auth_method: "local", key_class: null }} checking={checking} revision={revision}><Routes><Route path="/vault/:name" element={<VaultPage />} /></Routes></CurrentUserProvider></MemoryRouter></QueryClientProvider>;
  const { rerender } = render(view("a"));
  await screen.findByText("Private account A description");
  expect(screen.getByRole("button", { name: "Vault access: Private" })).toBeVisible();
  expect(screen.getByText("Account A owner")).toBeVisible();
  expect(screen.getByRole("link", { name: "Members" })).toHaveAccessibleDescription("7 members");
  rerender(view("b", true));
  expect(screen.queryByText("Private account A description")).not.toBeInTheDocument();
  expect(screen.queryByText("Account A owner")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Vault access: Private" })).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Members" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "New content" })).not.toBeInTheDocument();
  await act(async () => resolve({ content: "Private account A guide" }));
  rerender(view("b", false, 1));
  await screen.findByText("Account B description");
  await waitFor(() => expect(getDocumentMock).toHaveBeenCalledTimes(2));
  expect(screen.getByRole("button", { name: "Vault access: Public read" })).toBeVisible();
  expect(screen.getByText("Account B owner")).toBeVisible();
  expect(screen.getByRole("link", { name: "Members" })).toHaveAccessibleDescription("2 members");
  expect(screen.queryByText("Account A owner")).not.toBeInTheDocument();
  expect(screen.queryByText("Private account A guide")).not.toBeInTheDocument();
});

it.each([
  { kind: "table", menu: "New table", dialog: "Create a table", field: /Table name/, value: "retained_table" },
  { kind: "collection", menu: "New collection", dialog: "New collection", field: "Path", value: "guides/retained" },
])("preserves an Overview $kind draft across same-account proof while resetting its private read scope", async ({ menu, dialog: dialogName, field, value }) => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = (checking: boolean, revision: number) => <QueryClientProvider client={client}><MemoryRouter initialEntries={["/vault/my-v"]}><CurrentUserProvider user={{ user_id: "a", username: "a", email: "a@x.test", display_name: null, is_admin: false, auth_method: "local", key_class: null }} checking={checking} revision={revision}><Routes><Route path="/vault/:name" element={<VaultPage />} /></Routes></CurrentUserProvider></MemoryRouter></QueryClientProvider>;
  const { rerender } = render(view(false, 0));
  await chooseCreate(menu);
  const dialog = screen.getByRole("dialog", { name: dialogName });
  fireEvent.change(within(dialog).getByLabelText(field), { target: { value } });
  rerender(view(true, 0));
  expect(screen.getByRole("status", { name: "Verifying vault access…" })).toBeVisible();
  expect(screen.queryByRole("dialog", { name: dialogName })).not.toBeInTheDocument();
  rerender(view(false, 1));
  await waitFor(() => expect(getVaultInfoMock).toHaveBeenCalledTimes(2));
  const restored = await screen.findByRole("dialog", { name: dialogName });
  expect(within(restored).getByLabelText(field)).toHaveValue(value);
});

it("ignores an old account's collection completion after the verified identity changes", async () => {
  let finish!: (result: { created: boolean; collection: { path: string } }) => void;
  createCollectionMock.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
  getVaultInfoMock.mockResolvedValueOnce({ name: "my-v", role: "owner", description: "Account A", document_count: 4 })
    .mockResolvedValue({ name: "my-v", role: "reader", description: "Account B", document_count: 4 });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = (id: string) => <QueryClientProvider client={client}><VaultRefreshProvider refetchVaults={() => {}} refetchTree={refetchTreeMock}><MemoryRouter initialEntries={["/vault/my-v"]}><CurrentUserProvider user={{ user_id: id, username: id, email: `${id}@x.test`, display_name: null, is_admin: false, auth_method: "local", key_class: null }}><LocationProbe /><Routes><Route path="/vault/:name" element={<VaultPage />} /></Routes></CurrentUserProvider></MemoryRouter></VaultRefreshProvider></QueryClientProvider>;
  const { rerender } = render(view("a"));
  await chooseCreate("New collection");
  const dialog = screen.getByRole("dialog", { name: "New collection" });
  fireEvent.change(within(dialog).getByLabelText("Path"), { target: { value: "private-work" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "Create" }));
  await waitFor(() => expect(createCollectionMock).toHaveBeenCalledTimes(1));
  rerender(view("b"));
  await screen.findByText("Account B");
  await act(async () => finish({ created: true, collection: { path: "private-work" } }));
  expect(refetchTreeMock).not.toHaveBeenCalled();
  expect(getVaultInfoMock).toHaveBeenCalledTimes(2);
  expect(screen.getByTestId("vault-location")).toHaveTextContent(/^\/vault\/my-v$/);
  expect(screen.queryByRole("dialog", { name: "New collection" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "New content" })).not.toBeInTheDocument();
});

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(browseVault).mockImplementation(async (_vault, _collection, _depth, options) => ({ vault: "my-v", path: "", complete: true, next_cursor: null,
    items: options?.content_type === "collections" ? ["guides/team", "overview", "overview/nested", "overview/private"].map(path => ({ type: "collection", name: path.split("/").at(-1)!, path })) : [],
  }));
  authenticatedFetchMock.mockResolvedValue({ ok: false });
  getRecentMock.mockResolvedValue({ changes: [] });
  getVaultActivityMock.mockResolvedValue({ activity: [] });
  getSkillTemplateMock.mockResolvedValue(TEMPLATE);
  getDocumentMock.mockResolvedValue({ content: SEEDED_BODY });
  getVaultInfoMock.mockResolvedValue({
    name: "my-v",
    role: "owner",
    document_count: 4,
    table_count: 0,
    file_count: 0,
  });
  importKnowledgeBundleMock.mockResolvedValue({
    format: "okf",
    vault: "my-v",
    created: 2,
    skipped: 0,
    failed: 0,
    uris: [],
    skipped_paths: [],
    reserved: [],
    errors: [],
  });
  uploadVaultFileMock.mockResolvedValue({
    kind: "file",
    uri: "akb://my-v/file/4f9d5609-c6c7-4b62-a8be-79308c18cb8d",
    vault: "my-v",
    name: "runbook.pdf",
    mime_type: "application/pdf",
    size_bytes: 7,
  });
  createVaultTableMock.mockResolvedValue({
    kind: "table",
    uri: "akb://my-v/table/incidents",
    vault: "my-v",
    name: "incidents",
  });
  createCollectionMock.mockImplementation(async (_vault: string, path: string) => ({
    ok: true, created: true, collection: { path, name: path.split("/").at(-1), doc_count: 0 },
  }));
});

afterEach(cleanup);

describe("vault page guide metadata", () => {
  it("shows Guide load failure and retries without suggesting a new Guide", async () => {
    getDocumentMock.mockRejectedValueOnce(Object.assign(new Error("Service unavailable"), { status: 503 }));
    renderVault();
    const retry = await screen.findByRole("button", { name: "Retry vault guide" });
    const guide = screen.getByRole("region", { name: "Vault guide" });
    expect(retry).toHaveTextContent("Guide unavailable · Retry");
    expect(within(guide).getAllByRole("button")).toHaveLength(1);
    expect(within(guide).queryByRole("link", { name: "Set up vault guide" })).not.toBeInTheDocument();
    expect(within(guide).queryByText(/Set up a vault guide/)).not.toBeInTheDocument();
    fireEvent.click(retry);
    expect(await findChip()).toBeVisible();
    expect(screen.queryByRole("button", { name: "Retry vault guide" })).not.toBeInTheDocument();
  });

  it.each(["owner", "admin", "writer"])("offers Guide setup to a %s after a confirmed 404", async role => {
    getVaultInfoMock.mockResolvedValue({ name: "my-v", role, document_count: 4 });
    getDocumentMock.mockRejectedValueOnce(Object.assign(new Error("Not found"), { status: 404 }));
    renderVault();
    expect(await screen.findByRole("link", { name: "Set up vault guide" })).toHaveAttribute("href", "/vault/my-v/settings#skill");
    expect(screen.queryByRole("button", { name: "Retry vault guide" })).not.toBeInTheDocument();
  });

  it.each([
    { role: "reader" },
    { role: "owner", is_archived: true },
  ])("keeps a missing Guide passive when setup is unavailable: %j", async info => {
    getVaultInfoMock.mockResolvedValue({ name: "my-v", document_count: 4, ...info });
    getDocumentMock.mockRejectedValueOnce(Object.assign(new Error("Not found"), { status: 404 }));
    renderVault();
    const guide = await screen.findByRole("region", { name: "Vault guide" });
    expect(await within(guide).findByText("Guide not configured")).toBeVisible();
    expect(within(guide).queryByRole("link")).not.toBeInTheDocument();
    expect(within(guide).queryByRole("button")).not.toBeInTheDocument();
  });

  it("does not offer Guide setup while its presence is still loading", async () => {
    getDocumentMock.mockReturnValueOnce(new Promise(() => {}));
    renderVault();
    expect(await screen.findByText("Checking vault guide")).toBeVisible();
    expect(within(screen.getByRole("region", { name: "Vault guide" })).getByRole("status")).toHaveTextContent("Checking vault guide");
    expect(screen.queryByRole("link", { name: "Set up vault guide" })).not.toBeInTheDocument();
    expect(screen.queryByText(/Set up a vault guide/)).not.toBeInTheDocument();
  });
  it("reads as 'template' when the body still matches the substituted seed", async () => {
    renderVault();
    const link = await findChip();
    await waitFor(() => expect(link).toHaveAccessibleDescription("Starter template"));
    expect(link.getAttribute("href")).toBe("/vault/my-v/doc/overview%2Fvault-skill.md");
    expect(getSkillTemplateMock).toHaveBeenCalled();
    expect(link).toHaveTextContent("Vault guide");
    expect(link).not.toHaveTextContent("Starter template");
  });

  it("reads as 'customized' once the body diverges", async () => {
    getDocumentMock.mockResolvedValue({
      content: `${SEEDED_BODY}\n\nWe keep incident write-ups here.`,
    });
    renderVault();
    const link = await findChip();
    await waitFor(() => expect(link).toHaveAccessibleDescription("Guide customized"));
    expect(link.getAttribute("href")).toBe("/vault/my-v/doc/overview%2Fvault-skill.md");
  });

  it("keeps an untouched historical seed classified as template while retaining overview context", async () => {
    getDocumentMock.mockResolvedValue({
      content: "# my-v Guide\n\nAn older default placeholder.",
      created_at: "2026-01-02T03:04:05Z",
      updated_at: "2026-01-02T03:04:05Z",
    });
    renderVault();
    const link = await findChip();
    await waitFor(() => expect(link).toHaveAccessibleDescription("Starter template"));
    expect(screen.getByRole("region", { name: "Vault summary" })).toContainElement(link);
    expect(screen.queryByText("An older default placeholder.")).not.toBeInTheDocument();
  });

  it("keeps recent activity focused and progressively discloses commit history", async () => {
    getRecentMock.mockResolvedValue({
      changes: [
        {
          doc_id: "d-runbook",
          vault: "my-v",
          path: "ops/runbook.md",
          title: "Runbook",
          type: "note",
          commit: "abcdef0123456789",
          changed_at: "2026-08-25T08:00:00Z",
        },
      ],
    });
    getVaultActivityMock.mockResolvedValue({
      activity: [
        {
          hash: "abcdef0123456789",
          author_name: "Vault Owner",
          subject: "Update incident runbook",
          date: "2026-08-25T08:00:00Z",
          files: [{ path: "ops/runbook.md", change: "modified" }],
        },
      ],
    });

    renderVault();

    expect(
      await screen.findByRole("region", { name: "Recent activity" }),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("region", { name: "Commit history" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Full activity" })).toBeNull();
    expect(screen.queryByText("Update incident runbook")).toBeNull();
    expect(
      screen.getByRole("link", { name: "Full commit log" }),
    ).toHaveAttribute("href", "/vault/my-v/activity");
    const disclosure = await screen.findByRole("button", {
      name: "Show commits",
    });
    expect(disclosure).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(disclosure);
    expect(
      screen.getByRole("button", { name: "Hide commits" }),
    ).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Update incident runbook")).toBeInTheDocument();
    expect(screen.getByText("Vault Owner")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Contents" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Tables" })).toBeNull();
    await waitFor(() =>
      expect(getVaultActivityMock).toHaveBeenCalledWith("my-v", { limit: 10 }),
    );
  });

  it("keeps the Guide ready while its template classification is in flight", async () => {
    getSkillTemplateMock.mockReturnValue(new Promise(() => {}));
    renderVault();
    const link = await findChip();
    await waitFor(() => expect(getSkillTemplateMock).toHaveBeenCalled());
    expect(link).toHaveAccessibleDescription("Guide ready");
  });

  it("withholds the chip on a mirror vault, which carries no guide", async () => {
    getVaultInfoMock.mockResolvedValue({
      name: "mirror-v",
      role: "owner",
      is_external_git: true,
      document_count: 4,
      table_count: 0,
      file_count: 0,
    });
    getDocumentMock.mockRejectedValue(new Error("not found"));
    renderVault();
    // The stat tiles only render once /info resolved — i.e. once the chip
    // would have had everything it needs.
    await screen.findByText("Documents");
    expect(screen.queryByRole("link", { name: "Open vault guide" })).toBeNull();
    expect(
      screen.queryByRole("link", { name: "Set up vault guide" }),
    ).toBeNull();
    expect(getSkillTemplateMock).not.toHaveBeenCalled();
    expect(getDocumentMock).not.toHaveBeenCalled();
    expect(screen.queryByRole("region", { name: "Vault guide" })).not.toBeInTheDocument();
    expect(screen.getByText("external git")).toBeVisible();
  });

  it("keeps the overview hierarchy for an empty vault and points the guide step at settings", async () => {
    getVaultInfoMock.mockResolvedValue({
      name: "my-v",
      role: "owner",
      owner_display_name: "Vault Owner",
      member_count: 1,
      public_access: "none",
      created_at: "2026-01-02T03:04:05Z",
      description: "A quiet place for the team's first knowledge.",
      document_count: 1, // the seeded guide alone → still "empty"
      table_count: 0,
      file_count: 0,
    });
    renderVault();
    const step = await screen.findByRole("link", {
      name: /Describe this Vault/i,
    });
    expect(step.getAttribute("href")).toBe("/vault/my-v/settings#skill");
    const setup = screen.getByRole("region", { name: "Set up this Vault" });
    expect(setup).toContainElement(step);
    expect(setup).toContainElement(
      screen.getByRole("button", { name: /Import knowledge bundle/i }),
    );
    expect(setup).toContainElement(
      screen.getByRole("link", { name: /Connect an agent/i }),
    );
    expect(
      screen.getByRole("button", { name: /Write a document/ }),
    ).toBeInTheDocument();
    expect(
      screen.getAllByRole("button", { name: /Upload a file/ }).length,
    ).toBeGreaterThan(0);
    expect(
      screen.getAllByRole("button", { name: /Create a table|New table/ })
        .length,
    ).toBeGreaterThan(0);
    expect(screen.getByText("Optional next steps")).toBeInTheDocument();
    expect(screen.queryByText("Build the foundation")).toBeNull();
    expect(
      screen.getByRole("heading", { level: 1, name: "my-v" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: "Contents" }),
    ).toBeInTheDocument();
    const workspace = screen.getByRole("region", {
      name: "my-v Vault overview",
    });
    expect(workspace).toContainElement(
      screen.getByRole("region", { name: "Contents" }),
    );
    const summary = screen.getByRole("region", { name: "Vault summary" });
    expect(summary).toHaveTextContent("Vault Owner");
    expect(within(summary).getByRole("button", { name: "Vault access: Private" })).toBeVisible();
    expect(within(summary).getByRole("link", { name: "Members" })).toHaveAccessibleDescription("1 members");
    expect(screen.queryByRole("complementary", { name: "Vault overview details" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "About this vault" }),
    ).toBeNull();
    expect(screen.queryByText("Edges")).toBeNull();
    expect(screen.queryByText(/Owned by/)).toBeNull();
    expect(
      screen.getByText("A quiet place for the team's first knowledge."),
    ).toBeInTheDocument();
  });

  it("imports an OKF zip from the empty-state action and reports the result", async () => {
    getVaultInfoMock.mockResolvedValue({
      name: "my-v",
      role: "owner",
      document_count: 1,
      table_count: 0,
      file_count: 0,
    });
    renderVault();
    const input = await screen.findByLabelText("Choose knowledge bundle");
    const bundle = new File(["bundle"], "knowledge.okf.zip", {
      type: "application/zip",
    });
    fireEvent.change(input, { target: { files: [bundle] } });
    await waitFor(() =>
      expect(importKnowledgeBundleMock).toHaveBeenCalledWith("my-v", bundle),
    );
    expect(await screen.findByText("Knowledge imported")).toBeInTheDocument();
    expect(screen.getByText(/2 documents created/i)).toBeInTheDocument();
  });

  it("keeps the icon-only creation menu available while inline Vault navigation is open", async () => {
    renderVault({ vaultNavigationOpen: true });
    const trigger = await screen.findByRole("button", { name: "New content" });
    expect(trigger.textContent).toBe("");
    expect(trigger.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
    const user = userEvent.setup();
    trigger.focus();
    await user.keyboard("{Enter}");
    const menu = await screen.findByRole("menu");
    expect(within(menu).getAllByRole("menuitem").map(item => item.textContent)).toEqual([
      "New document", "New collection", "New table", "Upload file",
    ]);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it.each([
    { role: "reader" },
    { role: "owner", is_archived: true },
    { role: "owner", is_external_git: true },
  ])("withholds creation when content cannot be changed: %j", async restriction => {
    getVaultInfoMock.mockResolvedValue({ name: "my-v", document_count: 4, ...restriction });
    renderVault({ vaultNavigationOpen: true });
    await screen.findByRole("region", { name: "Contents" });
    expect(screen.queryByRole("button", { name: "New content" })).not.toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Create content" })).not.toBeInTheDocument();
  });

  it.each(["", "guides/team", "overview", "overview/private"])("opens the document composer with a safe collection default from %s", async collection => {
    renderVault({ initialEntry: `/vault/my-v?collection=${encodeURIComponent(collection)}` });
    await chooseCreate("New document");
    expect(openCreateDocumentMock).toHaveBeenCalledExactlyOnceWith({
      collection: collection.startsWith("overview") ? "" : collection,
    });
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("withholds header creation for a deleted collection context", async () => {
    vi.mocked(browseVault).mockResolvedValue({ vault: "my-v", path: "deleted", complete: true, next_cursor: null,
      context: { type: "collection", uri: "akb://team/coll/deleted", path: "deleted", name: "deleted" }, items: [] });
    renderVault({ initialEntry: "/vault/my-v?collection=deleted" });
    await screen.findByText(/This collection is unavailable/);
    expect(screen.queryByRole("button", { name: "New content" })).toBeNull();
  });

  it("creates a child collection, refreshes inventory, and keeps the current directory", async () => {
    renderVault({ initialEntry: "/vault/my-v?collection=guides%2Fteam" });
    await chooseCreate("New collection");
    const dialog = screen.getByRole("dialog", { name: "New collection" });
    expect(within(dialog).getByLabelText("Path")).toHaveValue("guides/team/");
    fireEvent.change(within(dialog).getByLabelText("Path"), { target: { value: "guides/team/decisions" } });
    fireEvent.change(within(dialog).getByLabelText("Summary (optional)"), { target: { value: "Team decisions" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create" }));
    await waitFor(() => expect(createCollectionMock).toHaveBeenCalledExactlyOnceWith("my-v", "guides/team/decisions", "Team decisions"));
    await waitFor(() => expect(refetchTreeMock).toHaveBeenCalledTimes(1));
    expect(getVaultInfoMock).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId("vault-location")).toHaveTextContent("/vault/my-v?collection=guides%2Fteam");
    expect(screen.queryByRole("dialog", { name: "New collection" })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "New content" })).toHaveFocus());
  });

  it.each(["", "overview/nested"])("starts a new root collection when the requested parent is %s", async collection => {
    renderVault({ initialEntry: `/vault/my-v?collection=${encodeURIComponent(collection)}` });
    await chooseCreate("New collection");
    const dialog = screen.getByRole("dialog", { name: "New collection" });
    expect(within(dialog).getByLabelText("Path")).toHaveValue("");
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "New content" })).toHaveFocus());
  });

  it.each(["", "guides/team", "overview/nested"])("uploads a file with the safe collection default from %s", async collection => {
    renderVault({ initialEntry: `/vault/my-v?collection=${encodeURIComponent(collection)}` });
    await chooseCreate("Upload file");
    const dialog = screen.getByRole("dialog", { name: "Upload a file" });
    const defaultCollection = collection.startsWith("overview") ? "" : collection;
    expect(within(dialog).getByLabelText(/^Collection/)).toHaveValue(defaultCollection);
    const upload = new File(["runbook"], "runbook.pdf", { type: "application/pdf" });
    fireEvent.change(screen.getByLabelText(/File \*/), { target: { files: [upload] } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Upload file" }));
    await waitFor(() => expect(uploadVaultFileMock).toHaveBeenCalledWith(
      "my-v", upload, expect.objectContaining({ collection: defaultCollection, description: "", onStageChange: expect.any(Function) }),
    ));
    expect(await screen.findByRole("heading", { name: "Uploaded file" })).toBeVisible();
    expect(refetchTreeMock).toHaveBeenCalledTimes(1);
  });

  it.each(["", "guides/team", "overview/nested"])("creates a table with the safe collection default from %s", async collection => {
    renderVault({ initialEntry: `/vault/my-v?collection=${encodeURIComponent(collection)}` });
    await chooseCreate("New table");
    const dialog = screen.getByRole("dialog", { name: "Create a table" });
    const defaultCollection = collection.startsWith("overview") ? "" : collection;
    expect(within(dialog).getByLabelText(/^Collection/)).toHaveValue(defaultCollection);
    fireEvent.change(within(dialog).getByLabelText(/Table name/), { target: { value: "incidents" } });
    fireEvent.change(within(dialog).getByLabelText("Name"), { target: { value: "status" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create table" }));
    await waitFor(() => expect(createVaultTableMock).toHaveBeenCalledWith("my-v", {
      name: "incidents", description: "", collection: defaultCollection,
      columns: [{ name: "status", type: "text", required: false, unique: false }],
    }));
    expect(await screen.findByRole("heading", { name: "Created table" })).toBeVisible();
    expect(refetchTreeMock).toHaveBeenCalledTimes(1);
  });

});

describe("vault overview content hierarchy", () => {
  it("keeps Guide metadata compact without exposing its body or frontmatter", async () => {
    getDocumentMock.mockResolvedValue({
      content: "---\ntitle: Private guide metadata\n---\n# Internal procedures\n\nPrivate first paragraph.\n\nPrivate later paragraph.",
    });
    renderVault();
    const link = await findChip();
    await waitFor(() => expect(link).toHaveAccessibleDescription("Guide customized"));
    const summary = screen.getByRole("region", { name: "Vault summary" });
    const guide = within(summary).getByRole("region", { name: "Vault guide" });
    expect(guide).toContainElement(link);
    expect(within(guide).getAllByRole("link")).toHaveLength(1);
    expect(within(guide).queryByRole("heading")).not.toBeInTheDocument();
    expect(screen.queryByText(/Private guide metadata|Internal procedures|Private first paragraph|Private later paragraph/)).not.toBeInTheDocument();
  });

  it("keeps an empty existing Guide available through its native document destination", async () => {
    getDocumentMock.mockResolvedValue({ content: "" });
    renderVault();
    const link = await findChip();
    expect(link).toHaveAttribute("href", "/vault/my-v/doc/overview%2Fvault-skill.md");
    expect(screen.queryByRole("link", { name: "Set up vault guide" })).not.toBeInTheDocument();
    expect(screen.queryByText(/Add purpose, scope, and agent instructions/)).not.toBeInTheDocument();
  });

  it.each([
    ["none", "Private", "Only people with access can open this vault."],
    ["reader", "Public read", "Any signed-in person can read this vault."],
    ["writer", "Public write", "Signed-in users can read, and write when vault policies allow."],
    [undefined, "Access unavailable", "Visibility information is unavailable."],
  ])("explains %s access separately from the member destination", async (publicAccess, label, explanation) => {
    getVaultInfoMock.mockResolvedValue({
      name: "my-v", role: "reader", document_count: 4,
      public_access: publicAccess, owner_display_name: "Vault Owner", member_count: 5,
    });
    renderVault();
    const access = await screen.findByRole("region", { name: "Access and ownership" });
    const trigger = within(access).getByRole("button", { name: `Vault access: ${label}` });
    expect(access).toHaveTextContent("OwnerVault Owner");
    expect(access).not.toHaveTextContent(explanation!);
    expect(within(access).queryByRole("link")).not.toBeInTheDocument();
    const members = within(screen.getByRole("navigation", { name: "Vault pages" })).getByRole("link", { name: "Members" });
    expect(members).toHaveAttribute("href", "/vault/my-v/members");
    expect(members).toHaveAccessibleDescription("5 members");
    const user = userEvent.setup();
    await user.click(trigger);
    const policy = screen.getByRole("dialog", { name: "Vault access" });
    expect(policy).toHaveTextContent(explanation!);
    expect(policy).toHaveTextContent("Public links are managed separately and may allow access without signing in.");
    await user.click(within(policy).getByRole("button", { name: "Close vault access" }));
    expect(screen.queryByRole("dialog", { name: "Vault access" })).not.toBeInTheDocument();
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it.each(["is_archived", "is_external_git"])("does not promise public writes when %s makes content read-only", async (restriction) => {
    getVaultInfoMock.mockResolvedValue({
      name: "my-v", role: "owner", document_count: 4,
      public_access: "writer", [restriction]: true,
    });
    renderVault();
    const trigger = await screen.findByRole("button", { name: "Vault access: Public write" });
    await userEvent.click(trigger);
    const policy = screen.getByRole("dialog", { name: "Vault access" });
    expect(policy).toHaveTextContent("Any signed-in person can read this vault. Content is read-only.");
    expect(policy).not.toHaveTextContent("write when vault policies allow");
    expect(screen.queryByRole("button", { name: "New content" })).not.toBeInTheDocument();
  });

  it("keeps identity, passive totals, metadata, and membership together above the content", async () => {
    getVaultInfoMock.mockResolvedValue({
      name: "my-v", role: "writer", document_count: 36, collection_count: 8,
      table_count: 2, file_count: 7, member_count: 5, public_access: "reader",
      owner_display_name: "Vault Owner", tables: [{ name: "service_catalog", row_count: 18 }],
    });
    renderVault();
    const contents = await screen.findByRole("region", { name: "Contents" });
    const summary = screen.getByRole("region", { name: "Vault summary" });
    const details = screen.getByRole("complementary", { name: "Vault overview details" });
    expect(summary).toContainElement(contents);
    const pages = within(summary).getByRole("navigation", { name: "Vault pages" });
    expect(within(pages).getAllByRole("link").map(link => link.getAttribute("href"))).toEqual(["/vault/my-v/graph", "/vault/my-v/publications", "/vault/my-v/members", "/vault/my-v/settings"]);
    expect(within(pages).queryByRole("link", { name: "Overview" })).not.toBeInTheDocument();
    expect(details).not.toContainElement(contents);
    expect(within(summary).getByText("writer", { exact: true })).toBeInTheDocument();
    expect(within(summary).getByRole("button", { name: "Vault access: Public read" })).toBeVisible();
    expect(within(summary).queryByText("public:reader")).not.toBeInTheDocument();
    expect(within(summary).getByRole("button", { name: "Copy akb://my-v" })).toBeEnabled();
    expect(within(details).queryByRole("button", { name: "Copy akb://my-v" })).toBeNull();
    expect(within(contents).queryByRole("button")).toBeNull();
    for (const [label, value] of [["Documents", "36"], ["Collections", "8"], ["Tables", "2"], ["Files", "7"]]) {
      expect(within(contents).getByText(label).closest("div")).toHaveTextContent(value);
    }
    expect(within(contents).queryByText("Members")).toBeNull();
    const access = screen.getByRole("region", { name: "Access and ownership" });
    expect(summary).toContainElement(access);
    expect(within(access).queryByText("Members")).not.toBeInTheDocument();
    const members = within(pages).getByRole("link", { name: "Members" });
    expect(members).toHaveAttribute("href", "/vault/my-v/members");
    expect(members).toHaveTextContent("Members5");
    expect(members).toHaveAccessibleDescription("5 members");
    expect(access).not.toHaveTextContent("Your role");
    expect(within(details).queryByRole("region", { name: "Tables" })).toBeNull();
    expect(screen.queryByRole("link", { name: /service_catalog/ })).toBeNull();
    const actions = within(summary).getByRole("group", { name: "Create content" });
    expect(within(actions).getByRole("button", { name: "New content" })).toBeEnabled();
    expect(within(actions).getAllByRole("button")).toHaveLength(1);
    expect(screen.queryByRole("menuitem")).not.toBeInTheDocument();
  });

  it("distinguishes missing counts from zero and never treats a legacy info response as empty", async () => {
    getVaultInfoMock.mockResolvedValue({ name: "my-v", role: "reader", file_count: 0 });
    renderVault();
    const contents = await screen.findByRole("region", { name: "Contents" });
    expect(within(contents).getByText("Documents").closest("div")).toHaveTextContent("—");
    expect(within(contents).getByText("Files").closest("div")).toHaveTextContent("0");
    expect(screen.getByRole("region", { name: "Access and ownership" })).not.toHaveTextContent("Private");
    expect(screen.queryByRole("region", { name: "Set up this Vault" })).toBeNull();
    expect(screen.getByRole("region", { name: "Recent activity" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New table" })).toBeNull();
  });

  it("keeps document destinations while showing collection context instead of a repeated filename and commit", async () => {
    getRecentMock.mockResolvedValue({ changes: [
      { doc_id: "d-runbook", title: "Runbook", path: "team/ops/runbook.md", commit: "abcdef012345", changed_at: "2026-09-21T00:00:00Z" },
      { doc_id: "d-root", title: "Welcome", path: "welcome.md" },
    ] });
    renderVault();
    const recent = await screen.findByRole("region", { name: "Recent activity" });
    const runbook = await within(recent).findByRole("link", { name: /Runbook/ });
    expect(runbook).toHaveAttribute("href", "/vault/my-v/doc/team%2Fops%2Frunbook.md");
    expect(runbook).toHaveTextContent("team / ops");
    expect(runbook).not.toHaveTextContent("runbook.md");
    expect(recent).not.toHaveTextContent("abcdef0");
    expect(within(recent).getByRole("link", { name: /Welcome/ })).toHaveTextContent("Vault root");
  });

  it("limits the aside to recent activity and commits after the main collection workspace", async () => {
    getVaultInfoMock.mockResolvedValue({
      name: "my-v", role: "reader", table_count: 4, file_count: 2,
      tables: [{ name: "catalog" }, { name: "inventory", row_count: 100 }],
    });
    renderVault();
    const details = await screen.findByRole("complementary", { name: "Vault overview details" });
    expect(within(details).getByRole("region", { name: "Recent activity" })).toBeInTheDocument();
    expect(within(details).getByRole("region", { name: "Commit history" })).toBeInTheDocument();
    expect(details).not.toContainElement(screen.getByRole("region", { name: "Collection workspace" }));
    expect(within(details).getAllByRole("region")).toHaveLength(2);
    expect(within(details).queryByRole("region", { name: "Vault guide" })).not.toBeInTheDocument();
    expect(within(details).queryByRole("region", { name: "Access and ownership" })).not.toBeInTheDocument();
    const summary = screen.getByRole("region", { name: "Vault summary" });
    expect(summary).toContainElement(screen.getByRole("region", { name: "Vault guide" }));
    expect(summary).toContainElement(screen.getByRole("region", { name: "Access and ownership" }));
    expect(summary.compareDocumentPosition(screen.getByRole("region", { name: "Collection workspace" })) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByRole("region", { name: "Collection workspace" }).compareDocumentPosition(details) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(details).queryByRole("region", { name: "Tables" })).toBeNull();
    expect(screen.queryByRole("link", { name: /catalog|inventory/ })).toBeNull();
    const contents = screen.getByRole("region", { name: "Contents" });
    expect(within(contents).getByText("Tables").closest("div")).toHaveTextContent("4");
    expect(within(contents).getByText("Files").closest("div")).toHaveTextContent("2");
    const recent = screen.getByRole("region", { name: "Recent activity" });
    expect(await within(recent).findByText("Nothing written yet")).toBeInTheDocument();
    expect(within(recent).queryByRole("link")).toBeNull();
  });

  it("shows an unavailable context rather than a permanent loading skeleton after info fails", async () => {
    getVaultInfoMock.mockRejectedValue(new Error("Unavailable"));
    getRecentMock.mockResolvedValueOnce({ changes: [{ doc_id: "d-recent", path: "guides/runbook.md", title: "Available recent update" }] });
    renderVault();
    expect(await screen.findByText("Vault details are unavailable.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeEnabled();
    expect(screen.queryByRole("region", { name: "Contents" })).toBeNull();
    expect(screen.queryByRole("button", { name: "New content" })).not.toBeInTheDocument();
    const details = screen.getByRole("complementary", { name: "Vault overview details" });
    expect(await within(details).findByRole("link", { name: /Available recent update/ })).toHaveAttribute("href", "/vault/my-v/doc/guides%2Frunbook.md");
  });
});


it("opens an existing guide as a preview over the current Overview", async () => {
  const user = userEvent.setup();
  renderVault();
  await user.click(await findChip());
  const state = JSON.parse(screen.getByTestId("preview-state").textContent!);
  expect(state.documentPreview).toBe(true);
  expect(state.backgroundLocation.pathname).toBe("/vault/my-v");
  expect(state.returnFocusId).toBe("vault-overview-guide");
});

it("keeps visible skeleton geometry while vault details are pending", async () => {
  getVaultInfoMock.mockReturnValueOnce(new Promise(() => {}));
  renderVault();
  const loading = screen.getByRole("status", { name: "Loading vault overview…" });
  expect(loading).toBeVisible();
  expect(loading.querySelectorAll('[data-slot="skeleton"]').length || loading.querySelectorAll('.animate-pulse').length).toBeGreaterThan(3);
  expect(screen.queryByRole("region", { name: "my-v Vault overview" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "New content" })).not.toBeInTheDocument();
});
