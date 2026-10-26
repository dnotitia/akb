import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ResourceNavigationProvider, useResourceNavigationGuard } from "@/contexts/resource-navigation-context";
import { AppPageLocation } from "../app-page-location";

describe("App page location", () => {
  it.each([
    ["/", "Home"], ["/search?q=test", "Search"],
    ["/settings?tab=tokens", "Agent connections"], ["/vault", "Vaults"],
    ["/settings", "Profile"], ["/settings?tab=preferences", "Appearance"],
    ["/settings?tab=notifications", "Watched documents"],
    ["/settings?tab=admin", "Profile"], ["/settings?tab=unknown", "Profile"],
    ["/vault/new", "New vault"], ["/notifications", "Notifications"],
    ["/unknown", "Page not found"],
  ])("identifies %s without an unnecessary hierarchy", (path, label) => {
    render(<MemoryRouter initialEntries={[path]}><AppPageLocation /></MemoryRouter>);
    const location = screen.getByRole("navigation", { name: "Current page" });
    expect(within(location).getByText(label)).toHaveAttribute("aria-current", "page");
    expect(location.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
    expect(within(location).queryByRole("link")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Pages in/ })).not.toBeInTheDocument();
  });

  it.each([
    ["", "Overview"], ["/members", "Members"], ["/settings", "Settings"],
    ["/publications", "Public links"], ["/graph", "Graph"], ["/search", "Search"],
    ["/activity", "Activity"], ["/doc/new", "New document"],
    ["/doc/d-12345678", "Document"], ["/table/data", "Table"], ["/file/uuid", "File"],
  ])("keeps named Vault section %s in the top location trail", (tail, label) => {
    render(<MemoryRouter initialEntries={[`/vault/%ED%8C%80%20Vault${tail}`]}><AppPageLocation /></MemoryRouter>);
    const location = screen.getByRole("navigation", { name: "Current page" });
    const backToVault = ["/graph", "/publications", "/members", "/settings"].includes(tail);
    const vault = within(location).getByRole("link", { name: backToVault ? "Back to 팀 Vault" : "팀 Vault" });
    expect(vault).toHaveAttribute("href", "/vault/%ED%8C%80%20Vault");
    expect(vault.querySelector(backToVault ? ".lucide-arrow-left" : ".lucide-box")).toHaveAttribute("aria-hidden", "true");
    const current = tail ? within(location).getByText(label) : vault;
    expect(current).toHaveAttribute("aria-current", "page");
    if (!tail) expect(location).toHaveTextContent(/^팀 Vault$/);
    const resourceGlyph = ({ Document: ".lucide-file-text", File: ".lucide-file", Table: ".lucide-table-2" } as Record<string, string>)[label];
    expect(location.querySelectorAll("svg")).toHaveLength(resourceGlyph ? 2 : 1);
    expect(within(location).queryByRole("button", { name: /Pages in/ })).not.toBeInTheDocument();
    expect(current.closest("button")).toBeNull();
    if (resourceGlyph) {
      expect(current.compareDocumentPosition(location.querySelector(resourceGlyph)!)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    }
    expect(location.querySelector("ol")).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Vault sections" })).not.toBeInTheDocument();
  });

  it.each([true, false])("keeps the Vault name a guarded Overview link (allowed=%s)", async allowed => {
    const user = userEvent.setup();
    const guard = vi.fn(() => allowed);
    function GuardedLocation() {
      useResourceNavigationGuard(guard);
      return <AppPageLocation />;
    }
    render(<MemoryRouter initialEntries={["/vault/team/settings"]}><ResourceNavigationProvider><GuardedLocation /></ResourceNavigationProvider></MemoryRouter>);
    await user.click(screen.getByRole("link", { name: "Back to team" }));
    expect(guard).toHaveBeenNthCalledWith(1, "/vault/team");
    expect(guard).toHaveBeenCalledTimes(allowed ? 2 : 1);
    const location = within(screen.getByRole("navigation", { name: "Current page" }));
    expect(allowed ? location.getByRole("link", { name: "team" }) : location.getByText("Settings")).toHaveAttribute("aria-current", "page");
    expect(screen.queryByRole("button", { name: /Pages in/ })).not.toBeInTheDocument();
  });

  it("leaves modified Vault-link clicks to the browser", () => {
    const guard = vi.fn(() => false);
    function GuardedLocation() {
      useResourceNavigationGuard(guard);
      return <AppPageLocation />;
    }
    render(<MemoryRouter initialEntries={["/vault/team/settings"]}><ResourceNavigationProvider><GuardedLocation /></ResourceNavigationProvider></MemoryRouter>);
    const vault = screen.getByRole("link", { name: "Back to team" });
    expect(vault).toHaveAttribute("href", "/vault/team");
    fireEvent.click(vault, { ctrlKey: true });
    expect(guard).not.toHaveBeenCalled();
    expect(screen.getByText("Settings")).toHaveAttribute("aria-current", "page");
  });
});
