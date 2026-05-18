// RTL coverage for doc-type filter chips on SearchPage.
//
// The chips filter `doc_type` on dense results client-side — no backend
// changes needed. This file covers:
//   - chip row renders with ALL_TYPES including "skill"
//   - toggling a chip off hides matching results without a re-fetch
//
// DenseResult shape: { source_type, uri, vault, path, title, doc_type, score }
// The filter field is `doc_type` (not `type`).
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
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
  vi.clearAllMocks();
  mockedListVaults.mockResolvedValue({ vaults: [] });
});

function renderAt(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <SearchPage />
    </MemoryRouter>,
  );
}

describe("Search filter chips", () => {
  it("renders type chips including SKILL", () => {
    mockedSearch.mockResolvedValue({ query: "", total: 0, returned: 0, total_matches: 0, results: [] });
    renderAt("/search?q=test");
    expect(screen.getByRole("button", { name: /toggle skill/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /toggle note/i })).toBeTruthy();
  });

  it("toggling NOTE off hides NOTE results but keeps SKILL results", async () => {
    mockedSearch.mockResolvedValue({
      query: "x",
      total: 2,
      returned: 2,
      total_matches: 2,
      results: [
        {
          source_type: "document",
          uri: "akb://v/document/d-1",
          vault: "v",
          path: "overview/vault-skill.md",
          title: "A skill doc",
          doc_type: "skill",
          score: 0.95,
        },
        {
          source_type: "document",
          uri: "akb://v/document/d-2",
          vault: "v",
          path: "n.md",
          title: "A note",
          doc_type: "note",
          score: 0.85,
        },
      ],
    });
    const u = userEvent.setup();
    renderAt("/search?q=x");

    // wait for results to render
    await screen.findByText("A skill doc");
    expect(screen.queryByText("A note")).toBeTruthy();

    // toggle NOTE chip off
    await u.click(screen.getByRole("button", { name: /toggle note/i }));

    // NOTE is now filtered out client-side; skill doc remains
    expect(screen.queryByText("A note")).toBeNull();
    expect(screen.queryByText("A skill doc")).toBeTruthy();

    // only one searchDocs call — no re-fetch
    expect(mockedSearch).toHaveBeenCalledTimes(1);
  });
});
