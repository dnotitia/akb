import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";

import { VaultShell } from "@/components/vault-shell";

vi.mock("@/components/vault-explorer", () => ({
  VaultExplorer: () => <div>Collection tree content</div>,
}));

vi.mock("@/components/vault-rail", () => ({
  VaultRail: () => <nav aria-label="Vaults">Vault rail</nav>,
}));

vi.mock("@/components/title-bar", () => ({
  TitleBar: ({ left, right }: { left?: React.ReactNode; right?: React.ReactNode }) => (
    <header>
      {left}
      {right}
    </header>
  ),
  VaultActions: () => null,
}));

vi.mock("@/components/vault-create-dialog", () => ({
  VaultCreateDialog: () => null,
}));

vi.mock("@/components/document-create-dialog", () => ({
  DocumentCreateDialog: () => null,
}));

vi.mock("@/hooks/use-column-resize", () => ({
  useColumnResize: ({ default: width }: { default: number }) => ({
    width,
    setWidth: vi.fn(),
    reset: vi.fn(),
    handlers: {},
  }),
}));

afterEach(cleanup);

beforeEach(() => {
  window.localStorage.clear();
  window.localStorage.setItem("akb.treeVisible", "1");
});

function renderShell() {
  return render(
    <MemoryRouter initialEntries={["/vault/demo/search"]}>
      <Routes>
        <Route path="/vault/:name" element={<VaultShell />}>
          <Route
            index
            element={
              <div>
                Overview page
                <Link to="/vault/demo/search">Open search</Link>
              </div>
            }
          />
          <Route
            path="search"
            element={
              <div>
                Search page
                <Link to="/vault/demo">Open overview</Link>
              </div>
            }
          />
          <Route path="members" element={<div>Members page</div>} />
          <Route path="publications" element={<div>Publications page</div>} />
          <Route path="settings" element={<div>Settings page</div>} />
          <Route path="activity" element={<div>Activity page</div>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

describe("VaultShell search collection tree", () => {
  it("starts Search collapsed without changing the persisted tree preference", async () => {
    const user = userEvent.setup();
    renderShell();

    expect(screen.queryByText("Collection tree content")).toBeNull();
    expect(screen.getByRole("button", { name: "Show collection tree" })).toBeTruthy();
    expect(window.localStorage.getItem("akb.treeVisible")).toBe("1");

    await user.click(screen.getByRole("button", { name: "Show collection tree" }));
    expect(screen.getByText("Collection tree content")).toBeTruthy();
    expect(window.localStorage.getItem("akb.treeVisible")).toBe("1");

    await user.click(screen.getByRole("link", { name: "Open overview" }));
    expect(screen.getByText("Collection tree content")).toBeTruthy();

    await user.click(screen.getByRole("link", { name: "Open search" }));
    expect(screen.queryByText("Collection tree content")).toBeNull();
    expect(screen.getByRole("button", { name: "Show collection tree" })).toBeTruthy();
  });

  it.each(["members", "publications", "settings", "activity"])(
    "starts the %s tool view with Collections folded",
    async (route) => {
      render(
        <MemoryRouter initialEntries={[`/vault/demo/${route}`]}>
          <Routes>
            <Route path="/vault/:name" element={<VaultShell />}>
              <Route path={route} element={<div>{route} page</div>} />
            </Route>
          </Routes>
        </MemoryRouter>,
      );

      expect(screen.queryByText("Collection tree content")).toBeNull();
      expect(screen.getByRole("button", { name: "Show collection tree" })).toBeTruthy();
      expect(window.localStorage.getItem("akb.treeVisible")).toBe("1");

      const routeViewport = document.querySelector('[data-slot="vault-route-viewport"]');
      expect(routeViewport).not.toBeNull();
      if (route === "members" || route === "settings") {
        expect(routeViewport).toHaveClass("xl:overflow-hidden");
      } else {
        expect(routeViewport).not.toHaveClass("xl:overflow-hidden");
      }

      if (route === "activity" || route === "publications") {
        expect(routeViewport?.firstElementChild).toHaveClass("px-3", "xl:px-5");
      }
    },
  );
});
