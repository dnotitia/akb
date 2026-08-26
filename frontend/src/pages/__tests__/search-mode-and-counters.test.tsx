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
import { MemoryRouter, useLocation } from "react-router-dom";

import SearchPage from "../search";
import {
  searchDocs,
  grepDocs,
  listVaults,
  type CurrentUser,
} from "@/lib/api";
import { CurrentUserProvider } from "@/contexts/current-user-context";
import { recordRecentSearch } from "@/lib/recent-searches";
import { recordRecentDocumentView } from "@/lib/recent-document-views";

vi.mock("@/lib/api", () => ({
  searchDocs: vi.fn(),
  grepDocs: vi.fn(),
  listVaults: vi.fn(),
}));

const mockedSearch = vi.mocked(searchDocs);
const mockedGrep = vi.mocked(grepDocs);
const mockedListVaults = vi.mocked(listVaults);
const CURRENT_USER: CurrentUser = {
  user_id: "user-1",
  username: "mina",
  email: "mina@example.com",
  display_name: "Mina",
  is_admin: false,
  auth_method: "local",
  key_class: null,
};

afterEach(cleanup);
beforeEach(() => {
  window.localStorage.clear();
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

function renderAuthenticatedAt(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <CurrentUserProvider user={CURRENT_USER}>
        <SearchPage />
      </CurrentUserProvider>
    </MemoryRouter>,
  );
}

function SearchLocationProbe() {
  const location = useLocation();
  return (
    <output data-testid="search-location">
      {JSON.stringify({
        pathname: location.pathname,
        search: location.search,
        state: location.state,
      })}
    </output>
  );
}

describe("SearchPage · semantic (dense) mode", () => {
  it("uses a full-height two-row command workspace with a stable results ledger", async () => {
    const user = userEvent.setup();
    renderAt("/search");

    const workspace = screen.getByTestId("search-workspace");
    const commandHeader = screen.getByTestId("search-command-header");
    const suggestions = await screen.findByLabelText("Suggested searches");

    expect(workspace).toHaveClass(
      "h-full",
      "w-full",
      "max-w-none",
      "overflow-hidden",
    );
    expect(commandHeader).toHaveClass("flex", "min-h-14");
    expect(workspace.contains(commandHeader)).toBe(true);
    expect(workspace.contains(suggestions)).toBe(true);
    expect(screen.getByRole("group", { name: "Search mode" })).toBeTruthy();
    expect(screen.getByRole("region", { name: "Search results" })).toBeTruthy();
    expect(screen.getByTestId("search-tool-row")).toHaveClass(
      "min-h-10",
      "border-t",
    );
    expect(screen.getByTestId("search-scope-row")).toHaveClass(
      "flex-1",
      "flex-wrap",
    );
    expect(screen.getByTestId("search-results-pane")).toHaveClass(
      "flex-1",
      "overflow-y-auto",
    );
    await user.click(
      screen.getByRole("button", { name: "Filter by document type" }),
    );
    expect(
      screen.getByRole("complementary", { name: "Search filters" }),
    ).toBeTruthy();
  });

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
    await waitFor(() =>
      expect(mockedSearch).toHaveBeenCalledWith("postgres", [], 25),
    );
    expect(await screen.findByText("PostgreSQL tuning")).toBeTruthy();
    expect(screen.getByText("Top match")).toBeTruthy();
    expect(screen.queryByText("91%")).toBeNull();
    expect(mockedGrep).not.toHaveBeenCalled();
  });

  it("opens document results with the current search location preserved for preview", async () => {
    mockedSearch.mockResolvedValue({
      query: "postgres",
      total: 1,
      returned: 1,
      total_matches: 1,
      results: [
        {
          source_type: "document",
          uri: "akb://akb/coll/notes/doc/postgres.md",
          vault: "akb",
          path: "notes/postgres.md",
          title: "PostgreSQL tuning",
          score: 0.91,
        },
      ],
    });
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/search?q=postgres"]}>
        <SearchPage />
        <SearchLocationProbe />
      </MemoryRouter>,
    );

    await user.click(
      await screen.findByRole("link", { name: /PostgreSQL tuning/i }),
    );

    const location = screen.getByTestId("search-location");
    expect(location).toHaveTextContent(
      '"pathname":"/vault/akb/doc/notes%2Fpostgres.md"',
    );
    expect(location).toHaveTextContent('"documentPreview":true');
    expect(location).toHaveTextContent('"search":"?q=postgres"');
  });

  it("does not search when ?q is missing", async () => {
    renderAt("/search");
    // Give the effect a tick to misfire.
    await new Promise((r) => setTimeout(r, 20));
    expect(mockedSearch).not.toHaveBeenCalled();
    expect(mockedGrep).not.toHaveBeenCalled();
  });

  it("supports suggested searches and returning to the empty state", async () => {
    mockedSearch.mockResolvedValue({
      query: "deployment guide",
      total: 0,
      returned: 0,
      total_matches: 0,
      results: [],
    });
    renderAt("/search");

    expect(
      screen.getByRole("searchbox", { name: "Search query" }),
    ).toBeTruthy();
    expect(screen.getByLabelText("Suggested searches")).toBeTruthy();

    const u = userEvent.setup();
    await u.click(screen.getByRole("button", { name: "deployment guide" }));
    await waitFor(() =>
      expect(mockedSearch).toHaveBeenCalledWith("deployment guide", [], 25),
    );

    await u.click(screen.getByRole("button", { name: "Clear search query" }));
    expect(await screen.findByLabelText("Suggested searches")).toBeTruthy();
    expect(screen.getByRole("searchbox", { name: "Search query" })).toHaveValue(
      "",
    );
  });

  it("uses local recent searches and accessible document views as a re-entry state", async () => {
    recordRecentSearch(CURRENT_USER.user_id, {
      query: "worker isolation",
      mode: "semantic",
      surface: "advanced",
    });
    recordRecentDocumentView(CURRENT_USER.user_id, {
      vault: "v",
      path: "notes/worker.md",
      title: "Worker isolation",
      type: "note",
    });
    mockedListVaults.mockResolvedValue({ vaults: [{ name: "v" }] });
    mockedSearch.mockResolvedValue({
      query: "worker isolation",
      total: 0,
      returned: 0,
      total_matches: 0,
      results: [],
    });

    const user = userEvent.setup();
    renderAuthenticatedAt("/search");

    expect(await screen.findByRole("heading", { name: "Recent searches" })).toBeTruthy();
    expect(await screen.findByRole("heading", { name: "Recently viewed" })).toBeTruthy();
    expect(screen.getByRole("link", { name: /Worker isolation/i })).toHaveAttribute(
      "href",
      "/vault/v/doc/notes%2Fworker.md",
    );

    await user.click(screen.getByRole("button", { name: /worker isolation/i }));
    await waitFor(() =>
      expect(mockedSearch).toHaveBeenCalledWith("worker isolation", [], 25),
    );
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
        {
          uri: "akb://akb/document/a",
          vault: "akb",
          path: "a.md",
          title: "A",
          matches: [],
        },
        {
          uri: "akb://akb/document/b",
          vault: "akb",
          path: "b.md",
          title: "B",
          matches: [],
        },
      ],
    });
    renderAt("/search?q=TODO&mode=literal");
    expect(await screen.findByText(/2 docs · 7 matches/)).toBeTruthy();
    expect(mockedSearch).not.toHaveBeenCalled();
  });

  it("opens literal document results with the search ledger preserved for preview", async () => {
    mockedGrep.mockResolvedValue({
      pattern: "TODO",
      regex: false,
      total_docs: 1,
      total_matches: 1,
      results: [
        {
          uri: "akb://akb/coll/notes/doc/todo.md",
          vault: "akb",
          path: "notes/todo.md",
          title: "TODO notes",
          matches: [{ section: "Tasks", text: "TODO: verify preview" }],
        },
      ],
    });
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/search?q=TODO&mode=literal"]}>
        <SearchPage />
        <SearchLocationProbe />
      </MemoryRouter>,
    );

    await user.click(await screen.findByRole("link", { name: /TODO notes/i }));

    const location = screen.getByTestId("search-location");
    expect(location).toHaveTextContent(
      '"pathname":"/vault/akb/doc/notes%2Ftodo.md"',
    );
    expect(location).toHaveTextContent('"documentPreview":true');
    expect(location).toHaveTextContent('"search":"?q=TODO&mode=literal"');
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
    // The mode toggle ("Literal", with aria-pressed) and the short-query
    // hint link below ("LITERAL") both reference literal mode. The toggle
    // is the only one with aria-pressed set.
    const toggle = screen
      .getAllByRole("button", { name: "Literal" })
      .find((b) => b.hasAttribute("aria-pressed"));
    if (!toggle) throw new Error("Literal toggle button not found");
    await u.click(toggle);
    await waitFor(() => expect(mockedGrep).toHaveBeenCalledWith("k8s", []));
  });
});
