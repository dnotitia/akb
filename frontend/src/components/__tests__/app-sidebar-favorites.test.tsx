import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AppSidebar } from "@/components/app-sidebar";
import { listVaults } from "@/lib/api";

vi.mock("@/contexts/current-user-context", () => ({ useCurrentUser: () => ({ user_id: "sidebar-test" }) }));
vi.mock("@/lib/api", () => ({ listVaults: vi.fn() }));
const key = "akb-vault-favorites:v2:sidebar-test";
const vaults = Array.from({ length: 7 }, (_, index) => ({ id: `id-${index}`, name: `vault-${index}`, role: "writer" }));

function mount(compact = false, path = "/vault/vault-0/settings") {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={[path]}><AppSidebar compact={compact} /></MemoryRouter>
  </QueryClientProvider>);
}
beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear();
  localStorage.setItem(key, JSON.stringify([...vaults.map(v => v.id), "revoked"]));
  vi.mocked(listVaults).mockResolvedValue({ vaults });
});

describe("Workspace favorite navigation", () => {
  it("keeps account Settings last in the fixed support area, distinct from Vault settings", () => {
    mount();
    const support = screen.getByRole("navigation", { name: "Workspace support" });
    const settings = within(support).getByRole("link", { name: "Settings" });
    expect(settings).toHaveAttribute("href", "/settings");
    expect(settings).not.toHaveAttribute("aria-current");
    expect(support.lastElementChild).toBe(settings);
  });

  it.each([false, true])("marks Settings active and preserves the open section (compact=%s)", async compact => {
    mount(compact, "/settings?tab=tokens");
    const settings = screen.getByRole("link", { name: "Settings" });
    expect(settings).toHaveAttribute("aria-current", "page");
    expect(settings).toHaveAttribute("href", "/settings?tab=tokens");
    expect(screen.queryByRole("link", { name: "Connect AI tools" })).not.toBeInTheDocument();
    if (compact) {
      const user = userEvent.setup();
      await user.hover(settings);
      expect(await screen.findByRole("tooltip", { name: "Settings" })).toBeVisible();
    }
  });
  it("labels favorites and imports only explicitly confirmed accessible legacy entries", async () => {
    localStorage.setItem(key, JSON.stringify(["id-0"]));
    localStorage.setItem("akb-vault-favorites", JSON.stringify(["id-1", "inaccessible"]));
    mount(); const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Import saved favorites" }));
    expect(screen.getByRole("dialog", { name: "Import saved favorites?" })).toBeVisible();
    expect(JSON.parse(localStorage.getItem(key)!)).toEqual(["id-0"]);
    await user.click(screen.getByRole("button", { name: "Import favorites" }));
    expect(JSON.parse(localStorage.getItem(key)!)).toEqual(["id-0", "id-1"]);
    expect(screen.getByText("Favorites", { exact: true })).toBeVisible();
  });

  it("omits duplicate inbox and connection navigation while retaining Settings and help", async () => {
    mount(); const user = userEvent.setup();
    expect(screen.queryByRole("link", { name: /Inbox/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Connect AI tools" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/settings");
    await user.click(screen.getByRole("button", { name: "Help" }));
    expect(screen.getByRole("dialog", { name: "Getting around AKB" })).toBeVisible();
    await user.keyboard("{Escape}");
    expect(screen.getByRole("button", { name: "Help" })).toHaveFocus();
  });
  it("shows five accessible favorites with explicit more/less", async () => {
    mount(); const user = userEvent.setup();
    const favorites = await screen.findByRole("navigation", { name: "Favorite vaults" });
    const disclosure = screen.getByRole("button", { name: "Collapse favorite vaults" });
    expect(disclosure.closest('[data-slot="workspace-favorites-heading"]')).toContainElement(screen.getByText("Favorites", { exact: true }));
    expect(within(screen.getByRole("navigation", { name: "Workspace navigation" })).queryByRole("button")).not.toBeInTheDocument();
    expect(within(favorites).getAllByRole("link")).toHaveLength(5);
    expect(within(favorites).getByRole("link", { name: "vault-0" })).toHaveAttribute("aria-current", "location");
    expect(screen.getByRole("link", { name: "Vaults" })).not.toHaveAttribute("aria-current");
    await user.click(screen.getByRole("button", { name: "Show 2 more" }));
    expect(within(favorites).getAllByRole("link")).toHaveLength(7);
    await user.click(screen.getByRole("button", { name: "Show less" }));
    expect(within(favorites).getAllByRole("link")).toHaveLength(5);
    await user.click(screen.getByRole("button", { name: "Collapse favorite vaults" }));
    expect(screen.queryByRole("navigation", { name: "Favorite vaults" })).not.toBeInTheDocument();
    expect(screen.getByText("Favorites", { exact: true })).toBeVisible();
    expect(screen.getByRole("link", { name: "Vaults" })).toHaveAttribute("aria-current", "page");
    await user.click(screen.getByRole("button", { name: "Expand favorite vaults" }));
    expect(screen.getByRole("navigation", { name: "Favorite vaults" })).toBeVisible();
  });

  it("keeps compact navigation to primary links only", () => {
    mount(true);
    expect(screen.queryByRole("navigation", { name: "Favorite vaults" })).not.toBeInTheDocument();
    expect(within(screen.getByRole("navigation", { name: "Workspace navigation" })).getAllByRole("link").map(link => link.textContent || link.getAttribute("aria-label"))).toEqual(["Home", "Search", "Vaults"]);
  });

  it("removes a favorite through the menu and restores focus", async () => {
    mount(); const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Options for vault-0" }));
    await user.click(screen.getByRole("menuitem", { name: "Remove from favorites" }));
    expect(screen.queryByRole("link", { name: "vault-0" })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Collapse favorite vaults" })).toHaveFocus());
    expect(JSON.parse(localStorage.getItem(key)!)).not.toContain("id-0");
  });

  it("updates when another surface changes the favorites", async () => {
    mount(); await screen.findByRole("navigation", { name: "Favorite vaults" });
    act(() => {
      localStorage.setItem(key, JSON.stringify(["id-6"]));
      window.dispatchEvent(new CustomEvent("akb:vault-favorites-changed", { detail: key }));
    });
    expect(screen.getByRole("link", { name: "vault-6" })).toBeVisible();
    expect(screen.queryByRole("link", { name: "vault-0" })).not.toBeInTheDocument();
  });

  it("does not invent an empty section for inaccessible favorites", async () => {
    vi.mocked(listVaults).mockResolvedValue({ vaults: [] });
    mount();
    await waitFor(() => expect(listVaults).toHaveBeenCalled());
    expect(screen.queryByRole("navigation", { name: "Favorite vaults" })).not.toBeInTheDocument();
  });
});
