import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { VaultDirectorySidebar } from "@/components/vault-directory-sidebar";
import { useVaults } from "@/hooks/use-vaults";
import { useResourceNavigation } from "@/contexts/resource-navigation-context";

vi.mock("@/hooks/use-vaults", () => ({ useVaults: vi.fn() }));
vi.mock("@/hooks/use-vault-favorites", () => ({ useVaultFavorites: () => ({ isFavorite: (id: string) => id === "fav", favOrder: () => 0 }) }));
vi.mock("@/contexts/resource-navigation-context", () => ({ useResourceNavigation: vi.fn() }));
const rows = [
  { id: "plain", name: "engineering", role: "owner" },
  { id: "fav", name: "research", role: "reader" },
  { id: "long", name: "긴 이름 / #? a complete human vault name", role: "writer" },
];
const refetch = vi.fn();
const requestNavigation = vi.fn(() => true);
afterEach(cleanup);
function Location() { const location = useLocation(); return <output aria-label="Location">{location.pathname}</output>; }
function mount() {
  const onNavigate = vi.fn(), onCreateVault = vi.fn();
  render(<MemoryRouter initialEntries={["/vault"]}><VaultDirectorySidebar onCreateVault={onCreateVault} onNavigate={onNavigate} /><Location /></MemoryRouter>);
  return { onNavigate, onCreateVault };
}
beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  requestNavigation.mockReturnValue(true);
  vi.mocked(useVaults).mockReturnValue({ vaults: rows, loading: false, error: false, refetch });
  vi.mocked(useResourceNavigation).mockReturnValue({ requestNavigation, registerGuard: vi.fn(), continueNavigation: vi.fn(), completeSessionNavigation: vi.fn() });
});

describe("Vault directory sidebar", () => {
  it("shows favorites first, searches full native links, and clears role/name scope together", async () => {
    const user = userEvent.setup();
    mount();
    const nav = screen.getByRole("navigation", { name: "Vaults" });
    expect(within(nav).getAllByRole("link").map(link => link.textContent)).toEqual(["research", "engineering", rows[2].name]);
    expect(within(nav).getByRole("link", { name: rows[2].name })).toHaveAttribute("href", `/vault/${encodeURIComponent(rows[2].name)}`);
    await user.type(screen.getByRole("searchbox", { name: "Find a vault in sidebar" }), "human");
    expect(within(nav).getAllByRole("link")).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Filter sidebar vaults by role" }));
    await user.click(screen.getByRole("button", { name: "Owned" }));
    expect(within(nav).getByRole("status")).toHaveTextContent("No vaults match these filters");
    await user.click(screen.getByRole("button", { name: "Reset filters" }));
    expect(within(nav).getAllByRole("link")).toHaveLength(3);
    expect(screen.getByRole("searchbox")).toHaveValue("");
  });

  it("honors navigation guards and native modified activation", async () => {
    const user = userEvent.setup();
    const { onNavigate } = mount();
    const link = screen.getByRole("link", { name: "engineering" });
    fireEvent.click(link, { metaKey: true });
    expect(requestNavigation).not.toHaveBeenCalled();
    expect(onNavigate).not.toHaveBeenCalled();
    requestNavigation.mockReturnValue(false);
    await user.click(link);
    expect(screen.getByLabelText("Location")).toHaveTextContent(/^\/vault$/);
    requestNavigation.mockReturnValue(true);
    await user.click(link);
    expect(screen.getByLabelText("Location")).toHaveTextContent("/vault/engineering");
    expect(onNavigate).toHaveBeenCalledOnce();
  });

  it("keeps role scope while clearing a name query with the keyboard", async () => {
    const user = userEvent.setup();
    mount();
    await user.click(screen.getByRole("button", { name: "Filter sidebar vaults by role" }));
    await user.click(screen.getByRole("button", { name: "Owned" }));
    const search = screen.getByRole("searchbox", { name: "Find a vault in sidebar" });
    await user.type(search, "engine");
    await user.tab();
    expect(screen.getByRole("button", { name: "Clear find a vault in sidebar" })).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(search).toHaveFocus();
    expect(search).toHaveValue("");
    expect(screen.getByRole("button", { name: "Filter sidebar vaults by role, 1 active" })).toHaveAttribute("aria-expanded", "true");
    expect(within(screen.getByRole("navigation", { name: "Vaults" })).getAllByRole("link")).toHaveLength(1);
  });

  it("keeps management controls before search in keyboard order", async () => {
    const user = userEvent.setup();
    mount();
    screen.getByRole("searchbox", { name: "Find a vault in sidebar" }).focus();
    await user.tab({ shift: true });
    expect(screen.getByRole("button", { name: "Create vault" })).toHaveFocus();
    await user.tab({ shift: true });
    const filter = screen.getByRole("button", { name: "Filter sidebar vaults by role" });
    expect(filter).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(filter).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("group", { name: "Filter sidebar vaults by your role" })).toBeVisible();
  });

  it("keeps stale snapshots usable on refresh failure and provides Retry and Create", async () => {
    vi.mocked(useVaults).mockReturnValue({ vaults: rows, loading: false, error: true, refetch });
    const { onCreateVault } = mount();
    expect(screen.getByText(/Couldn't update vaults\. Previous list shown/)).toBeVisible();
    expect(screen.getByRole("navigation", { name: "Vaults" })).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(refetch).toHaveBeenCalledOnce();
    await userEvent.click(screen.getByRole("button", { name: "Create vault" }));
    expect(onCreateVault).toHaveBeenCalledOnce();
  });
});
