import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { ResourceNavigationProvider, useResourceNavigationGuard } from "@/contexts/resource-navigation-context";
import { VaultOverviewNavigation, VaultOverviewNavigationFocus } from "../vault-overview-navigation";

function renderNavigation(guard?: () => boolean) {
  function Workspace() {
    useResourceNavigationGuard(guard ?? null);
    return <><VaultOverviewNavigationFocus /><main id="main" tabIndex={-1}><Routes>
      <Route path="/vault/team" element={<VaultOverviewNavigation vault="team" />} />
      <Route path="/vault/team/:page" element={<h1>Destination</h1>} />
    </Routes></main></>;
  }
  return render(<MemoryRouter initialEntries={["/vault/team"]}><ResourceNavigationProvider><Workspace /></ResourceNavigationProvider></MemoryRouter>);
}

describe("Overview page navigation", () => {
  it("focuses main for an accepted Collection query destination without opening another surface", async () => {
    render(<MemoryRouter initialEntries={["/vault/team"]}><VaultOverviewNavigationFocus /><main id="main" tabIndex={-1}>
      <Link to="/vault/team?collection=guides" state={{ vaultPageNavigation: "/vault/team?collection=guides", collectionView: "main" }}>Open guides</Link>
    </main></MemoryRouter>);
    await userEvent.click(screen.getByRole("link", { name: "Open guides" }));
    await waitFor(() => expect(screen.getByRole("main")).toHaveFocus());
  });
  it("offers four direct destinations without a redundant Overview or Search link", () => {
    renderNavigation();
    const nav = screen.getByRole("navigation", { name: "Vault pages" });
    const links = within(nav).getAllByRole("link");
    expect(links.map(link => [link.textContent, link.getAttribute("href")])).toEqual([
      ["Graph", "/vault/team/graph"], ["Public links", "/vault/team/publications"],
      ["Members", "/vault/team/members"], ["Settings", "/vault/team/settings"],
    ]);
    expect(within(nav).queryByRole("button")).not.toBeInTheDocument();
    expect(nav.querySelector("[aria-current]")).toBeNull();
    for (const link of links) expect(link.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  });

  it("encodes the Vault boundary in every native link", () => {
    render(<MemoryRouter><VaultOverviewNavigation vault="팀 /#? Vault" /></MemoryRouter>);
    for (const link of screen.getAllByRole("link")) expect(link.getAttribute("href")).toMatch(/^\/vault\/%ED%8C%80%20%2F%23%3F%20Vault\//);
  });

  it("keeps the member count on the existing native Members destination", () => {
    render(<MemoryRouter><VaultOverviewNavigation vault="team" memberCount={5} /></MemoryRouter>);
    const members = screen.getByRole("link", { name: "Members" });
    expect(members).toHaveAttribute("href", "/vault/team/members");
    expect(members).toHaveTextContent("Members5");
    expect(members).toHaveAccessibleDescription("5 members");
    expect(within(members).getByText("5")).toHaveAttribute("aria-hidden", "true");
    expect(screen.getAllByRole("link")).toHaveLength(4);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it.each([undefined, -1, Number.NaN, Number.POSITIVE_INFINITY])("omits an unavailable member count instead of inventing zero: %s", memberCount => {
    render(<MemoryRouter><VaultOverviewNavigation vault="team" memberCount={memberCount} /></MemoryRouter>);
    const members = screen.getByRole("link", { name: "Members" });
    expect(members).toHaveTextContent(/^Members$/);
    expect(members).not.toHaveAccessibleDescription();
  });

  it("retains an authoritative zero member count", () => {
    render(<MemoryRouter><VaultOverviewNavigation vault="team" memberCount={0} /></MemoryRouter>);
    expect(screen.getByRole("link", { name: "Members" })).toHaveAccessibleDescription("0 members");
  });

  it("uses normal Tab order and focuses the destination when an accepted link unmounts", async () => {
    const user = userEvent.setup();
    renderNavigation();
    for (const name of ["Graph", "Public links", "Members", "Settings"]) {
      await user.tab();
      expect(screen.getByRole("link", { name })).toHaveFocus();
    }
    await user.keyboard("{Enter}");
    expect(screen.getByRole("heading", { name: "Destination" })).toBeVisible();
    expect(screen.queryByRole("navigation", { name: "Vault pages" })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("main")).toHaveFocus());
  });

  it("retains the source and focus when a workspace blocks departure", async () => {
    const user = userEvent.setup();
    const guard = vi.fn(() => false);
    renderNavigation(guard);
    const members = screen.getByRole("link", { name: "Members" });
    members.focus();
    await user.keyboard("{Enter}");
    expect(guard).toHaveBeenCalledExactlyOnceWith("/vault/team/members");
    expect(screen.queryByRole("heading", { name: "Destination" })).not.toBeInTheDocument();
    expect(members).toHaveFocus();
  });

  it.each([{ ctrlKey: true }, { metaKey: true }, { shiftKey: true }, { button: 1 }])("leaves modified/native clicks alone: %j", modifiers => {
    const guard = vi.fn(() => false);
    renderNavigation(guard);
    fireEvent.click(screen.getByRole("link", { name: "Members" }), modifiers);
    expect(guard).not.toHaveBeenCalled();
    expect(screen.getByRole("navigation", { name: "Vault pages" })).toBeVisible();
  });
});
