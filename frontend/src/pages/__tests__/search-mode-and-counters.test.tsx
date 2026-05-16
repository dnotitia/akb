// RTL coverage for SearchPage.
//
// Why this file: search.tsx wires the URL (?q / ?mode / ?v) into two
// different API calls (searchDocs vs grepDocs) and renders two
// different shapes — and we shipped the returned/total_matches change
// in PR #39 without any UI-level test for it. These cases catch:
//   - mode toggle issues a fresh API call and the right shape lands
//   - literal results render the `[N docs · M matches]` header
//     (regression guard for total_matches drift)
//   - an empty query never fires a search
//
// Module-level vi.mock of @/lib/api keeps the test fast (no MSW spin-up
// cost) and lets us assert call args directly. The MSW-level contract
// test already lives in lib/__tests__/api-search-contract.test.ts.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

import SearchPage from "../search";
import { searchDocs, grepDocs, listVaults } from "@/lib/api";

vi.mock("@/lib/api", () => ({
  searchDocs: vi.fn(),
  grepDocs: vi.fn(),
  listVaults: vi.fn(),
}));

const mockedSearch = vi.mocked(searchDocs);
const mockedGrep = vi.mocked(grepDocs);
const mockedListVaults = vi.mocked(listVaults);

afterEach(cleanup);
beforeEach(() => {
  mockedSearch.mockReset();
  mockedGrep.mockReset();
  mockedListVaults.mockReset();
  mockedListVaults.mockResolvedValue({ vaults: [] });
});

function renderAt(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <SearchPage />
    </MemoryRouter>,
  );
}

describe("SearchPage · semantic (dense) mode", () => {
  it("calls searchDocs on initial render with ?q and renders a hit", async () => {
    mockedSearch.mockResolvedValue({
      query: "postgres",
      total: 1,
      returned: 1,
      total_matches: 1,
      results: [
        {
          source_type: "document",
          uri: "akb://akb/document/abc",
          vault: "akb",
          path: "notes/postgres.md",
          title: "PostgreSQL tuning",
          score: 0.91,
        },
      ],
    });
    renderAt("/search?q=postgres");
    await waitFor(() => expect(mockedSearch).toHaveBeenCalledWith("postgres", undefined));
    expect(await screen.findByText("PostgreSQL tuning")).toBeTruthy();
    expect(mockedGrep).not.toHaveBeenCalled();
  });

  it("does not search when ?q is missing", async () => {
    renderAt("/search");
    // Give the effect a tick to misfire.
    await new Promise((r) => setTimeout(r, 20));
    expect(mockedSearch).not.toHaveBeenCalled();
    expect(mockedGrep).not.toHaveBeenCalled();
  });
});

describe("SearchPage · literal mode + counter header (PR #39 regression)", () => {
  it("renders [N docs · M matches] from grepDocs response", async () => {
    mockedGrep.mockResolvedValue({
      pattern: "TODO",
      regex: false,
      total_docs: 2,
      total_matches: 7,
      results: [
        { uri: "akb://akb/document/a", vault: "akb", path: "a.md", title: "A", matches: [] },
        { uri: "akb://akb/document/b", vault: "akb", path: "b.md", title: "B", matches: [] },
      ],
    });
    renderAt("/search?q=TODO&mode=literal");
    expect(await screen.findByText(/2 docs · 7 matches/)).toBeTruthy();
    expect(mockedSearch).not.toHaveBeenCalled();
  });
});

describe("SearchPage · mode toggle re-issues the correct call", () => {
  it("switches from semantic to literal on button click", async () => {
    mockedSearch.mockResolvedValue({
      query: "k8s",
      total: 0,
      returned: 0,
      total_matches: 0,
      results: [],
    });
    mockedGrep.mockResolvedValue({
      pattern: "k8s",
      regex: false,
      total_docs: 0,
      total_matches: 0,
      results: [],
    });
    renderAt("/search?q=k8s");
    await waitFor(() => expect(mockedSearch).toHaveBeenCalledTimes(1));
    const u = userEvent.setup();
    // Two buttons render with text "LITERAL": the mode toggle (with
    // aria-pressed) and the short-query hint link below. The toggle
    // is the only one with aria-pressed set.
    const toggle = screen
      .getAllByRole("button", { name: "LITERAL" })
      .find((b) => b.hasAttribute("aria-pressed"));
    if (!toggle) throw new Error("LITERAL toggle button not found");
    await u.click(toggle);
    await waitFor(() => expect(mockedGrep).toHaveBeenCalledWith("k8s", undefined));
  });
});
