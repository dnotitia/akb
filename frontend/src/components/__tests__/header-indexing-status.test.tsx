import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { HeaderIndexingStatus } from "../header-indexing-status";

afterEach(() => cleanup());

describe("HeaderIndexingStatus", () => {
  it("keeps a quiet reserved slot after every accessible Vault catches up", () => {
    render(
      <HeaderIndexingStatus
        status={{
          vaultCount: 4,
          checkedVaultCount: 4,
          pending: 0,
          abandoned: 0,
          indexed: 120,
          incomplete: false,
        }}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent(
      "Knowledge indexing is caught up across 4 accessible Vaults",
    );
    expect(screen.queryByText("120 indexed")).toBeNull();
  });

  it("shows an active lower-bound count without wrapping the badge", () => {
    render(
      <HeaderIndexingStatus
        status={{
          vaultCount: 4,
          checkedVaultCount: 3,
          pending: 12,
          abandoned: 0,
          indexed: 80,
          incomplete: true,
        }}
      />,
    );

    expect(screen.getByText("12+ indexing")).toBeTruthy();
    expect(screen.getByRole("status")).toHaveTextContent(
      "12+ items are indexing across accessible Vaults",
    );
  });

  it("prioritizes failed work over active work in the compact slot", () => {
    render(
      <HeaderIndexingStatus
        status={{
          vaultCount: 2,
          checkedVaultCount: 2,
          pending: 8,
          abandoned: 3,
          indexed: 40,
          incomplete: false,
        }}
      />,
    );

    expect(screen.getByText("3 need attention")).toBeTruthy();
    expect(screen.queryByText("8 indexing")).toBeNull();
  });
});
