import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Outlet, useLocation } from "react-router-dom";

vi.mock("@/components/layout", () => ({
  Layout: () => (
    <div>
      <span>App shell</span>
      <Outlet />
    </div>
  ),
}));

vi.mock("@/components/vault-shell", () => ({
  VaultShell: () => <Outlet />,
}));

vi.mock("@/pages/search", () => ({
  default: () => (
    <div>
      <span>Preserved search results</span>
      <a id="search-result-1" href="/vault/alpha/doc/notes%2Fpostgres.md">
        PostgreSQL tuning
      </a>
    </div>
  ),
}));

vi.mock("@/pages/document", () => ({
  default: ({ presentation = "page" }: { presentation?: string }) => (
    <div>{presentation === "preview" ? "Preview document body" : "Full document page"}</div>
  ),
}));

import { AppRoutes } from "@/app-routes";

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="route-location">{location.pathname + location.search}</output>;
}

afterEach(cleanup);

describe("AppRoutes document preview", () => {
  it("renders a search-backed modal route and returns to the exact query on close", async () => {
    const user = userEvent.setup();
    const backgroundLocation = {
      pathname: "/search",
      search: "?q=postgres&mode=literal",
      hash: "",
      state: null,
      key: "search-ledger",
    };

    render(
      <MemoryRouter
        initialEntries={[
          backgroundLocation,
          {
            pathname: "/vault/alpha/doc/notes%2Fpostgres.md",
            state: {
              documentPreview: true,
              backgroundLocation,
              returnFocusId: "search-result-1",
            },
          },
        ]}
        initialIndex={1}
      >
        <AppRoutes />
        <LocationProbe />
      </MemoryRouter>,
    );

    expect(screen.getByText("Preserved search results")).toBeInTheDocument();
    expect(screen.getByText("Preview document body")).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Document preview" })).toBeInTheDocument();
    expect(screen.getByTestId("route-location")).toHaveTextContent(
      "/vault/alpha/doc/notes%2Fpostgres.md",
    );

    await user.click(screen.getByRole("button", { name: "Close dialog" }));

    expect(screen.queryByRole("dialog", { name: "Document preview" })).toBeNull();
    expect(screen.getByText("Preserved search results")).toBeInTheDocument();
    expect(screen.getByTestId("route-location")).toHaveTextContent(
      "/search?q=postgres&mode=literal",
    );
    await waitFor(() =>
      expect(
        screen.getByRole("link", { name: "PostgreSQL tuning" }),
      ).toHaveFocus(),
    );
  });

  it("returns to the preserved search route when the backdrop is clicked", async () => {
    const user = userEvent.setup();
    const backgroundLocation = {
      pathname: "/search",
      search: "?q=postgres&mode=literal",
      hash: "",
      state: null,
      key: "search-ledger",
    };

    render(
      <MemoryRouter
        initialEntries={[
          backgroundLocation,
          {
            pathname: "/vault/alpha/doc/notes%2Fpostgres.md",
            state: {
              documentPreview: true,
              backgroundLocation,
              returnFocusId: "search-result-1",
            },
          },
        ]}
        initialIndex={1}
      >
        <AppRoutes />
        <LocationProbe />
      </MemoryRouter>,
    );

    const overlay = document.querySelector<HTMLElement>(
      '[data-slot="dialog-overlay"]',
    );
    expect(overlay).not.toBeNull();
    await user.click(overlay!);

    expect(screen.queryByRole("dialog", { name: "Document preview" })).toBeNull();
    expect(screen.getByTestId("route-location")).toHaveTextContent(
      "/search?q=postgres&mode=literal",
    );
  });
});
