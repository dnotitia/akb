import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within, cleanup, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { VaultExplorer } from "@/components/vault-explorer";

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
