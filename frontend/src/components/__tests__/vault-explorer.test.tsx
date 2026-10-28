import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, within, cleanup, waitFor, fireEvent, act } from "@testing-library/react";
import { render } from "@/test-query-client";
import userEvent from "@testing-library/user-event";
import { Link, MemoryRouter, useLocation } from "react-router-dom";
import { ResourceLocationProvider, usePublishResourceLocation, type ResourceLocation } from "@/contexts/resource-location-context";
import { VaultExplorer } from "@/components/vault-explorer";
import { CurrentUserProvider } from "@/contexts/current-user-context";

vi.mock("@/lib/api", async (original) => ({
  ...await original<typeof import("@/lib/api")>(),
  browseVault: vi.fn(),
  getVaultInfo: vi.fn(),
  // Mutations are not exercised by these tests but the explorer imports
  // them transitively via the dialog components.
  createCollection: vi.fn(),
  deleteCollection: vi.fn(),
  updateCollection: vi.fn(),
  uploadVaultFile: vi.fn(),
  createVaultTable: vi.fn(),
  deleteDocument: vi.fn(),
  deleteVaultFile: vi.fn(),
  deleteVaultTable: vi.fn(),
  ApiError: class ApiError extends Error {
    status?: number;
  },
}));

// Pull the mock references after declaration so we can set per-test responses.
import {
  browseVault,
  createVaultTable,
  getVaultInfo,
  uploadVaultFile,
} from "@/lib/api";
const browseMock = browseVault as unknown as ReturnType<typeof vi.fn>;
const vaultInfoMock = getVaultInfo as unknown as ReturnType<typeof vi.fn>;
const uploadMock = uploadVaultFile as unknown as ReturnType<typeof vi.fn>;
const tableCreateMock = createVaultTable as unknown as ReturnType<typeof vi.fn>;

const sample = {
  complete: true,
  next_cursor: null,
  vault: "v",
  path: "",
  items: [
    {
      type: "collection",
      name: "architecture",
      path: "architecture",
      summary: "System boundaries and ownership.",
      doc_count: 2,
    },
    { type: "collection", name: "guides", path: "guides", doc_count: 1 },
    { type: "document", name: "Schema", path: "architecture/schema.md" },
    { type: "document", name: "System", path: "architecture/system.md" },
    { type: "table", name: "owners", path: "owners", collection: "architecture" },
    {
      type: "file",
      name: "diagram.png",
      path: "diagram.png",
      uri: "akb://v/coll/architecture/file/file-1",
      collection: "architecture",
    },
    { type: "document", name: "Start", path: "guides/start.md" },
    { type: "table", name: "audit_log", path: "audit_log" },
  ],
};

function renderAt(pathname: string, presentation: "standalone" | "focused" = "standalone") {
  return render(
    <MemoryRouter initialEntries={[pathname]}>
      <VaultExplorer vault="v" presentation={presentation} />
    </MemoryRouter>,
  );
}

function PublishCurrent({ value, onlyOnResource = false }: { value: ResourceLocation; onlyOnResource?: boolean }) {
  const { pathname } = useLocation();
  usePublishResourceLocation(onlyOnResource && !pathname.includes("/doc/") ? null : value);
  return null;
}

beforeEach(() => {
  browseMock.mockReset();
  browseMock.mockImplementation(async (_vault: string, _collection: string, _depth: number, options: { name_query?: string } = {}) => ({
    ...sample, name_query: options.name_query,
    items: options.name_query ? sample.items.filter(item => `${item.name} ${item.path}`.toLocaleLowerCase().includes(options.name_query!.toLocaleLowerCase())) : sample.items,
  }));
  vaultInfoMock.mockReset();
  // Default to reader so the existing tests don't accidentally render
  // the mutation affordances. Tests that need writer+ override this.
  vaultInfoMock.mockResolvedValue({ role: "reader" });
  uploadMock.mockReset();
  uploadMock.mockResolvedValue({
    uri: "akb://v/coll/architecture/file/file-1",
    name: "diagram.png",
  });
  tableCreateMock.mockReset();
  tableCreateMock.mockResolvedValue({ name: "owners_new" });
  localStorage.clear();
});

afterEach(() => cleanup());

