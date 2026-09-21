import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { VaultSectionNavigation } from "../vault-navigation-menu";

function renderNavigation(route: string, vault = "team") {
  return render(<MemoryRouter initialEntries={[route]}>
    <VaultSectionNavigation vault={vault} />
  </MemoryRouter>);
}

describe("VaultSectionNavigation", () => {
  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });
  it("starts with Overview and keeps content destinations before management", () => {
    renderNavigation("/vault/team");
    const navigation = screen.getByRole("navigation", { name: "Vault sections" });
    expect(within(navigation).getAllByRole("link").map(link => link.textContent)).toEqual([
      "Overview", "Search", "Graph", "Public links", "Members", "Settings",
    ]);
    expect(screen.getAllByRole("link").map(link => link.getAttribute("href"))).toEqual([
      "/vault/team", "/vault/team/search", "/vault/team/graph", "/vault/team/publications",
      "/vault/team/members", "/vault/team/settings",
    ]);
    expect(screen.getByRole("link", { name: "Overview" })).toHaveAttribute("aria-current", "page");
    expect(screen.queryByRole("tablist")).not.toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Search" })).toHaveTextContent(/^Search$/);
    for (const link of screen.getAllByRole("link")) expect(link.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  });

  it.each([
    ["", "Overview"], ["/search", "Search"], ["/graph", "Graph"],
    ["/publications", "Public links"], ["/members", "Members"], ["/settings", "Settings"],
  ])("marks only the exact working-page destination current on %s", (suffix, label) => {
    renderNavigation(`/vault/team${suffix}?filter=anything`);
    expect(screen.getByRole("link", { name: label })).toHaveAttribute("aria-current", "page");
    expect(screen.getAllByRole("link").filter(link => link.getAttribute("aria-current") === "page")).toHaveLength(1);
  });

  it("does not mark Overview current on the separate Activity route", () => {
    renderNavigation("/vault/team/activity");
    expect(screen.getAllByRole("link").every(link => !link.hasAttribute("aria-current"))).toBe(true);
  });

  it.each(["doc/note", "doc/note?view=raw", "doc/note?view=edit", "file/file-id", "table/records"])("keeps Vault links without a false current section on %s", suffix => {
    renderNavigation(`/vault/team/${suffix}`);
    expect(screen.getByRole("navigation", { name: "Vault sections" })).toBeInTheDocument();
    expect(screen.getAllByRole("link")).toHaveLength(6);
    expect(screen.getAllByRole("link").every(link => !link.hasAttribute("aria-current"))).toBe(true);
  });

  it.each(["doc/new", "skill"])("omits the whole navigation row on %s", suffix => {
    renderNavigation(`/vault/team/${suffix}`);
    expect(screen.queryByRole("navigation", { name: "Vault sections" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it.each(["/vault", "/search", "/vault/other/members", "/vault/team/unknown"])("does not render a stale Vault's navigation on %s", route => {
    renderNavigation(route);
    expect(screen.queryByRole("navigation", { name: "Vault sections" })).not.toBeInTheDocument();
  });

  it("encodes Vault names and supports keyboard navigation in visible order", async () => {
    const user = userEvent.setup();
    renderNavigation("/vault/%ED%8C%80%20Vault", "팀 Vault");
    await user.tab();
    const overview = screen.getByRole("link", { name: "Overview" });
    expect(overview).toHaveFocus();
    expect(overview).toHaveAttribute("href", "/vault/%ED%8C%80%20Vault");
    await user.tab();
    const search = screen.getByRole("link", { name: "Search" });
    expect(search).toHaveFocus();
    expect(search).toHaveAttribute("href", "/vault/%ED%8C%80%20Vault/search");
    await user.keyboard("{Enter}");
    expect(search).toHaveAttribute("aria-current", "page");
    await user.tab();
    expect(screen.getByRole("link", { name: "Graph" })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("link", { name: "Public links" })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("link", { name: "Members" })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("link", { name: "Settings" })).toHaveFocus();
  });

  it("uses measured overflow, preserves the active destination, and restores focus on Escape/resize", async () => {
    let available = 320;
    let resize: () => void = () => {};
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const width = this.dataset.slot === "vault-section-navigation" ? available : this.textContent === "More" ? 72 : 100;
      return { width, height: 40, x: 0, y: 0, top: 0, bottom: 40, left: 0, right: width, toJSON: () => ({}) };
    });
    vi.stubGlobal("ResizeObserver", class {
      callback: () => void;
      constructor(callback: () => void) { this.callback = callback; }
      observe(element: HTMLElement) { if (element.dataset.slot === "vault-section-navigation") resize = this.callback; }
      unobserve() {}
      disconnect() {}
    });
    const user = userEvent.setup();
    renderNavigation("/vault/team/settings");
    expect(screen.getAllByRole("link").map(link => link.textContent)).toEqual(["Overview", "Settings"]);
    expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("aria-current", "page");
    const more = screen.getByRole("button", { name: "More vault pages" });
    more.focus();
    await user.keyboard("{Enter}");
    const menu = screen.getByRole("menu", { name: "More vault pages" });
    expect(within(menu).getAllByRole("menuitem").map(item => item.textContent)).toEqual(["Search", "Graph", "Public links", "Members"]);
    expect(within(menu).getByRole("menuitem", { name: "Search" })).toHaveAttribute("href", "/vault/team/search");
    await user.keyboard("{Escape}");
    expect(more).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(screen.getByRole("menuitem", { name: "Search" })).toHaveFocus();
    await act(async () => { available = 900; resize(); });
    expect(screen.queryByRole("button", { name: "More vault pages" })).not.toBeInTheDocument();
    expect(screen.getAllByRole("link")).toHaveLength(6);
    expect(screen.getByRole("link", { name: "Search" })).toHaveFocus();
    screen.getByRole("link", { name: "Graph" }).focus();
    await act(async () => { available = 320; resize(); });
    expect(screen.getByRole("button", { name: "More vault pages" })).toHaveFocus();
    expect(screen.getByRole("button", { name: "More vault pages" })).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    await user.keyboard("{Enter}");
    await user.click(screen.getByRole("menuitem", { name: "Graph" }));
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Graph" })).toHaveAttribute("aria-current", "page");
  });
});
