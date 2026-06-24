// RTL coverage for the multi-vault search scope (VaultScopePicker + search.tsx).
//
// Why this file: the global /search page lets you scope a query to one OR MORE
// vaults via `?v=a,b,c` (the multi-select scope picker). These cases lock the
// contract:
//   - a comma-joined `?v=` drives searchDocs with a string[] of those vaults
//   - the scope trigger reads "All vaults (N)" with no selection (the default)
//   - a selection renders the count label + a removable chip per vault
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import SearchPage from "../search";
import { searchDocs, grepDocs, listVaults } from "@/lib/api";

vi.mock("@/lib/api", () => ({
  searchDocs: vi.fn(),
  grepDocs: vi.fn(),
  listVaults: vi.fn(),
}));

const mockedSearch = vi.mocked(searchDocs);
const mockedListVaults = vi.mocked(listVaults);

const EMPTY = { query: "", total: 0, returned: 0, total_matches: 0, results: [] };

afterEach(cleanup);
beforeEach(() => {
  mockedSearch.mockReset().mockResolvedValue(EMPTY);
  vi.mocked(grepDocs).mockReset();
  mockedListVaults.mockReset().mockResolvedValue({
    vaults: [{ name: "alpha" }, { name: "beta" }, { name: "gamma" }],
  });
});

function renderAt(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <SearchPage />
    </MemoryRouter>,
  );
}

describe("SearchPage · multi-vault scope", () => {
  it("passes the comma-joined ?v= as a string[] to searchDocs", async () => {
    renderAt("/search?q=postgres&v=alpha,beta");
    await waitFor(() =>
      expect(mockedSearch).toHaveBeenCalledWith("postgres", ["alpha", "beta"], 25),
    );
  });

  it("shows 'All vaults (N)' when nothing is scoped", async () => {
    renderAt("/search");
    expect(await screen.findByText("All vaults (3)")).toBeTruthy();
  });

  it("renders the count label + a removable chip per selected vault", async () => {
    renderAt("/search?q=x&v=alpha,beta");
    // trigger reflects the selection count
    expect(await screen.findByText("2 vaults")).toBeTruthy();
    // each selection is a removable chip
    expect(
      screen.getByRole("button", { name: "Remove alpha from search scope" }),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Remove beta from search scope" }),
    ).toBeTruthy();
  });
});
