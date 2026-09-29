import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Link, MemoryRouter } from "react-router-dom";
import { VaultExplorer } from "@/components/vault-explorer";
import { browseVault, getVaultInfo } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/api")>(),
  browseVault: vi.fn(),
  getVaultInfo: vi.fn(),
}));

const sample = {
  vault: "v",
  path: "",
  items: [
    { type: "collection", name: "architecture", path: "architecture", doc_count: 2 },
    { type: "document", name: "Schema", path: "architecture/schema.md" },
    { type: "document", name: "System", path: "architecture/system.md" },
    { type: "table", name: "owners", path: "owners", collection: "architecture" },
    { type: "table", name: "teams", path: "teams", collection: "architecture" },
    { type: "file", name: "diagram.png", path: "diagram.png", uri: "akb://v/coll/architecture/file/file-1", collection: "architecture" },
    { type: "file", name: "map.png", path: "map.png", uri: "akb://v/coll/architecture/file/file-2", collection: "architecture" },
  ],
};

const resources = [
  { group: "Documents", activeRoute: "/vault/v/doc/architecture%2Fschema.md", nextRoute: "/vault/v/doc/architecture%2Fsystem.md", activeName: /Document:\s*Schema/, nextName: /Document:\s*System/ },
  { group: "Tables", activeRoute: "/vault/v/table/owners", nextRoute: "/vault/v/table/teams", activeName: /Table:\s*owners/, nextName: /Table:\s*teams/ },
  { group: "Files", activeRoute: "/vault/v/file/file-1", nextRoute: "/vault/v/file/file-2", activeName: /File:\s*diagram.png/, nextName: /File:\s*map.png/ },
];

function renderAt(activeRoute: string, nextRoute: string) {
  return render(
    <MemoryRouter initialEntries={[activeRoute]}>
      <Link to={nextRoute}>Open another resource</Link>
      <VaultExplorer vault="v" />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.mocked(browseVault).mockReset();
  vi.mocked(browseVault).mockResolvedValue(sample);
  vi.mocked(getVaultInfo).mockReset();
  vi.mocked(getVaultInfo).mockResolvedValue({ role: "reader" });
  localStorage.clear();
});

afterEach(cleanup);

describe("VaultExplorer resource group disclosure", () => {
  it.each(resources)("shows the owning collection in the header for $group", async ({ activeRoute, nextRoute, activeName }) => {
    const { container } = renderAt(activeRoute, nextRoute);
    await screen.findByRole("treeitem", { name: activeName });

    const header = container.querySelector('[data-slot="collection-identity-header"]');
    expect(header).toHaveTextContent("architecture");
    expect(header).not.toHaveTextContent("All collections");
  });

  it.each(resources)("lets the current $group group stay collapsed through refresh", async ({ group, activeRoute, nextRoute, activeName }) => {
    localStorage.setItem("akb-explorer-expanded:v", JSON.stringify(["architecture"]));
    const user = userEvent.setup();
    renderAt(activeRoute, nextRoute);
    await screen.findByRole("treeitem", { name: activeName });

    const disclosure = screen.getByRole("treeitem", { name: `${group}, 2 items` });
    await user.click(disclosure);
    expect(disclosure).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("treeitem", { name: activeName })).not.toBeInTheDocument();

    vi.mocked(browseVault).mockResolvedValue({
      ...sample,
      items: [
        ...sample.items.map((item) => ({ ...item })),
        { type: "collection", name: "refreshed", path: "refreshed", doc_count: 0 },
      ],
    });
    await user.click(screen.getByRole("button", { name: "Refresh collections" }));
    await screen.findByRole("button", { name: "refreshed" });
    expect(screen.getByRole("treeitem", { name: `${group}, 2 items` })).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("treeitem", { name: activeName })).not.toBeInTheDocument();

    disclosure.focus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("treeitem", { name: activeName })).toHaveAttribute("aria-current", "page");
    await user.keyboard("{ArrowLeft}");
    expect(disclosure).toHaveAttribute("aria-expanded", "false");
  });

  it.each(resources)("reveals $group on navigation and allows a new manual collapse", async ({ group, activeRoute, nextRoute, activeName, nextName }) => {
    const user = userEvent.setup();
    renderAt(activeRoute, nextRoute);
    await screen.findByRole("treeitem", { name: activeName });

    await user.click(screen.getByRole("treeitem", { name: `${group}, 2 items` }));
    await user.click(screen.getByRole("button", { name: "architecture" }));
    await user.click(screen.getByRole("link", { name: "Open another resource" }));

    await waitFor(() => expect(screen.getByRole("treeitem", { name: nextName })).toHaveAttribute("aria-current", "page"));
    const disclosure = screen.getByRole("treeitem", { name: `${group}, 2 items` });
    expect(disclosure).toHaveAttribute("aria-expanded", "true");
    await user.click(disclosure);
    expect(disclosure).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("treeitem", { name: nextName })).not.toBeInTheDocument();
  });
});
