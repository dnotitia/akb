import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Link, MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { useState } from "react";
import { VaultNavigationToggle } from "@/components/vault-navigation-controls";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import {
  VaultShell,
  type VaultNavigationControl,
} from "@/components/vault-shell";
vi.mock("@/components/vault-explorer", () => ({
  VaultExplorer: ({ presentation }: { presentation: string }) => (
    <nav aria-label="Content explorer" data-presentation={presentation}>
      <button>Collection disclosure</button>Collection tree content
    </nav>
  ),
}));
vi.mock("@/components/vault-navigation-header", () => ({
  VaultNavigationHeader: function MockVaultNavigationHeader({ navigationControl }: { navigationControl: VaultNavigationControl }) {
    const [open, setOpen] = useState(false);
    return (
    <nav aria-label="Current vault">
      <VaultNavigationToggle id="vault-navigation-collapse" {...navigationControl} />
      <TooltipProvider delayDuration={0}><Tooltip><TooltipTrigger asChild>
        <button data-vault-switcher-trigger="" aria-expanded={open} onClick={() => setOpen(value => !value)}>Switch vault: demo</button>
      </TooltipTrigger><TooltipContent>demo</TooltipContent></Tooltip></TooltipProvider>
      <Link to="/vault/demo">Vault overview</Link>
    </nav>
    );
  },
}));
vi.mock("@/hooks/use-vaults", () => ({
  useVaults: () => ({ vaults: [], refetch: vi.fn() }),
}));
vi.mock("@/components/vault-create-dialog", () => ({
  VaultCreateDialog: () => null,
}));
vi.mock("@/components/document-create-dialog", () => ({
  DocumentCreateDialog: () => null,
}));
vi.mock("@/components/app-page-location", () => ({
  AppPageLocation: () => <span>Current location</span>,
}));
vi.mock("@/hooks/use-column-resize", () => ({
  useColumnResize: ({ default: width }: { default: number }) => ({
    width,
    settledWidth: width,
    isResizing: false,
    cancel: vi.fn(),
    handlers: {},
  }),
}));
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
beforeEach(() => {
  localStorage.clear();
  vi.spyOn(window, "matchMedia").mockImplementation((query) => ({
    matches: true,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
});
function Layout() { return <Outlet />; }

function mount(entry = "/vault/demo") {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/vault/:name?" element={<VaultShell />}>
            <Route
              index
              element={
                <div>
                  Overview page<Link to="/vault/demo/search">Open search</Link><Link to="/vault/demo">Open demo</Link><Link to="/vault">Open vaults</Link><Link to="/vault/demo?collection=guides" state={{ vaultPageNavigation: "/vault/demo?collection=guides", collectionView: "main" }}>Open collection in main</Link>
                </div>
              }
            />
            {[
              "search",
              "members",
              "publications",
              "settings",
              "graph",
              "activity",
            ].map((route) => (
              <Route
                key={route}
                path={route}
                element={
                  <div>
                    {route} page<Link to="/vault/demo">Open overview</Link>
                  </div>
                }
              />
            ))}
          </Route>
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}
describe("Focused Vault navigation", () => {
  it.each([
    "",
    "search",
    "members",
    "publications",
    "settings",
    "graph",
    "activity",
  ])("keeps one context navigator on /%s", (route) => {
    mount(`/vault/demo/${route}`);
    expect(
      screen.getByRole("navigation", { name: "Content explorer" }),
    ).toHaveAttribute("data-presentation", "focused");
    expect(
      screen.getByRole("navigation", { name: "Current vault" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("navigation", { name: "Vaults" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("navigation", { name: "Vault sections" }),
    ).not.toBeInTheDocument();
    expect(screen.getAllByRole("separator")).toHaveLength(1);
  });
  it("preserves visibility through routes and returns focus to the persistent toggle", async () => {
    const user = userEvent.setup();
    mount("/vault/demo/search");
    await user.click(screen.getByRole("button", { name: "Close vault navigation" }));
    const trigger = screen.getByRole("button", { name: "Open vault navigation" });
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(
      screen.queryByRole("navigation", { name: "Content explorer" }),
    ).not.toBeInTheDocument();
    expect(localStorage.getItem("akb.treeVisible")).toBe("0");
    await user.click(screen.getByRole("link", { name: "Open overview" }));
    expect(
      screen.queryByRole("navigation", { name: "Content explorer" }),
    ).not.toBeInTheDocument();
    await user.click(trigger);
    expect(
      screen.getByRole("navigation", { name: "Content explorer" }),
    ).toBeVisible();
  });
  it("reveals a Collection shortcut without changing the saved choice", () => {
    localStorage.setItem("akb.treeVisible", "0");
    mount("/vault/demo?collection=guides");
    expect(
      screen.getByRole("navigation", { name: "Content explorer" }),
    ).toBeVisible();
    expect(localStorage.getItem("akb.treeVisible")).toBe("0");
  });
  it("lets a revealed Collection shortcut collapse without reopening an overlay", async () => {
    localStorage.setItem("akb.treeVisible", "0");
    mount("/vault/demo?collection=guides");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Close vault navigation" }));
    expect(screen.queryByRole("navigation", { name: "Content explorer" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Dismiss vault navigation" })).not.toBeInTheDocument();
    expect(localStorage.getItem("akb.treeVisible")).toBe("0");
  });

  it("does not treat a Search scope as a Collection shortcut", () => {
    localStorage.setItem("akb.treeVisible", "0");
    mount("/vault/demo/search?collection=guides");
    expect(
      screen.queryByRole("navigation", { name: "Content explorer" }),
    ).not.toBeInTheDocument();
  });
  it("toggles the same panel with its shortcut from inside content", async () => {
    mount();
    screen.getByRole("button", { name: "Collection disclosure" }).focus();
    fireEvent.keyDown(window, { key: "\\", ctrlKey: true });
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Open vault navigation" }),
      ).toHaveFocus(),
    );
    expect(
      screen.queryByRole("navigation", { name: "Content explorer" }),
    ).not.toBeInTheDocument();
    fireEvent.keyDown(window, { key: "\\", ctrlKey: true });
    expect(
      screen.getByRole("navigation", { name: "Content explorer" }),
    ).toBeVisible();
  });
  it("retains the Vault directory panel and swaps its contents when entering a Vault", async () => {
    localStorage.setItem("akb.treeVisible", "0");
    const user = userEvent.setup();
    mount("/vault");
    const panel = document.getElementById("vault-workspace-navigation");
    expect(panel).toBeVisible();
    expect(screen.getByRole("navigation", { name: "Vaults" })).toBeVisible();
    expect(screen.getByRole("separator")).toBeVisible();
    await user.click(screen.getByRole("link", { name: "Open demo" }));
    expect(document.getElementById("vault-workspace-navigation")).toBe(panel);
    expect(screen.getByRole("navigation", { name: "Content explorer" })).toBeVisible();
    expect(screen.queryByRole("navigation", { name: "Vaults" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: "Open vaults" }));
    expect(document.getElementById("vault-workspace-navigation")).toBe(panel);
    expect(screen.getByRole("navigation", { name: "Vaults" })).toBeVisible();
  });
  it("uses the same boundary opener and shortcut on the Vault directory without a parent-back link", async () => {
    const user = userEvent.setup();
    mount("/vault");
    expect(screen.queryByRole("link", { name: "Back to all vaults" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Close vault navigation" }));
    const open = screen.getByRole("button", { name: "Open vault navigation" });
    await waitFor(() => expect(open).toHaveFocus());
    expect(screen.queryByRole("navigation", { name: "Vaults" })).not.toBeInTheDocument();
    fireEvent.keyDown(window, { key: "\\", ctrlKey: true });
    expect(screen.getByRole("navigation", { name: "Vaults" })).toBeVisible();
    await waitFor(() => expect(screen.getByRole("button", { name: "Close vault navigation" })).toHaveFocus());
    fireEvent.keyDown(window, { key: "\\", metaKey: true });
    await waitFor(() => expect(screen.getByRole("button", { name: "Open vault navigation" })).toHaveFocus());
    expect(localStorage.getItem("akb.treeVisible")).toBeNull();
  });
  it("keeps a main-content Collection destination out of the mobile drawer", async () => {
    vi.mocked(window.matchMedia).mockImplementation(query => ({ matches: false, media: query, onchange: null, addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn() }));
    mount("/vault/demo");
    await userEvent.click(screen.getByRole("button", { name: "Open vault navigation" }));
    expect(screen.getByRole("navigation", { name: "Content explorer" })).toBeVisible();
    await userEvent.click(screen.getByRole("link", { name: "Open collection in main" }));
    await waitFor(() => expect(screen.queryByRole("navigation", { name: "Content explorer" })).not.toBeInTheDocument());
  });
  it.each(["focused", "hovered"])("dismisses the mobile drawer with one Escape when a %s control has a tooltip", async mode => {
    vi.mocked(window.matchMedia).mockImplementation((query) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }));
    mount("/vault/demo/members");
    const user = userEvent.setup();
    const mobile = screen.getByRole("button", {
      name: "Open vault navigation",
    });
    expect(mobile).toHaveAttribute("id", "vault-navigation-trigger");
    await user.click(mobile);
    expect(
      screen.getByRole("navigation", { name: "Content explorer" }),
    ).toBeVisible();
    await waitFor(() => expect(screen.getByRole("button", { name: "Close vault navigation" })).toHaveFocus());
    screen.getByRole("button", { name: "Switch vault: demo" }).focus();
    if (mode === "hovered") {
      screen.getByRole("button", { name: "Collection disclosure" }).focus();
      await user.hover(screen.getByRole("button", { name: "Switch vault: demo" }));
    }
    expect(await screen.findByRole("tooltip", { name: "demo" })).toBeVisible();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(mobile).toHaveFocus());
    expect(
      screen.queryByRole("navigation", { name: "Content explorer" }),
    ).not.toBeInTheDocument();
  });
  it("respects Escape cancellation by an interactive drawer control", async () => {
    vi.mocked(window.matchMedia).mockImplementation(query => ({ matches: false, media: query, onchange: null, addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn() }));
    mount("/vault/demo/members");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Open vault navigation" }));
    const control = screen.getByRole("button", { name: "Collection disclosure" });
    await waitFor(() => expect(screen.getByRole("button", { name: "Close vault navigation" })).toHaveFocus());
    control.focus();
    control.addEventListener("keydown", event => event.preventDefault(), { once: true });
    await user.keyboard("{Escape}");
    expect(screen.getByRole("navigation", { name: "Content explorer" })).toBeVisible();
    expect(control).toHaveFocus();
  });
  it("retains the mobile drawer through picker focus restoration and dismisses after an intentional departure", async () => {
    vi.mocked(window.matchMedia).mockImplementation((query) => ({
      matches: false, media: query, onchange: null,
      addEventListener: vi.fn(), removeEventListener: vi.fn(),
      addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
    }));
    mount("/vault/demo/members");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Open vault navigation" }));
    const picker = screen.getByRole("button", { name: "Switch vault: demo" });
    await waitFor(() => expect(screen.getByRole("button", { name: "Close vault navigation" })).toHaveFocus());
    await user.click(picker);
    expect(picker).toHaveAttribute("aria-expanded", "true");
    const outside = screen.getByRole("link", { name: "Open overview" });
    outside.focus();
    await new Promise<void>(resolve => requestAnimationFrame(() => resolve()));
    expect(screen.getByRole("navigation", { name: "Content explorer" })).toBeVisible();
    await user.click(picker);
    expect(picker).toHaveAttribute("aria-expanded", "false");
    outside.focus();
    await waitFor(() => expect(screen.queryByRole("navigation", { name: "Content explorer" })).not.toBeInTheDocument());
    expect(outside).toHaveFocus();
  });
});
