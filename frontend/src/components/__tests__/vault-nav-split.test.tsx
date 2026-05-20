import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { VaultNav } from "../vault-nav";

vi.mock("@/hooks/use-vaults", () => ({
  useVaults: () => ({
    vaults: [
      { id: "v1", name: "alpha" },
      { id: "v2", name: "beta" },
    ],
    loading: false,
    refetch: vi.fn(),
  }),
}));

describe("VaultNav active row split", () => {
  it("active row exposes an overview Link AND a separate chevron toggle", () => {
    const onToggle = vi.fn();
    render(
      <MemoryRouter>
        <VaultNav current="alpha" onCurrentVaultClick={onToggle} treeOpen={true} />
      </MemoryRouter>,
    );
    // Overview link
    const overviewLink = screen.getByRole("link", { name: /alpha/i });
    expect(overviewLink.getAttribute("href")).toBe("/vault/alpha");
    // Chevron toggle button
    const toggle = screen.getByRole("button", { name: /collapse vault tree/i });
    fireEvent.click(toggle);
    expect(onToggle).toHaveBeenCalledTimes(1);
  });

  it("inactive row remains a single Link", () => {
    render(
      <MemoryRouter>
        <VaultNav current="alpha" onCurrentVaultClick={vi.fn()} treeOpen={false} />
      </MemoryRouter>,
    );
    const betaLink = screen.getByRole("link", { name: /beta/i });
    expect(betaLink.getAttribute("href")).toBe("/vault/beta");
    // beta is inactive → no chevron toggle button for it
    const togglesForBeta = screen
      .queryAllByRole("button")
      .filter((b) => /beta/i.test(b.getAttribute("aria-label") || ""));
    expect(togglesForBeta).toEqual([]);
  });

  it("chevron rotates with treeOpen state (aria-expanded)", () => {
    const { rerender } = render(
      <MemoryRouter>
        <VaultNav current="alpha" onCurrentVaultClick={vi.fn()} treeOpen={true} />
      </MemoryRouter>,
    );
    expect(screen.getByRole("button", { name: /collapse vault tree/i }).getAttribute("aria-expanded")).toBe("true");
    rerender(
      <MemoryRouter>
        <VaultNav current="alpha" onCurrentVaultClick={vi.fn()} treeOpen={false} />
      </MemoryRouter>,
    );
    expect(screen.getByRole("button", { name: /expand vault tree/i }).getAttribute("aria-expanded")).toBe("false");
  });
});