describe("focused content navigation", () => {
  it("keeps sibling Collections visible when a document is opened without requiring a filter reset", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/vault/v"]}><ResourceLocationProvider identity="reader">
      <PublishCurrent onlyOnResource value={{ vault: "v", title: "Start", kind: "Document", collectionPath: "guides", resourceRef: "guides/start.md", status: "active" }} />
      <VaultExplorer vault="v" presentation="focused" />
    </ResourceLocationProvider></MemoryRouter>);
    await user.click(await screen.findByRole("button", { name: "architecture" }));
    await user.click(screen.getByRole("button", { name: "guides" }));
    await user.click(screen.getByRole("link", { name: /^Document:\s*Start$/ }));
    await waitFor(() => expect(screen.getByRole("link", { name: /^Document:\s*Start$/ })).toHaveAttribute("aria-current", "page"));
    expect(screen.getByRole("button", { name: "architecture" })).toBeVisible();
    expect(screen.getByRole("link", { name: /^Document:\s*Schema$/ })).toBeVisible();
    expect(screen.getByRole("link", { name: /^Document:\s*System$/ })).toBeVisible();
    expect(screen.getByRole("button", { name: "Filter content" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Reset filters" })).not.toBeInTheDocument();
  });

  it("keeps identity in the parent and exposes native collection disclosures and document links", async () => {
    const user = userEvent.setup();
    const { container } = renderAt("/vault/v", "focused");
    const collection = await screen.findByRole("button", { name: "guides" });
    expect(container.querySelector('[data-slot="collection-identity-header"]')).toBeNull();
    expect(container.querySelector('[data-slot="collection-management-row"]')).toHaveTextContent("Collections");
    expect(screen.queryByText("Content", { exact: true })).not.toBeInTheDocument();
    expect(screen.getByRole("searchbox", { name: "Filter resources" })).toBeVisible();
    expect(screen.queryByRole("tree")).not.toBeInTheDocument();
    expect(screen.queryByRole("treeitem")).not.toBeInTheDocument();
    expect(collection).toHaveAttribute("aria-expanded", "false");
    await user.click(collection);
    expect(collection).toHaveAttribute("aria-expanded", "true");
    const link = within(screen.getByRole("navigation", { name: "v explorer" })).getByRole("link", { name: /Document:\s*Start/ });
    expect(link).toHaveAttribute("href", "/vault/v/doc/guides%2Fstart.md");
    expect(link).not.toHaveAttribute("aria-selected");
    expect(screen.queryByRole("button", { name: "Add content" })).not.toBeInTheDocument();
  });

  it("keeps search available while advanced filters are closed and restores it after Reset", async () => {
    const user = userEvent.setup();
    renderAt("/vault/v", "focused");
    await screen.findByRole("button", { name: "architecture" });
    await user.click(screen.getByRole("button", { name: "Filter content" }));
    const query = screen.getByRole("searchbox", { name: "Filter resources" });
    await waitFor(() => expect(query).toHaveFocus());
    await user.type(query, "Schema");
    await user.click(screen.getByRole("button", { name: "Filter content, 1 active" }));
    expect(query).toBeVisible();
    expect(screen.queryByRole("button", { name: "Collection search scope" })).not.toBeInTheDocument();
    expect(screen.getByText("Name: Schema")).toBeVisible();
    expect(screen.getByRole("link", { name: /Document:\s*Schema/ })).toBeVisible();
    await waitFor(() => expect(screen.queryByRole("button", { name: "guides" })).not.toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Reset filters" }));
    expect(query).toHaveFocus();
    expect(await screen.findByRole("button", { name: "guides" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Filter content" }));
    expect(screen.getByRole("searchbox", { name: "Filter resources" })).toHaveValue("");
  });

  it("reveals the name field when a collection action narrows the scope", async () => {
    const user = userEvent.setup();
    renderAt("/vault/v", "focused");
    await user.click(await screen.findByRole("button", { name: "Collection actions for architecture" }));
    await user.click(screen.getByRole("menuitem", { name: "Search in collection" }));
    await waitFor(() => expect(screen.getByRole("searchbox", { name: "Filter resources" })).toHaveFocus());
    await user.click(screen.getByRole("button", { name: "Filter content, 1 active" }));
    expect(screen.getByText("Collection: architecture")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Reset filters" }));
    expect(screen.getByRole("button", { name: "guides" })).toBeVisible();
  });

  it("clears only the standalone search query and keeps Collection scope", async () => {
    const user = userEvent.setup();
    renderAt("/vault/v", "focused");
    await user.click(await screen.findByRole("button", { name: "Collection actions for architecture" }));
    await user.click(screen.getByRole("menuitem", { name: "Search in collection" }));
    const search = screen.getByRole("searchbox", { name: "Filter resources" });
    await user.type(search, "Schema");
    expect(screen.getByRole("button", { name: "Filter content, 2 active" })).toBeVisible();
    await user.tab();
    expect(screen.getByRole("button", { name: "Clear filter resources" })).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(search).toHaveValue("");
    expect(search).toHaveFocus();
    expect(screen.getByRole("button", { name: "Filter content, 1 active" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Collection search scope" })).toHaveTextContent("architecture");
    await waitFor(() => expect(screen.queryByRole("button", { name: "guides" })).not.toBeInTheDocument());
  });

  it("updates the focused list after a document state change without manual refresh", async () => {
    renderAt("/vault/v", "focused");
    await screen.findByRole("button", { name: "guides" });
    browseMock.mockResolvedValue({ ...sample, items: [...sample.items, { type: "collection", name: "reference", path: "reference" }] });
    fireEvent(window, new CustomEvent("akb:document-status-changed", { detail: { vault: "v" } }));
    expect(await screen.findByRole("button", { name: "reference" })).toBeVisible();
    expect(browseMock).toHaveBeenCalledTimes(2);
  });

  it("offers one creation menu only after write authority is verified", async () => {
    vaultInfoMock.mockResolvedValue({ role: "writer" });
    const user = userEvent.setup();
    renderAt("/vault/v", "focused");
    const create = await screen.findByRole("button", { name: "Add content" });
    expect(create).toHaveTextContent("");
    expect(screen.queryByText("New content", { exact: true })).not.toBeInTheDocument();
    await user.click(create);
    for (const name of ["New document", "Upload file", "New table", "New collection"]) {
      expect(screen.getByRole("menuitem", { name })).toBeVisible();
    }
  });

  it("returns from an add dialog to the persistent plus button", async () => {
    vaultInfoMock.mockResolvedValue({ role: "writer" });
    renderAt("/vault/v", "focused");
    const user = userEvent.setup();
    const add = await screen.findByRole("button", { name: "Add content" });
    await user.click(add);
    await user.click(screen.getByRole("menuitem", { name: "Upload file" }));
    const dialog = screen.getByRole("dialog", { name: "Upload a file" });
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(add).toHaveFocus());
  });

  it("keeps refresh available after an initial read failure without opening filters", async () => {
    browseMock.mockRejectedValueOnce(new Error("Content unavailable"));
    const user = userEvent.setup();
    renderAt("/vault/v", "focused");
    expect(await screen.findByRole("alert")).toHaveTextContent("Content unavailable");
    expect(screen.getByRole("searchbox", { name: "Filter resources" })).toBeVisible();
    const refresh = screen.getByRole("button", { name: "Refresh content" });
    expect(refresh).toHaveTextContent("Refresh content");
    expect(refresh.closest('[data-slot="sidebar-search-toolbar"]')).toBeNull();
    await user.click(refresh);
    expect(await screen.findByRole("button", { name: "guides" })).toBeVisible();
    expect(screen.getByRole("navigation", { name: "v explorer" })).not.toHaveAttribute("aria-busy");
  });

  it("moves between a disclosure and its child without trapping ordinary Tab navigation", async () => {
    const user = userEvent.setup();
    renderAt("/vault/v", "focused");
    const collection = await screen.findByRole("button", { name: "guides" });
    collection.focus();
    await user.keyboard("{ArrowRight}{ArrowRight}");
    const child = screen.getByRole("link", { name: /Document:\s*Start/ });
    expect(child).toHaveFocus();
    await user.keyboard("{ArrowLeft}");
    expect(collection).toHaveFocus();
    await user.keyboard("{ArrowLeft}");
    expect(collection).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("link", { name: /Document:\s*Start/ })).not.toBeInTheDocument();
  });
});

describe("collection exploration", () => {
  const nested = { ...sample, items: [...sample.items,
    { type: "collection", name: "Design decisions", path: "architecture/design" },
    { type: "collection", name: "Older designs", path: "architecture/design-old" },
    { type: "document", name: "Shared decision", path: "architecture/design/decision.md" },
    { type: "document", name: "Shared old decision", path: "architecture/design-old/decision.md" },
  ] };

  async function explore(user: ReturnType<typeof userEvent.setup>, path: string) {
    await user.click(await screen.findByRole("button", { name: `Collection actions for ${path}` }));
    await user.click(screen.getByRole("menuitem", { name: "Explore from here" }));
  }

  it("narrows only the rail, preserves filters, and returns to its actual parent", async () => {
    browseMock.mockImplementation(async (_vault: string, _collection: string, _depth: number, options: { name_query?: string } = {}) => ({
      ...nested,
      name_query: options.name_query,
      items: options.name_query
        ? nested.items.filter(item => `${item.name} ${item.path}`.toLocaleLowerCase().includes(options.name_query!.toLocaleLowerCase()))
        : nested.items,
    }));
    const user = userEvent.setup();
    const { container } = renderAt("/vault/v/doc/architecture%2Fdesign%2Fdecision.md", "focused");
    await screen.findByRole("button", { name: "design" });
    await user.type(screen.getByRole("searchbox", { name: "Filter resources" }), "Shared");
    await waitFor(() => expect(browseMock).toHaveBeenCalledWith("v", undefined, -1, expect.objectContaining({ name_query: "Shared" })));
    const reads = browseMock.mock.calls.length;
    await explore(user, "architecture/design");
    const context = screen.getByRole("navigation", { name: "Collection exploration" });
    expect(within(context).getByRole("button", { name: "Collection exploration path" })).toHaveTextContent("design");
    expect(within(context).getByRole("button", { name: "Back to collection architecture" })).toHaveFocus();
    expect(container.querySelector('[data-sig="collection:architecture/design"]')).toBeNull();
    expect(await screen.findByRole("link", { name: /Document:\s*Shared decision/ })).toHaveAttribute("aria-current", "page");
    expect(screen.queryByRole("link", { name: /Document:\s*Shared old decision/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Reset filters" }));
    expect(context).toBeVisible();
    expect(screen.getByRole("searchbox", { name: "Filter resources" })).toHaveValue("");
    await user.click(within(context).getByRole("button", { name: "Back to collection architecture" }));
    expect(within(context).getByRole("button", { name: "Back to all collections" })).toHaveFocus();
    await screen.findByRole("button", { name: "design" });
    expect(screen.getByRole("button", { name: "design-old" })).toBeVisible();
    await user.click(within(context).getByRole("button", { name: "Collection exploration path" }));
    await user.click(screen.getByRole("menuitem", { name: "All collections" }));
    expect(screen.queryByRole("navigation", { name: "Collection exploration" })).not.toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "guides" })).toBeVisible();
    expect(browseMock.mock.calls.length).toBeGreaterThan(reads);
    expect(browseMock.mock.calls.every(call => call[3]?.page_size === 100 && !call[3]?.cursor)).toBe(true);
  });

  it("does not reset explicit exploration because a different document is already open", async () => {
    const user = userEvent.setup();
    browseMock.mockResolvedValue({ ...sample, items: [...sample.items, { type: "document", name: "Other guide", path: "guides/other.md" }] });
    render(<MemoryRouter initialEntries={["/vault/v/doc/guides%2Fstart.md"]}>
      <Link to="/vault/v/doc/guides%2Fstart.md?view=raw">Change reading mode</Link>
      <Link to="/vault/v/doc/guides%2Fother.md">Open another destination</Link>
      <VaultExplorer vault="v" presentation="focused" />
    </MemoryRouter>);
    await explore(user, "architecture");
    expect(screen.getByRole("navigation", { name: "Collection exploration" })).toBeVisible();
    await waitFor(() => expect(screen.queryByRole("button", { name: "guides" })).not.toBeInTheDocument());
    await user.click(screen.getByRole("link", { name: "Change reading mode" }));
    expect(screen.getByRole("navigation", { name: "Collection exploration" })).toBeVisible();
    await user.click(screen.getByRole("link", { name: "Open another destination" }));
    await waitFor(() => expect(screen.queryByRole("navigation", { name: "Collection exploration" })).not.toBeInTheDocument());
    expect(screen.getByRole("link", { name: /Document:\s*Other guide/ })).toHaveAttribute("aria-current", "page");
  });

  it("resets exploration when the account or Vault changes", async () => {
    const user = userEvent.setup();
    const explorer = (vault: string, id: string) => <MemoryRouter><CurrentUserProvider user={{ user_id: id, username: id, display_name: id, email: `${id}@example.test`, is_admin: false, auth_method: "local", key_class: null }}><VaultExplorer vault={vault} presentation="focused" /></CurrentUserProvider></MemoryRouter>;
    const { rerender } = render(explorer("v", "alice"));
    await explore(user, "architecture");
    rerender(explorer("v", "bob"));
    expect(screen.queryByRole("navigation", { name: "Collection exploration" })).not.toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "guides" })).toBeVisible();
    await explore(user, "architecture");
    rerender(explorer("other", "bob"));
    expect(screen.queryByRole("navigation", { name: "Collection exploration" })).not.toBeInTheDocument();
  });

  it("resolves an outside destination that arrives after its browse refresh", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/vault/v/doc/architecture%2Fschema.md"]}>
      <Link to="/vault/v/doc/guides%2Fnew.md">Open new resource</Link>
      <VaultExplorer vault="v" presentation="focused" />
    </MemoryRouter>);
    await explore(user, "architecture");
    let finish!: (value: typeof sample) => void;
    await screen.findByRole("link", { name: /Document:\s*Schema/ });
    let pending = true;
    browseMock.mockImplementation((_vault: string, _collection: string, depth: number) => {
      if (depth === -1 && pending) { pending = false; return new Promise<typeof sample>(resolve => { finish = resolve; }); }
      return Promise.resolve(sample);
    });
    await user.click(screen.getByRole("button", { name: "Refresh content" }));
    await user.click(screen.getByRole("link", { name: "Open new resource" }));
    expect(screen.getByRole("navigation", { name: "Collection exploration" })).toBeVisible();
    const updated = { ...sample, items: [...sample.items, { type: "document", name: "New resource", path: "guides/new.md" }] };
    browseMock.mockResolvedValue(updated);
    await act(async () => finish(updated));
    await waitFor(() => expect(screen.queryByRole("navigation", { name: "Collection exploration" })).not.toBeInTheDocument());
    expect(await screen.findByRole("link", { name: /Document:\s*New resource/ })).toHaveAttribute("aria-current", "page");
  });
});

describe("collection shortcut navigation", () => {
  it("keeps the scope selector inside the filters disclosure instead of the initial rail", async () => {
    const user = userEvent.setup();
    renderAt("/vault/v");
    await screen.findByRole("button", { name: "architecture" });
    expect(screen.getByRole("searchbox", { name: "Filter resources" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Collection search scope" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Filter collections" }));
    expect(screen.getByRole("button", { name: "Collection search scope" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Filter collections" }));
    expect(screen.queryByRole("button", { name: "Collection search scope" })).not.toBeInTheDocument();
  });

  it("counts a collapsed Collection scope and clears it with Reset filters while preserving the name query", async () => {
    const user = userEvent.setup();
    renderAt("/vault/v");
    await user.click(await screen.findByRole("button", { name: "Collection actions for architecture" }));
    await user.click(screen.getByRole("menuitem", { name: "Search in collection" }));
    expect(screen.getByRole("button", { name: "Collection search scope" })).toHaveTextContent("architecture");
    const toggle = screen.getByRole("button", { name: "Filter collections, 1 active" });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    await user.click(toggle);
    expect(screen.queryByRole("button", { name: "Collection search scope" })).not.toBeInTheDocument();
    const query = screen.getByRole("searchbox", { name: "Filter resources" });
    await user.type(query, "Start");
    expect(screen.queryByRole("treeitem", { name: /Document:\s*Start/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Reset filters" }));
    expect(query).toHaveValue("Start");
    expect(query).toHaveFocus();
    expect(screen.getByRole("treeitem", { name: /Document:\s*Start/ })).toBeVisible();
    expect(screen.getByRole("button", { name: "Filter collections" })).toHaveAttribute("aria-expanded", "false");
    await user.click(screen.getByRole("button", { name: "Filter collections" }));
    expect(screen.getByRole("button", { name: "Collection search scope" })).toHaveTextContent("All collections");
  });

  it.each([
    { name: "composing", event: { isComposing: true } },
    { name: "legacy composition key", event: { keyCode: 229 } },
  ])("does not select a Collection when Enter confirms an IME $name", async ({ event }) => {
    const user = userEvent.setup();
    renderAt("/vault/v");
    await screen.findByRole("button", { name: "architecture" });
    await user.click(screen.getByRole("button", { name: "Filter collections" }));
    await user.click(screen.getByRole("button", { name: "Collection search scope" }));
    const picker = screen.getByRole("combobox", { name: "Find a collection" });
    await user.type(picker, "architecture");
    fireEvent.keyDown(picker, { key: "Enter", ...event });
    expect(screen.getByRole("combobox", { name: "Find a collection" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Collection search scope" })).toHaveTextContent("All collections");
    await user.keyboard("{Enter}");
    expect(screen.queryByRole("combobox", { name: "Find a collection" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Collection search scope" })).toHaveTextContent("architecture");
  });

  it("lets an explicit Collection shortcut reveal its target outside a previous filter scope", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/vault/v"]}><Link to="/vault/v?collection=guides">Open guides shortcut</Link><VaultExplorer vault="v" /></MemoryRouter>);
    await user.click(await screen.findByRole("button", { name: "Collection actions for architecture" }));
    await user.click(screen.getByRole("menuitem", { name: "Search in collection" }));
    await user.click(screen.getByRole("link", { name: "Open guides shortcut" }));
    const guides = await screen.findByRole("button", { name: "guides" });
    await waitFor(() => expect(guides).toHaveFocus());
    expect(screen.getByRole("button", { name: "Collection search scope" })).toHaveTextContent("guides");
    expect(browseMock).toHaveBeenCalledWith("v", "guides", -1, expect.objectContaining({ page_size: 100 }));
  });

  it("chooses a nested Collection with the keyboard and keeps only its resource subtree", async () => {
    browseMock.mockResolvedValue({ ...sample, items: [...sample.items,
      { type: "collection", name: "design", path: "architecture/design" },
      { type: "collection", name: "design-old", path: "architecture/design-old" },
      { type: "collection", name: "sub", path: "architecture/design/sub" },
      { type: "document", name: "Shared guide", path: "architecture/design/guide.md" },
      { type: "document", name: "Shared child", path: "architecture/design/sub/child.md" },
      { type: "document", name: "Shared outside", path: "architecture/design-old/guide.md" },
      { type: "table", name: "shared_table", path: "shared_table", collection: "architecture/design" },
      { type: "file", name: "shared.txt", path: "shared.txt", collection: "architecture/design", uri: "akb://v/coll/architecture/design/file/shared" },
    ] });
    const user = userEvent.setup();
    renderAt("/vault/v");
    await screen.findByRole("button", { name: "architecture" });
    await user.click(screen.getByRole("button", { name: "Filter collections" }));
    await user.click(screen.getByRole("button", { name: "Collection search scope" }));
    const picker = screen.getByRole("combobox", { name: "Find a collection" });
    await user.type(picker, "architecture/design");
    await user.keyboard("{ArrowDown}{Enter}");
    expect(screen.getByRole("button", { name: "Collection search scope" })).toHaveTextContent("architecture/design");
    await user.type(screen.getByRole("searchbox", { name: "Filter resources" }), "shared");
    expect(screen.getByRole("treeitem", { name: /Document:\s*Shared guide/ })).toBeInTheDocument();
    expect(screen.getByRole("treeitem", { name: /Document:\s*Shared child/ })).toBeInTheDocument();
    expect(screen.getByRole("treeitem", { name: /Table:\s*shared_table/ })).toBeInTheDocument();
    expect(screen.getByRole("treeitem", { name: /File:\s*shared.txt/ })).toBeInTheDocument();
    expect(screen.queryByRole("treeitem", { name: /Shared outside/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear collection scope" }));
    expect(screen.getByRole("searchbox", { name: "Filter resources" })).toHaveValue("shared");
    expect(screen.getByRole("treeitem", { name: /Shared outside/ })).toBeInTheDocument();
  });

  it("does not restore a previous account or Vault's Collection scope", async () => {
    const user = userEvent.setup();
    const explorer = (vault: string, id: string) => <MemoryRouter><CurrentUserProvider user={{ user_id: id, username: id, display_name: id, email: `${id}@example.test`, is_admin: false, auth_method: "local", key_class: null }}><VaultExplorer vault={vault} /></CurrentUserProvider></MemoryRouter>;
    const { rerender } = render(explorer("v", "alice"));
    await user.click(await screen.findByRole("button", { name: "Collection actions for architecture" }));
    await user.click(screen.getByRole("menuitem", { name: "Search in collection" }));
    await user.type(screen.getByRole("searchbox", { name: "Filter resources" }), "Schema");
    rerender(explorer("v", "bob"));
    expect(screen.getByRole("button", { name: "Collection search scope" })).toHaveTextContent("All collections");
    expect(screen.getByRole("searchbox", { name: "Filter resources" })).toHaveValue("");
    rerender(explorer("v", "alice"));
    expect(screen.getByRole("button", { name: "Collection search scope" })).toHaveTextContent("All collections");
    await user.click(screen.getByRole("button", { name: "Collection actions for architecture" }));
    await user.click(screen.getByRole("menuitem", { name: "Search in collection" }));
    rerender(explorer("other", "alice"));
    expect(screen.getByRole("button", { name: "Collection search scope" })).toHaveTextContent("All collections");
    rerender(explorer("v", "alice"));
    expect(screen.getByRole("button", { name: "Collection search scope" })).toHaveTextContent("All collections");
  });

  it("filters a collection inline for readers instead of launching a search modal", async () => {
    const user = userEvent.setup();
    renderAt("/vault/v");
    await user.click(await screen.findByRole("button", { name: "Collection actions for architecture" }));
    await user.click(screen.getByRole("menuitem", { name: "Search in collection" }));
    const query = screen.getByRole("searchbox", { name: "Filter resources" });
    await waitFor(() => expect(query).toHaveFocus());
    expect(screen.getByRole("button", { name: "Collection search scope" })).toHaveTextContent("architecture");
    await waitFor(() => expect(screen.queryByRole("button", { name: "guides" })).not.toBeInTheDocument());
    await user.type(query, "diagram");
    expect(screen.getByRole("treeitem", { name: /File:\s*diagram.png/ })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("treeitem", { name: /Document:\s*Schema/ })).not.toBeInTheDocument());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("does not steal focus for a Search collection filter", async () => {
    renderAt("/vault/v/search?collection=architecture");
    const row = await screen.findByRole("button", { name: "architecture" });
    expect(row).not.toHaveFocus();
    expect(row.closest('[role="treeitem"]')).not.toHaveAttribute("aria-selected", "true");
  });
  it("reveals and focuses a requested collection without navigating to a document", async () => {
    renderAt("/vault/v?collection=architecture");
    const row = await screen.findByRole("button", { name: "architecture" });
    await waitFor(() => expect(row).toHaveFocus());
    expect(row.closest('[role="treeitem"]')).toHaveAttribute("aria-selected", "true");
    expect(row.closest('[role="treeitem"]')).toHaveAttribute("aria-expanded", "true");
  });
});

describe("archive document navigation", () => {
  it("keeps identity concise and preserves a query while changing and resetting filters", async () => {
    browseMock.mockImplementation(async (_v: string, _c: unknown, _d: number, options: { archive_scope?: string } = {}) => ({ ...sample, archive_scope: options.archive_scope ?? "unarchived" }));
    const { container } = renderAt("/vault/v/doc/architecture%2Fschema.md");
    const user = userEvent.setup();
    await screen.findByRole("button", { name: "architecture" });
    expect(container.querySelector('[data-slot="collection-identity-header"]')).toHaveTextContent("architecture");
    expect(container.querySelector('[data-slot="collection-identity-header"] svg.lucide-folder')).toHaveAttribute("aria-hidden", "true");
    expect(container.querySelector('[data-slot="collection-identity-header"]')).not.toHaveTextContent("Collections");
    expect(container.querySelector('[data-slot="collection-management-row"]')).toHaveTextContent("Collections");
    expect(screen.queryByText("Manage content")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Collection document state" })).not.toBeInTheDocument();
    const query = screen.getByRole("searchbox", { name: "Filter resources" });
    await user.type(query, "not found");
    await user.click(screen.getByRole("button", { name: "Filter collections" }));
    await user.click(screen.getByRole("button", { name: "Collection document state" }));
    await user.click(screen.getByRole("menuitemradio", { name: /^All documents/ }));
    expect(query).toHaveValue("not found");
    await user.click(screen.getByRole("button", { name: "Filter collections, 1 active" }));
    expect(screen.queryByRole("button", { name: "Collection document state" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Reset filters" }));
    expect(query).toHaveValue("not found");
    await user.click(screen.getByRole("button", { name: "Clear filter resources" }));
    expect(query).toHaveValue("");
    expect(query).toHaveFocus();
  });

  it("keeps the scope selector available in an empty Vault and gives compatibility recovery", async () => {
    browseMock.mockResolvedValue({ vault: "v", path: "", items: [], complete: true });
    renderAt("/vault/v");
    const user = userEvent.setup();
    await screen.findByText(/No collections yet/);
    await user.click(screen.getByRole("button", { name: "Filter collections" }));
    await user.click(screen.getByRole("button", { name: "Collection document state" }));
    await user.click(screen.getByRole("menuitemradio", { name: /Archived documents/ }));
    await screen.findByText(/This server does not support/);
    expect(screen.queryByText("No archived documents in this Vault.")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "show current documents" }));
    await screen.findByText(/No collections yet/);
  });

  it("shows confirmed archives at their Collection path with a readable state marker", async () => {
    vaultInfoMock.mockResolvedValue({ role: "owner" });
    browseMock.mockImplementation(async (_v: string, _c: unknown, _d: number, options: { archive_scope?: string } = {}) => ({
      vault: "v", path: "", archive_scope: options.archive_scope ?? "unarchived",
      items: options.archive_scope === "archived" ? [{ type: "document", name: "Old guide", path: "guides/old.md", status: "archived" }] : [],
    }));
    renderAt("/vault/v");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Filter collections" }));
    await user.click(screen.getByRole("button", { name: "Collection document state" }));
    await user.click(screen.getByRole("menuitemradio", { name: /Archived documents/ }));
    await user.click(await screen.findByRole("button", { name: "guides" }));
    expect(screen.getByRole("treeitem", { name: /Document:\s*Old guide\s*Archived/ })).toHaveAttribute("href", "/vault/v/doc/guides%2Fold.md");
    expect(screen.getByRole("button", { name: "Resource type" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Collection actions for guides" }));
    expect(screen.queryByRole("menuitem", { name: /Delete collection/ })).not.toBeInTheDocument();
  });
});

describe("VaultExplorer — rendering", () => {
  it.each([
    ["Document", "guides/last.md", "/vault/v/doc/guides%2Flast.md"],
    ["File", "file-last", "/vault/v/file/file-last"],
    ["Table", "table_last", "/vault/v/table/table_last"],
  ] as const)("reveals an authorized current %s without narrowing the Collection list or traversing every page", async (kind, ref, route) => {
    browseMock.mockResolvedValue({ ...sample, complete: false, next_cursor: "next", items: [sample.items[0]] });
    // A table's authorized name is also its routing identity; unlike a
    // Document title or File display name, it is not an arbitrary alias.
    const title = kind === "Table" ? ref : "Authorized outlier";
    render(<MemoryRouter initialEntries={[route]}><ResourceLocationProvider identity="reader">
      <PublishCurrent value={{ vault: "v", title, kind, collectionPath: "guides", resourceRef: ref, status: "active" }} />
      <VaultExplorer vault="v" />
    </ResourceLocationProvider></MemoryRouter>);
    const row = await screen.findByRole("treeitem", { name: new RegExp(`^${kind}:\\s*${title}$`) });
    expect(row).toBeVisible();
    expect(row).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("button", { name: "architecture" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Reset filters" })).not.toBeInTheDocument();
    expect(browseMock.mock.calls.every(call => !call[3]?.cursor)).toBe(true);
    expect(browseMock.mock.calls.every(call => !call[1])).toBe(true);
    expect(browseMock).toHaveBeenCalledWith("v", undefined, -1, expect.objectContaining({ page_size: 100, content_type: "all" }));
  });

  it("resets the old kind scope on resource navigation but preserves a later manual collapse", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/vault/v"]}><ResourceLocationProvider identity="reader">
      <Link to="/vault/v/doc/guides%2Flast.md">Open outlier</Link>
      <PublishCurrent onlyOnResource value={{ vault: "v", title: "Outlier", kind: "Document", collectionPath: "guides", resourceRef: "guides/last.md", status: "active" }} />
      <VaultExplorer vault="v" />
    </ResourceLocationProvider></MemoryRouter>);
    await screen.findByRole("button", { name: "architecture" });
    await user.click(screen.getByRole("button", { name: "Filter collections" }));
    await user.click(screen.getByRole("button", { name: "Resource type" }));
    await user.click(screen.getByRole("menuitemradio", { name: /^Tables/ }));
    await user.click(screen.getByRole("link", { name: "Open outlier" }));
    expect(await screen.findByRole("treeitem", { name: /^Document:\s*Outlier$/ })).toHaveAttribute("aria-current", "page");
    const guides = screen.getByRole("button", { name: "guides" });
    await user.click(guides);
    await user.click(screen.getByRole("button", { name: "Refresh collections" }));
    await waitFor(() => expect(screen.getByRole("tree", { name: "v explorer" })).not.toHaveAttribute("aria-busy", "true"));
    expect(screen.queryByRole("treeitem", { name: /^Document:\s*Outlier$/ })).not.toBeInTheDocument();
  });

  it("does not claim unrequested kinds are empty after a single-kind page completes", async () => {
    const user = userEvent.setup();
    renderAt("/vault/v");
    await screen.findByRole("button", { name: "architecture" });
    await user.click(screen.getByRole("button", { name: "Filter collections" }));
    await user.click(screen.getByRole("button", { name: "Resource type" }));
    await user.click(screen.getByRole("menuitemradio", { name: /^Documents/ }));
    await user.click(screen.getByRole("button", { name: "Resource type" }));
    const tables = screen.getByRole("menuitemradio", { name: /^Tables/ });
    expect(tables).toHaveTextContent("Browse tables");
    expect(tables).not.toHaveTextContent("0 tables");
  });

  it("reveals the same resource again after visiting the Vault and changing filters", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/vault/v/doc/guides%2Flast.md"]}><ResourceLocationProvider identity="reader">
      <Link to="/vault/v">Return to Vault</Link><Link to="/vault/v/doc/guides%2Flast.md">Reopen outlier</Link>
      <PublishCurrent onlyOnResource value={{ vault: "v", title: "Outlier", kind: "Document", collectionPath: "guides", resourceRef: "guides/last.md" }} />
      <VaultExplorer vault="v" />
    </ResourceLocationProvider></MemoryRouter>);
    await screen.findByRole("treeitem", { name: /^Document:\s*Outlier$/ });
    await user.click(screen.getByRole("link", { name: "Return to Vault" }));
    await user.click(screen.getByRole("button", { name: /^Filter collections/ }));
    await user.click(screen.getByRole("button", { name: "Resource type" }));
    await user.click(screen.getByRole("menuitemradio", { name: /^Tables/ }));
    await user.click(screen.getByRole("link", { name: "Reopen outlier" }));
    expect(await screen.findByRole("treeitem", { name: /^Document:\s*Outlier$/ })).toHaveAttribute("aria-current", "page");
  });

  it("keeps the reserved guide protected when its authorized row is outside the page", async () => {
    vaultInfoMock.mockResolvedValue({ role: "writer" });
    browseMock.mockResolvedValue({ ...sample, complete: false, next_cursor: "next", items: [sample.items[0]] });
    render(<MemoryRouter initialEntries={["/vault/v/doc/overview%2Fvault-skill.md"]}><ResourceLocationProvider identity="writer">
      <PublishCurrent value={{ vault: "v", title: "Vault guide", kind: "Document", collectionPath: "overview", resourceRef: "overview/vault-skill.md" }} />
      <VaultExplorer vault="v" />
    </ResourceLocationProvider></MemoryRouter>);
    expect(await screen.findByRole("treeitem", { name: /^Skill:\s*Vault guide/ })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Actions for Vault guide" })).not.toBeInTheDocument();
  });
  it("renders collections from browse response", async () => {
    renderAt("/vault/v");
    expect(await screen.findByRole("button", { name: /^architecture/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^guides/i })).toBeInTheDocument();
    expect(screen.getByRole("treeitem", { name: /audit_log/ })).toBeInTheDocument();
  });

  it("exposes the full name via a title attribute so truncated rows reveal it on hover", async () => {
    renderAt("/vault/v");
    // Collection row: the name span carries title=name for the CSS-truncated label.
    const collectionName = await screen.findByText("architecture");
    expect(collectionName).toHaveAttribute(
      "title",
      "architecture — System boundaries and ownership.",
    );
    // Leaf row (root-level table is visible without expanding any collection).
    expect(screen.getByText("audit_log")).toHaveAttribute("title", "audit_log");
  });

  it("exposes ARIA treeview semantics", async () => {
    renderAt("/vault/v");
    const tree = await screen.findByRole("tree", { name: /v explorer/ });
    expect(tree).toBeInTheDocument();
    const items = within(tree).getAllByRole("treeitem");
    expect(items.length).toBeGreaterThan(0);
    const collection = within(tree).getByRole("button", { name: /^architecture/i });
    expect(collection.parentElement).toHaveAttribute("aria-expanded", "false");
  });

  it("keeps the collection row clean and exposes counts on kind groups", async () => {
    const user = userEvent.setup();
    renderAt("/vault/v");
    const architecture = await screen.findByRole("button", { name: /^architecture/i });
    expect(within(architecture).queryByText(/2 documents/i)).not.toBeInTheDocument();
    await user.click(architecture);
    expect(screen.getByRole("treeitem", { name: "Documents, 2 items" })).toBeInTheDocument();
    expect(screen.getByRole("treeitem", { name: "Tables, 1 item" })).toBeInTheDocument();
    expect(screen.getByRole("treeitem", { name: "Files, 1 item" })).toBeInTheDocument();
  });

  it("shows a compact file identifier only for duplicate sibling document titles", async () => {
    browseMock.mockResolvedValueOnce({
      vault: "v",
      path: "",
      items: [
        { type: "collection", name: "runbooks", path: "runbooks" },
        { type: "document", name: "Incident response", path: "runbooks/incident-response.md" },
        { type: "document", name: "Incident response", path: "runbooks/incident-response-2f3df21c.md" },
      ],
    });
    const user = userEvent.setup();
    renderAt("/vault/v");

    await user.click(await screen.findByRole("button", { name: /^runbooks/i }));

    expect(screen.getAllByRole("treeitem", { name: /Incident response/ })).toHaveLength(2);
    expect(screen.getByText("incident-response")).toBeInTheDocument();
    expect(screen.getByText("incident-response · 2f3d")).toBeInTheDocument();
  });

  it("opens a stable details view for the collection summary", async () => {
    const user = userEvent.setup();
    renderAt("/vault/v");
    await user.click(
      await screen.findByRole("button", {
        name: /collection actions for architecture/i,
      }),
    );
    await user.click(screen.getByRole("menuitem", { name: /view details/i }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("System boundaries and ownership.")).toBeInTheDocument();
    expect(within(dialog).getByText("Loaded documents")).toBeInTheDocument();
    expect(within(dialog).getByText(/not the complete Collection/)).toBeInTheDocument();
    expect(within(dialog).getByText("Loaded tables")).toBeInTheDocument();
    expect(within(dialog).getByText("Loaded files")).toBeInTheDocument();
  });

  it("opens row options without expanding and restores focus before title activation", async () => {
    const user = userEvent.setup();
    renderAt("/vault/v");
    const title = await screen.findByRole("button", { name: /^architecture$/i });
    const row = title.closest('[role="treeitem"]')!;
    const options = within(row as HTMLElement).getByRole("button", { name: "Collection actions for architecture" });
    expect(within(row as HTMLElement).getAllByRole("button")).toHaveLength(2);
    expect(row).toHaveAttribute("aria-expanded", "false");
    await user.click(options);
    expect(row).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByRole("menuitem", { name: "Search in collection" })).toBeVisible();
    await user.keyboard("{Escape}");
    expect(options).toHaveFocus();
    await user.tab({ shift: true });
    expect(title).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(row).toHaveAttribute("aria-expanded", "true");
  });

  it("auto-reveals ancestors of the active document", async () => {
    renderAt("/vault/v/doc/architecture%2Fschema.md");
    const item = await screen.findByRole("treeitem", { name: /Schema/ });
    expect(item).toHaveAttribute("aria-current", "page");
  });
});

describe("VaultExplorer — interaction", () => {
  it("preserves the current tree while a manual refresh is pending", async () => {
    let resolveRefresh: ((value: typeof sample) => void) | undefined;
    browseMock
      .mockResolvedValueOnce(sample)
      .mockImplementationOnce(() => new Promise((resolve) => {
        resolveRefresh = resolve;
      }));
    const user = userEvent.setup();
    renderAt("/vault/v");

    const architecture = await screen.findByRole("button", { name: /^architecture/i });
    await user.click(screen.getByRole("button", { name: "Refresh collections" }));

    expect(architecture).toBeInTheDocument();
    expect(screen.getByRole("tree", { name: /v explorer/ })).toHaveAttribute("aria-busy", "true");

    resolveRefresh?.(sample);
    await waitFor(() => {
      expect(screen.getByRole("tree", { name: /v explorer/ })).not.toHaveAttribute("aria-busy");
    });
  });

  it("toggles a collection on click", async () => {
    const user = userEvent.setup();
    renderAt("/vault/v");
    const btn = await screen.findByRole("button", { name: /^architecture/i });
    expect(btn.parentElement).toHaveAttribute("aria-expanded", "false");
    await user.click(btn);
    expect(btn.parentElement).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("treeitem", { name: /Schema/ })).toBeInTheDocument();
  });

  it("filters the tree by name", async () => {
    const user = userEvent.setup();
    renderAt("/vault/v");
    await screen.findByRole("button", { name: /^architecture/i });
    const filter = screen.getByLabelText(/filter resources/i);
    await user.type(filter, "schema");
    expect(screen.getByRole("treeitem", { name: /Schema/ })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("treeitem", { name: /Start/ })).not.toBeInTheDocument());
  });

  it("filters by resource kind without making users traverse unrelated rows", async () => {
    const user = userEvent.setup();
    renderAt("/vault/v");
    await user.click(screen.getByRole("button", { name: "Filter collections" }));
    await user.click(await screen.findByRole("button", { name: /resource type/i }));
    await user.click(screen.getByRole("menuitemradio", { name: /Tables/i }));
    await user.click(screen.getByRole("button", { name: /^architecture/i }));
    expect(screen.getByRole("treeitem", { name: /owners/i })).toBeInTheDocument();
    expect(screen.getByRole("treeitem", { name: /audit_log/i })).toBeInTheDocument();
    expect(screen.queryByRole("treeitem", { name: /Schema/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("treeitem", { name: /diagram.png/i })).not.toBeInTheDocument();
  });

  it("keeps tables and files reachable beside a large document set", async () => {
    const user = userEvent.setup();
    browseMock.mockResolvedValueOnce({
      vault: "v",
      complete: true, next_cursor: null,
      path: "",
      items: [
        { type: "collection", name: "large", path: "large" },
        ...Array.from({ length: 75 }, (_, index) => ({
          type: "document",
          name: `Doc ${String(index + 1).padStart(4, "0")}`,
          path: `large/${String(index + 1).padStart(4, "0")}.md`,
        })),
        { type: "table", name: "owners", path: "owners", collection: "large" },
        {
          type: "file",
          name: "diagram.png",
          path: "diagram.png",
          uri: "akb://v/coll/large/file/file-1",
          collection: "large",
        },
      ],
    });
    renderAt("/vault/v");
    await user.click(await screen.findByRole("button", { name: /^large/i }));
    expect(screen.getByRole("treeitem", { name: "Documents, 75 items" })).toBeInTheDocument();
    expect(screen.getByRole("treeitem", { name: "Tables, 1 item" })).toBeInTheDocument();
    expect(screen.getByRole("treeitem", { name: "Files, 1 item" })).toBeInTheDocument();
    expect(screen.getByRole("treeitem", { name: /owners/i })).toBeInTheDocument();
    expect(screen.getByRole("treeitem", { name: /diagram.png/i })).toBeInTheDocument();
    expect(screen.getByRole("treeitem", { name: /Doc 0021/i })).toBeInTheDocument();
    expect(screen.getByRole("treeitem", { name: /Doc 0070/i })).toBeInTheDocument();
    expect(screen.getByRole("treeitem", { name: /Doc 0075/i })).toBeInTheDocument();
  });

  it("ArrowDown moves focus through the visible list", async () => {
    const user = userEvent.setup();
    renderAt("/vault/v");
    const first = await screen.findByRole("button", { name: /^architecture/i });
    first.focus();
    await user.keyboard("{ArrowDown}");
    expect(document.activeElement).toBe(screen.getByRole("button", { name: /^guides/i }));
  });

  it("End jumps to last visible row", async () => {
    const user = userEvent.setup();
    renderAt("/vault/v");
    const first = await screen.findByRole("button", { name: /^architecture/i });
    first.focus();
    await user.keyboard("{End}");
    expect(document.activeElement).toBe(screen.getByRole("treeitem", { name: /audit_log/ }));
  });

  it("typeahead jumps to a row starting with the typed prefix", async () => {
    const user = userEvent.setup();
    renderAt("/vault/v");
    const first = await screen.findByRole("button", { name: /^architecture/i });
    first.focus();
    await user.keyboard("g");
    expect(document.activeElement).toBe(screen.getByRole("button", { name: /^guides/i }));
  });

  it("ArrowRight expands a collapsed collection; ArrowLeft collapses it", async () => {
    const user = userEvent.setup();
    renderAt("/vault/v");
    const btn = await screen.findByRole("button", { name: /^architecture/i });
    btn.focus();
    await user.keyboard("{ArrowRight}");
    expect(btn.parentElement).toHaveAttribute("aria-expanded", "true");
    await user.keyboard("{ArrowLeft}");
    expect(btn.parentElement).toHaveAttribute("aria-expanded", "false");
  });
});

describe("VaultExplorer — role gating", () => {
  it("puts all root creation actions in one header menu for writer role", async () => {
    const user = userEvent.setup();
    vaultInfoMock.mockResolvedValue({ role: "writer" });
    renderAt("/vault/v");
    await user.click(await screen.findByRole("button", { name: /add content/i }));
    expect(screen.getByRole("menuitem", { name: /new document/i })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /upload file/i })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /new table/i })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /new collection/i })).toBeInTheDocument();
  });

  it("hides root collection creation for reader role", async () => {
    vaultInfoMock.mockResolvedValue({ role: "reader" });
    renderAt("/vault/v");
    await screen.findByRole("button", { name: /^architecture/i });
    expect(
      screen.queryByRole("button", { name: /add content/i }),
    ).not.toBeInTheDocument();
  });

  it("groups collection write actions into one overflow menu for writer role", async () => {
    const user = userEvent.setup();
    vaultInfoMock.mockResolvedValue({ role: "writer" });
    renderAt("/vault/v");
    await user.click(
      await screen.findByRole("button", { name: /collection actions for architecture/i }),
    );
    expect(screen.getByRole("menuitem", { name: /new document/i })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /upload file/i })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /new table/i })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /new sub-collection/i })).toBeInTheDocument();
    // This collection contains a table, so Writer cannot use collection
    // deletion to bypass the table endpoint's Admin requirement.
    expect(screen.queryByRole("menuitem", { name: /delete collection/i })).not.toBeInTheDocument();
  });

  it("shows document/file delete actions to writers and table actions to admins", async () => {
    const user = userEvent.setup();
    vaultInfoMock.mockResolvedValue({ role: "writer" });
    const view = renderAt("/vault/v");
    await user.click(await screen.findByRole("button", { name: /^architecture/i }));

    expect(screen.getByRole("button", { name: "Actions for Schema" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Actions for diagram.png" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Actions for owners" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Actions for audit_log" })).not.toBeInTheDocument();

    view.unmount();
    vaultInfoMock.mockResolvedValue({ role: "admin" });
    renderAt("/vault/v");
    expect(await screen.findByRole("button", { name: "Actions for audit_log" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /collection actions for architecture/i }));
    expect(screen.getByRole("menuitem", { name: /delete collection/i })).toBeInTheDocument();
  });

  it("prefills the target collection for file and table creation", async () => {
    const user = userEvent.setup();
    vaultInfoMock.mockResolvedValue({ role: "writer" });
    renderAt("/vault/v");

    await user.click(
      await screen.findByRole("button", { name: /collection actions for architecture/i }),
    );
    await user.click(screen.getByRole("menuitem", { name: /upload file/i }));
    let dialog = screen.getByRole("dialog");
    expect(within(dialog).getByLabelText(/Collection/i)).toHaveValue("architecture");
    await user.click(within(dialog).getByRole("button", { name: /Cancel/i }));

    await user.click(
      screen.getByRole("button", { name: /collection actions for architecture/i }),
    );
    await user.click(screen.getByRole("menuitem", { name: /new table/i }));
    dialog = screen.getByRole("dialog");
    expect(within(dialog).getByLabelText(/Collection/i)).toHaveValue("architecture");
  });

  it("uploads a file into the collection selected from the overflow menu", async () => {
    const user = userEvent.setup();
    vaultInfoMock.mockResolvedValue({ role: "writer" });
    renderAt("/vault/v");
    await user.click(
      await screen.findByRole("button", { name: /collection actions for architecture/i }),
    );
    await user.click(screen.getByRole("menuitem", { name: /upload file/i }));
    const dialog = screen.getByRole("dialog");
    const file = new File(["image"], "diagram.png", { type: "image/png" });
    // The native picker remains with the upload owner outside the transient
    // dialog portal so foreground proof cannot detach its change listener.
    await user.click(within(dialog).getByRole("button", { name: /Choose a file or drop/ }));
    fireEvent.change(screen.getByLabelText(/^File/i), { target: { files: [file] } });
    await user.click(within(dialog).getByRole("button", { name: /^Upload file$/i }));
    expect(uploadMock).toHaveBeenCalledWith(
      "v",
      file,
      expect.objectContaining({ collection: "architecture" }),
    );
  });

  it("creates a table in the collection selected from the overflow menu", async () => {
    const user = userEvent.setup();
    vaultInfoMock.mockResolvedValue({ role: "writer" });
    renderAt("/vault/v");
    await user.click(
      await screen.findByRole("button", { name: /collection actions for architecture/i }),
    );
    await user.click(screen.getByRole("menuitem", { name: /new table/i }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText(/Table name/i), "owners_new");
    await user.type(within(dialog).getByLabelText(/^Name$/i), "owner_name");
    await user.click(within(dialog).getByRole("button", { name: /^Create table$/i }));
    expect(tableCreateMock).toHaveBeenCalledWith(
      "v",
      expect.objectContaining({
        name: "owners_new",
        collection: "architecture",
      }),
    );
  });

  it("keeps reader collection menus read-only", async () => {
    const user = userEvent.setup();
    vaultInfoMock.mockResolvedValue({ role: "reader" });
    renderAt("/vault/v");
    await user.click(
      await screen.findByRole("button", { name: /collection actions for architecture/i }),
    );
    expect(screen.getByRole("menuitem", { name: /view details/i })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /new sub-collection/i })).not.toBeInTheDocument();
  });
});

describe("VaultExplorer — reserved system collection", () => {
  const withOverview = {
    vault: "v",
    path: "",
    items: [
      { type: "collection", name: "architecture", path: "architecture" },
      { type: "collection", name: "overview", path: "overview" },
      { type: "document", name: "AKB Guide", path: "overview/vault-skill.md", doc_type: "skill" },
    ],
  };

  beforeEach(() => {
    browseMock.mockResolvedValue(withOverview);
  });

  it("suppresses the row actions on `overview` while keeping them on normal collections", async () => {
    const user = userEvent.setup();
    vaultInfoMock.mockResolvedValue({ role: "writer" });
    renderAt("/vault/v");
    // A normal collection keeps all write actions behind one trigger.
    await user.click(
      await screen.findByRole("button", { name: /collection actions for architecture/i }),
    );
    expect(screen.getByRole("menuitem", { name: /new document/i })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /new sub-collection/i })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /delete collection/i })).toBeInTheDocument();
    await user.keyboard("{Escape}");

    // The system collection keeps only its read-only details action.
    await user.click(
      screen.getByRole("button", { name: /collection actions for overview/i }),
    );
    expect(screen.getByRole("menuitem", { name: /view details/i })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /new document/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /new sub-collection/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /delete collection/i })).not.toBeInTheDocument();
  });

  it("renders `overview` first, marked as a system collection", async () => {
    vaultInfoMock.mockResolvedValue({ role: "writer" });
    renderAt("/vault/v");
    const tree = await screen.findByRole("tree", { name: /v explorer/ });
    // Not hidden — pinned to the top of the tree.
    const rows = within(tree).getAllByRole("treeitem");
    expect(rows[0]).toHaveTextContent("overview");
    expect(within(tree).getByTitle(/system collection/i)).toBeInTheDocument();
  });

  it("shows the provisioned guide even when the backend returns no collection row", async () => {
    browseMock.mockResolvedValueOnce({
      vault: "v",
      path: "",
      items: [
        { type: "document", name: "Vault guide", path: "overview/vault-skill.md", doc_type: "skill" },
      ],
    });
    renderAt("/vault/v");
    const tree = await screen.findByRole("tree", { name: /v explorer/ });
    expect(within(tree).getByRole("treeitem")).toHaveTextContent("overview");
    expect(within(tree).getByTitle(/system collection/i)).toBeInTheDocument();
    expect(screen.queryByText(/No collections yet/i)).toBeNull();
  });
});

describe("VaultExplorer — error & empty", () => {
  it("shows a message when browse fails", async () => {
    browseMock.mockRejectedValueOnce(new Error("boom"));
    renderAt("/vault/v");
    // The error now renders through the <Alert variant="destructive"> primitive
    // (role=alert + icon), not a hand-rolled "⚠ {error}" line.
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/boom/);
  });

  it("explains how the empty tree will fill when the vault has no items", async () => {
    browseMock.mockResolvedValueOnce({ vault: "v", path: "", items: [], complete: true });
    renderAt("/vault/v");
    expect(await screen.findByText(/No collections yet/i)).toBeInTheDocument();
  });
});
