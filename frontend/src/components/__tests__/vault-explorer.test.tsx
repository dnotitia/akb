import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within, cleanup } from "@testing-library/react";
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
  ApiError: class ApiError extends Error {
    status?: number;
  },
}));

// Pull the mock references after declaration so we can set per-test responses.
import { browseVault, getVaultInfo } from "@/lib/api";
const browseMock = browseVault as unknown as ReturnType<typeof vi.fn>;
const vaultInfoMock = getVaultInfo as unknown as ReturnType<typeof vi.fn>;

const sample = {
  vault: "v",
  path: "",
  items: [
    { type: "collection", name: "architecture", path: "architecture", doc_count: 2 },
    { type: "collection", name: "guides", path: "guides", doc_count: 1 },
    { type: "document", name: "Schema", path: "architecture/schema.md" },
    { type: "document", name: "System", path: "architecture/system.md" },
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
  localStorage.clear();
});

afterEach(() => cleanup());

describe("VaultExplorer — rendering", () => {
  it("renders collections from browse response", async () => {
    renderAt("/vault/v");
    expect(await screen.findByRole("button", { name: /architecture/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /guides/ })).toBeInTheDocument();
    expect(screen.getByRole("treeitem", { name: /audit_log/ })).toBeInTheDocument();
  });

  it("exposes ARIA treeview semantics", async () => {
    renderAt("/vault/v");
    const tree = await screen.findByRole("tree", { name: /v explorer/ });
    expect(tree).toBeInTheDocument();
    const items = within(tree).getAllByRole("treeitem");
    expect(items.length).toBeGreaterThan(0);
    const collection = within(tree).getByRole("button", { name: /architecture/ });
    expect(collection.parentElement).toHaveAttribute("aria-expanded", "false");
  });

  it("auto-reveals ancestors of the active document", async () => {
    renderAt("/vault/v/doc/architecture%2Fschema.md");
    const item = await screen.findByRole("treeitem", { name: /Schema/ });
    expect(item).toHaveAttribute("aria-current", "page");
  });
});

describe("VaultExplorer — interaction", () => {
  it("toggles a collection on click", async () => {
    const user = userEvent.setup();
    renderAt("/vault/v");
    const btn = await screen.findByRole("button", { name: /architecture/ });
    expect(btn.parentElement).toHaveAttribute("aria-expanded", "false");
    await user.click(btn);
    expect(btn.parentElement).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("treeitem", { name: /Schema/ })).toBeInTheDocument();
  });

  it("filters the tree by name", async () => {
    const user = userEvent.setup();
    renderAt("/vault/v");
    await screen.findByRole("button", { name: /architecture/ });
    const filter = screen.getByLabelText(/filter tree/i);
    await user.type(filter, "schema");
    expect(screen.getByRole("treeitem", { name: /Schema/ })).toBeInTheDocument();
    expect(screen.queryByRole("treeitem", { name: /Start/ })).not.toBeInTheDocument();
  });

  it("ArrowDown moves focus through the visible list", async () => {
    const user = userEvent.setup();
    renderAt("/vault/v");
    const first = await screen.findByRole("button", { name: /architecture/ });
    first.focus();
    await user.keyboard("{ArrowDown}");
    expect(document.activeElement).toBe(screen.getByRole("button", { name: /guides/ }));
  });

  it("End jumps to last visible row", async () => {
    const user = userEvent.setup();
    renderAt("/vault/v");
    const first = await screen.findByRole("button", { name: /architecture/ });
    first.focus();
    await user.keyboard("{End}");
    expect(document.activeElement).toBe(screen.getByRole("treeitem", { name: /audit_log/ }));
  });

  it("typeahead jumps to a row starting with the typed prefix", async () => {
    const user = userEvent.setup();
    renderAt("/vault/v");
    const first = await screen.findByRole("button", { name: /architecture/ });
    first.focus();
    await user.keyboard("g");
    expect(document.activeElement).toBe(screen.getByRole("button", { name: /guides/ }));
  });

  it("ArrowRight expands a collapsed collection; ArrowLeft collapses it", async () => {
    const user = userEvent.setup();
    renderAt("/vault/v");
    const btn = await screen.findByRole("button", { name: /architecture/ });
    btn.focus();
    await user.keyboard("{ArrowRight}");
    expect(btn.parentElement).toHaveAttribute("aria-expanded", "true");
    await user.keyboard("{ArrowLeft}");
    expect(btn.parentElement).toHaveAttribute("aria-expanded", "false");
  });
});

describe("VaultExplorer — role gating", () => {
  it("renders the '+ COLL' button for writer+ roles", async () => {
    vaultInfoMock.mockResolvedValue({ role: "writer" });
    renderAt("/vault/v");
    // The header-affordance button has the exact aria-label "New collection".
    // Other affordances (bottom-of-section, sub-collection) have distinct
    // accessible names so this match remains unambiguous after the
    // 3-tier rollout.
    expect(
      await screen.findByRole("button", { name: /^new collection$/i }),
    ).toBeInTheDocument();
  });

  it("hides the '+ COLL' button for reader role", async () => {
    vaultInfoMock.mockResolvedValue({ role: "reader" });
    renderAt("/vault/v");
    // Wait for the tree to render before asserting absence.
    await screen.findByRole("button", { name: /architecture/ });
    expect(
      screen.queryByRole("button", { name: /^new collection$/i }),
    ).not.toBeInTheDocument();
  });

  it("renders the bottom-of-section '+ NEW COLLECTION' for writer role", async () => {
    vaultInfoMock.mockResolvedValue({ role: "writer" });
    renderAt("/vault/v");
    // The header button uses aria-label="New collection", the bottom-of-
    // section button has visible text "+ NEW COLLECTION". Look for the
    // text variant explicitly.
    expect(await screen.findByText(/\+ NEW COLLECTION/)).toBeInTheDocument();
  });

  it("hides the bottom-of-section '+ NEW COLLECTION' for reader role", async () => {
    vaultInfoMock.mockResolvedValue({ role: "reader" });
    renderAt("/vault/v");
    await screen.findByRole("button", { name: /architecture/ });
    expect(screen.queryByText(/\+ NEW COLLECTION/)).not.toBeInTheDocument();
  });

  it("renders the per-collection-row '+ sub-collection' button for writer role", async () => {
    vaultInfoMock.mockResolvedValue({ role: "writer" });
    renderAt("/vault/v");
    expect(
      await screen.findByRole("button", { name: /create sub-collection in architecture/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /create sub-collection in guides/i }),
    ).toBeInTheDocument();
  });

  it("hides the per-collection-row '+ sub-collection' button for reader role", async () => {
    vaultInfoMock.mockResolvedValue({ role: "reader" });
    renderAt("/vault/v");
    await screen.findByRole("button", { name: /architecture/ });
    expect(
      screen.queryByRole("button", { name: /create sub-collection/i }),
    ).not.toBeInTheDocument();
  });
});

describe("VaultExplorer — error & empty", () => {
  it("shows a message when browse fails", async () => {
    browseMock.mockRejectedValueOnce(new Error("boom"));
    renderAt("/vault/v");
    expect(await screen.findByText(/⚠ boom/)).toBeInTheDocument();
  });

  it("shows EMPTY when the vault has no items", async () => {
    browseMock.mockResolvedValueOnce({ vault: "v", path: "", items: [] });
    renderAt("/vault/v");
    expect(await screen.findByText(/— EMPTY —/)).toBeInTheDocument();
  });
});
