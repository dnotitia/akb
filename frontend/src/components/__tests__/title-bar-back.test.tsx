import { render, screen, cleanup, within } from "@testing-library/react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { TitleBar, VaultActions } from "../title-bar";

afterEach(cleanup);

function renderTitleBarAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="*" element={<TitleBar crumbs={[{ label: "X" }]} />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("TitleBar back button", () => {
  it("renders with aria-label 'Go back'", () => {
    vi.spyOn(window.history, "length", "get").mockReturnValue(2);
    renderTitleBarAt("/vault/foo");
    expect(screen.getByRole("button", { name: /go back/i })).toBeInTheDocument();
    vi.restoreAllMocks();
  });

  it("is disabled on the home route", () => {
    vi.spyOn(window.history, "length", "get").mockReturnValue(5);
    renderTitleBarAt("/");
    expect(screen.getByRole("button", { name: /go back/i })).toBeDisabled();
    vi.restoreAllMocks();
  });

  it("is disabled when history.length === 1", () => {
    vi.spyOn(window.history, "length", "get").mockReturnValue(1);
    renderTitleBarAt("/vault/foo");
    expect(screen.getByRole("button", { name: /go back/i })).toBeDisabled();
    vi.restoreAllMocks();
  });

  it("is enabled when there is prior history AND we are NOT on /", () => {
    vi.spyOn(window.history, "length", "get").mockReturnValue(3);
    renderTitleBarAt("/vault/foo");
    expect(screen.getByRole("button", { name: /go back/i })).not.toBeDisabled();
    vi.restoreAllMocks();
  });
});

describe("VaultActions", () => {
  it("groups Members beside Settings after the governance divider", () => {
    const { container } = render(
      <MemoryRouter>
        <VaultActions vault="demo" page="members" />
      </MemoryRouter>,
    );

    const nav = screen.getByRole("navigation", { name: "Vault sections" });
    expect(within(nav).getAllByRole("link").map((link) => link.textContent?.trim())).toEqual([
      "Overview",
      "Search",
      "Graph",
      "Publish",
      "Members",
      "Settings",
    ]);
    expect(within(nav).getByRole("link", { name: "Members" })).toHaveAttribute(
      "aria-current",
      "page",
    );

    const divider = container.querySelector('[aria-hidden="true"].w-px');
    expect(divider?.nextElementSibling).toHaveTextContent("Members");
  });
});
