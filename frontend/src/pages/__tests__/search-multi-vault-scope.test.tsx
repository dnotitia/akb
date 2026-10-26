// Query-attached scopes work on both global and Vault search routes. An absent
// v uses the route default, while an explicit empty v means all accessible Vaults.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, render, screen, cleanup, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";

import SearchPage from "../search";
import { searchDocs, grepDocs, listVaults } from "@/lib/api";
import { CurrentUserProvider } from "@/contexts/current-user-context";

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
  vi.mocked(grepDocs).mockReset().mockResolvedValue({
    pattern: "x", regex: false, total_docs: 0, total_matches: 0, results: [],
  });
  mockedListVaults.mockReset().mockResolvedValue({
    vaults: [{ name: "alpha" }, { name: "beta" }, { name: "gamma" }],
  });
});

function renderAt(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/search" element={<SearchPage />} />
        <Route path="/vault/:name/search" element={<SearchPage />} />
      </Routes>
      <Navigation />
    </MemoryRouter>,
  );
}

function Navigation() {
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <>
      <output data-testid="search-url">{location.pathname}{location.search}</output>
      <button onClick={() => navigate(-1)}>Back</button>
      <button onClick={() => navigate(1)}>Forward</button>
    </>
  );
}

async function removeVault(user: ReturnType<typeof userEvent.setup>, name: string) {
  await user.click(screen.getByRole("button", { name: /^Search scope:/ }));
  await user.click(screen.getByRole("menuitemcheckbox", { name: new RegExp(`^${name}(?:\\s*Current vault)?$`), checked: true }));
  await user.keyboard("{Escape}");
}

describe("SearchPage · multi-vault scope", () => {
  it("drops old-account completions while retaining the URL query and scope", async () => {
    let resolve!: (value: typeof EMPTY) => void;
    mockedSearch.mockReturnValueOnce(new Promise(r => { resolve = r; }));
    const view = (id: string, checking = false, revision = 0) => <MemoryRouter initialEntries={["/search?q=postgres&v=alpha"]}><CurrentUserProvider user={{ user_id: id, username: id, email: `${id}@example.com`, display_name: null, is_admin: false, auth_method: "local", key_class: null }} checking={checking} revision={revision}><SearchPage /></CurrentUserProvider></MemoryRouter>;
    const { rerender } = render(view("a"));
    await waitFor(() => expect(mockedSearch).toHaveBeenCalledOnce());
    rerender(view("b", true));
    await act(async () => resolve({ ...EMPTY, results: [{ title: "Private A result", vault: "alpha", path: "private.md", uri: "akb://alpha/doc/private.md", score: 1 }] } as unknown as typeof EMPTY));
    expect(screen.queryByText("Private A result")).not.toBeInTheDocument();
    rerender(view("b", false, 1));
    await waitFor(() => expect(mockedSearch).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("searchbox", { name: "Search query" })).toHaveValue("postgres");
    expect(mockedSearch).toHaveBeenLastCalledWith("postgres", ["alpha"], 25, expect.any(Object));
  });
  it("uses an explicit multi-Vault scope on a named Vault route", async () => {
    renderAt("/vault/alpha/search?q=postgres&v=alpha,beta&source=file&collection=guides");
    await waitFor(() => expect(mockedSearch).toHaveBeenCalledWith(
      "postgres", ["alpha", "beta"], 25,
      expect.objectContaining({ source_type: "file", collection: "guides" }),
    ));
  });

  it("distinguishes an explicit All scope from the route's default Vault", async () => {
    renderAt("/vault/alpha/search?q=postgres&v=");
    await waitFor(() => expect(mockedSearch).toHaveBeenCalledWith(
      "postgres", [], 25, expect.any(Object),
    ));
  });

  it("uses the route Vault when no explicit scope was saved", async () => {
    renderAt("/vault/alpha/search?q=postgres");
    await waitFor(() => expect(mockedSearch).toHaveBeenCalledWith(
      "postgres", ["alpha"], 25, expect.any(Object),
    ));
    await waitFor(() => expect(mockedListVaults).toHaveBeenCalled());
  });

  it("keeps valid Vault choices and ignores malformed or duplicate directory entries", async () => {
    mockedListVaults.mockResolvedValueOnce({
      vaults: [{ name: "alpha" }, null, { name: 5 }, {}, { name: "" }, { name: "beta" }, { name: "beta" }],
    } as unknown as Awaited<ReturnType<typeof listVaults>>);
    const user = userEvent.setup();
    renderAt("/vault/alpha/search?q=x");
    await user.click(screen.getByRole("button", { name: "Search scope: alpha" }));
    expect(await screen.findByRole("menuitemcheckbox", { name: "beta" })).toBeInTheDocument();
    expect(screen.getAllByRole("menuitemcheckbox")).toHaveLength(2);
  });
  it("passes the comma-joined ?v= as a string[] to searchDocs", async () => {
    renderAt("/search?q=postgres&v=alpha,beta");
    await waitFor(() =>
      expect(mockedSearch).toHaveBeenCalledWith("postgres", ["alpha", "beta"], 25, expect.any(Object)),
    );
  });

  it("exposes the All vaults picker inside the query form when nothing is scoped", async () => {
    renderAt("/search");
    const picker = await screen.findByRole("button", { name: "Search scope: All vaults" });
    expect(screen.getByRole("search")).toContainElement(picker);
  });

  it("summarizes selected Vaults in one trigger and keeps their names in the checklist", async () => {
    const user = userEvent.setup();
    renderAt("/search?q=x&v=alpha,beta");
    const scope = screen.getByRole("group", { name: "Vault search scope" });
    expect(within(scope).getAllByRole("button")).toHaveLength(1);
    await user.click(await screen.findByRole("button", { name: "Search scope: 2 vaults" }));
    expect(screen.getByRole("menuitemcheckbox", { name: "alpha", checked: true })).toBeInTheDocument();
    expect(screen.getByRole("menuitemcheckbox", { name: "beta", checked: true })).toBeInTheDocument();
  });
});

