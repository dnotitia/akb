import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within, cleanup, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Link, MemoryRouter } from "react-router-dom";
import { VaultExplorer } from "@/components/vault-explorer";
import { CurrentUserProvider } from "@/contexts/current-user-context";

vi.mock("@/lib/api", () => ({
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

function renderAt(pathname: string) {
  return render(
    <MemoryRouter initialEntries={[pathname]}>
      <VaultExplorer vault="v" />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  browseMock.mockReset();
  browseMock.mockResolvedValue(sample);
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
    expect(screen.getByRole("button", { name: "Collection search scope" })).toHaveTextContent("All collections");
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
    expect(screen.queryByRole("button", { name: "guides" })).not.toBeInTheDocument();
    await user.type(query, "diagram");
    expect(screen.getByRole("treeitem", { name: /File:\s*diagram.png/ })).toBeInTheDocument();
    expect(screen.queryByRole("treeitem", { name: /Document:\s*Schema/ })).not.toBeInTheDocument();
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
    browseMock.mockResolvedValue({ vault: "v", path: "", items: [] });
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
    expect(within(dialog).getByText("Documents")).toBeInTheDocument();
    expect(within(dialog).getByText("Tables")).toBeInTheDocument();
    expect(within(dialog).getByText("Files")).toBeInTheDocument();
  });

  it("keeps one compact actions trigger beside the collection name", async () => {
    renderAt("/vault/v");
    const name = await screen.findByText("architecture");
    const row = name.closest('[role="treeitem"]');
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getAllByRole("button")).toHaveLength(2);
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
    expect(screen.queryByRole("treeitem", { name: /Start/ })).not.toBeInTheDocument();
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
    expect(screen.queryByRole("treeitem", { name: /Doc 0021/i })).not.toBeInTheDocument();
    await user.click(screen.getByRole("treeitem", { name: /Show 50 more documents/i }));
    expect(screen.getByRole("treeitem", { name: /Doc 0070/i })).toBeInTheDocument();
    expect(screen.queryByRole("treeitem", { name: /Doc 0071/i })).not.toBeInTheDocument();
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
    await user.click(await screen.findByRole("button", { name: /create in vault/i }));
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
      screen.queryByRole("button", { name: /create in vault/i }),
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
    await user.upload(within(dialog).getByLabelText(/^File/i), file);
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
    browseMock.mockResolvedValueOnce({ vault: "v", path: "", items: [] });
    renderAt("/vault/v");
    expect(await screen.findByText(/No collections yet/i)).toBeInTheDocument();
  });
});