describe("SearchPage · multi-vault scope · interactions (write path)", () => {
  it("checking a vault in the picker adds it to the scope and re-searches", async () => {
    const user = userEvent.setup();
    renderAt("/search?q=x&v=alpha");
    await waitFor(() =>
      expect(mockedSearch).toHaveBeenCalledWith("x", ["alpha"], 25, expect.any(Object)),
    );
    await user.click(await screen.findByRole("button", { name: /Search scope/ }));
    await user.click(await screen.findByRole("menuitemcheckbox", { name: "beta" }));
    await waitFor(() =>
      expect(mockedSearch).toHaveBeenLastCalledWith("x", ["alpha", "beta"], 25, expect.any(Object)),
    );
  });

  it("unchecking a selected Vault drops it from the scope and re-searches", async () => {
    const user = userEvent.setup();
    renderAt("/search?q=x&v=alpha,beta");
    await waitFor(() =>
      expect(mockedSearch).toHaveBeenCalledWith("x", ["alpha", "beta"], 25, expect.any(Object)),
    );
    await removeVault(user, "alpha");
    await waitFor(() =>
      expect(mockedSearch).toHaveBeenLastCalledWith("x", ["beta"], 25, expect.any(Object)),
    );
  });

  it("All vaults resets the query scope", async () => {
    const user = userEvent.setup();
    renderAt("/search?q=x&v=alpha,beta");
    await waitFor(() => expect(mockedSearch).toHaveBeenCalled());
    await user.click(await screen.findByRole("button", { name: /Search scope/ }));
    await user.click(await screen.findByRole("menuitem", { name: "All vaults" }));
    await waitFor(() =>
      expect(mockedSearch).toHaveBeenLastCalledWith("x", [], 25, expect.any(Object)),
    );
    expect(await screen.findByRole("button", { name: "Search scope: All vaults" })).toBeTruthy();
  });

  it("adds a second Vault on a scoped route without losing query, content kind, or collection", async () => {
    const user = userEvent.setup();
    renderAt("/vault/alpha/search?q=guide&source=file&collection=reports");
    await user.click(await screen.findByRole("button", { name: "Search scope: alpha" }));
    await user.type(screen.getByRole("searchbox", { name: "Filter vaults" }), "bet");
    await user.click(await screen.findByRole("menuitemcheckbox", { name: "beta" }));
    await waitFor(() => expect(mockedSearch).toHaveBeenLastCalledWith(
      "guide", ["alpha", "beta"], 25,
      expect.objectContaining({ source_type: "file", collection: "reports" }),
    ));
    expect(screen.getByTestId("search-url")).toHaveTextContent("v=alpha%2Cbeta");
  });

  it("keeps explicit All through mode changes and browser Back/Forward on a scoped route", async () => {
    const user = userEvent.setup();
    renderAt("/vault/alpha/search?q=x");
    await removeVault(user, "alpha");
    await waitFor(() => expect(mockedSearch).toHaveBeenLastCalledWith("x", [], 25, expect.any(Object)));
    expect(screen.getByTestId("search-url")).toHaveTextContent("v=");
    await user.click(screen.getByRole("button", { name: "Search method" }));
    await user.click(screen.getByRole("menuitemradio", { name: /^Exact text/ }));
    await waitFor(() => expect(grepDocs).toHaveBeenLastCalledWith("x", [], 20, expect.any(Object)));
    await user.click(screen.getByRole("button", { name: "Back" }));
    await waitFor(() => expect(mockedSearch).toHaveBeenLastCalledWith("x", [], 25, expect.any(Object)));
    await user.click(screen.getByRole("button", { name: "Back" }));
    await waitFor(() => expect(mockedSearch).toHaveBeenLastCalledWith("x", ["alpha"], 25, expect.any(Object)));
    await user.click(screen.getByRole("button", { name: "Forward" }));
    await waitFor(() => expect(mockedSearch).toHaveBeenLastCalledWith("x", [], 25, expect.any(Object)));
  });

  it("turns a selected name suggestion into a saved Vault scope and clears only the consumed query", async () => {
    const user = userEvent.setup();
    renderAt("/vault/alpha/search?q=old&source=table");
    const query = screen.getByRole("searchbox", { name: "Search query" });
    await user.clear(query);
    await user.type(query, "bet");
    await user.click(await screen.findByRole("button", { name: "Add beta to search scope" }));
    expect(query).toHaveValue("");
    expect(query).toHaveFocus();
    expect(screen.getByTestId("search-url")).toHaveTextContent("v=alpha%2Cbeta");
    expect(screen.getByTestId("search-url")).toHaveTextContent("source=table");
    expect(screen.getByTestId("search-url")).not.toHaveTextContent("q=");
    await user.type(query, "new");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(mockedSearch).toHaveBeenLastCalledWith(
      "new", ["alpha", "beta"], 25, expect.objectContaining({ source_type: "table" }),
    ));
  });

  it("does not restore a cleared query when adding another Vault", async () => {
    const user = userEvent.setup();
    renderAt("/vault/alpha/search?q=old");
    await user.clear(screen.getByRole("searchbox", { name: "Search query" }));
    await user.click(screen.getByRole("button", { name: "Search scope: alpha" }));
    await user.click(await screen.findByRole("menuitemcheckbox", { name: "beta" }));
    expect(screen.getByTestId("search-url")).not.toHaveTextContent("q=");
    expect(screen.getByTestId("search-url")).toHaveTextContent("v=alpha%2Cbeta");
  });
});
