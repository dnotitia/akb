import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { GlobalSearchDialog } from "@/components/global-search-dialog";
import { listVaults, searchDocs, type CurrentUser } from "@/lib/api";
import { CurrentUserProvider } from "@/contexts/current-user-context";
import { readRecentSearches, recordRecentSearch } from "@/lib/recent-searches";
import { recordRecentDocumentView } from "@/lib/recent-document-views";
import { ResourceNavigationProvider, useResourceNavigationGuard } from "@/contexts/resource-navigation-context";

vi.mock("@/lib/api", () => ({
  searchDocs: vi.fn(),
  listVaults: vi.fn(),
}));

const searchDocsMock = vi.mocked(searchDocs);
const listVaultsMock = vi.mocked(listVaults);
const CURRENT_USER: CurrentUser = {
  user_id: "user-1",
  username: "mina",
  email: "mina@example.com",
  display_name: "Mina",
  is_admin: false,
  auth_method: "local",
  key_class: null,
};

function LocationProbe() {
  const location = useLocation();
  return (
    <>
      <output data-testid="location">{location.pathname + location.search}</output>
      <output data-testid="location-state">{JSON.stringify(location.state)}</output>
    </>
  );
}

function renderDialog() {
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <GlobalSearchDialog />
      <LocationProbe />
      <Routes>
        <Route path="/vault/:name/doc/:id" element={<div>Opened document</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

function renderAuthenticatedDialog() {
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <CurrentUserProvider user={CURRENT_USER}>
        <GlobalSearchDialog />
        <LocationProbe />
      </CurrentUserProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  listVaultsMock.mockResolvedValue({
    vaults: [{ id: "vault-1", name: "alpha" }],
  });
  searchDocsMock.mockResolvedValue({
    query: "postgres",
    total: 1,
    returned: 1,
    total_matches: 1,
    results: [
      {
        source_type: "document",
        uri: "akb://alpha/coll/notes/doc/postgres.md",
        vault: "alpha",
        collection: "notes",
        path: "notes/postgres.md",
        title: "PostgreSQL tuning",
        summary: "Connection pooling and query planning guidance.",
        score: 0.91,
      },
    ],
  });
});

afterEach(cleanup);

describe("GlobalSearchDialog", () => {
  it.each(["/", "/vault/alpha/members"])("searches another accessible Vault without changing the page or query from %s", async (route) => {
    listVaultsMock.mockResolvedValue({ vaults: [{ name: "alpha" }, { name: "팀-beta", role: "reader" }] });
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={[route]}><GlobalSearchDialog /><LocationProbe /></MemoryRouter>);
    await user.click(screen.getByRole("button", { name: "Search knowledge" }));
    const query = screen.getByRole("combobox");
    await user.type(query, "postgres");
    await user.click(screen.getByRole("button", { name: "Documents" }));
    await user.click(screen.getByRole("button", { name: /^Search scope:/ }));
    await user.type(screen.getByRole("searchbox", { name: "Filter vaults" }), "beta");
    expect(screen.queryByRole("menuitemradio", { name: /alpha/ })).not.toBeInTheDocument();
    await user.click(await screen.findByRole("menuitemradio", { name: "팀-beta" }));
    expect(query).toHaveValue("postgres");
    expect(query).toHaveAccessibleName("Search in 팀-beta");
    expect(screen.getByTestId("location")).toHaveTextContent(route);
    await waitFor(() => expect(searchDocsMock).toHaveBeenLastCalledWith("postgres", ["팀-beta"], 12, { source_type: "document" }));
    await user.click(screen.getByRole("button", { name: "Continue in search page" }));
    expect(screen.getByTestId("location")).toHaveTextContent("/vault/%ED%8C%80-beta/search?q=postgres&source=document");
  });

  it("keeps the current search usable when Vault discovery fails and can retry the selector", async () => {
    listVaultsMock.mockRejectedValueOnce(new Error("Directory unavailable"));
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/vault/alpha"]}><GlobalSearchDialog /></MemoryRouter>);
    await user.click(screen.getByRole("button", { name: "Search knowledge" }));
    await user.click(screen.getByRole("button", { name: "Search scope: alpha" }));
    expect(await screen.findByText("Could not load vaults.")).toBeInTheDocument();
    await user.click(screen.getByRole("menuitem", { name: "Retry loading vaults" }));
    expect(await screen.findByRole("menuitemradio", { name: /alpha/ })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.getByRole("combobox")).toHaveAccessibleName("Search in alpha");
    await user.type(screen.getByRole("combobox"), "postgres");
    await waitFor(() => expect(searchDocsMock).toHaveBeenLastCalledWith("postgres", ["alpha"], 12, expect.any(Object)));
  });

  it("filters and selects a Vault with the keyboard, keeping Escape local to the picker", async () => {
    listVaultsMock.mockResolvedValue({ vaults: [{ name: "alpha" }, { name: "beta" }] });
    const user = userEvent.setup();
    renderDialog();
    await user.click(screen.getByRole("button", { name: "Search knowledge" }));
    const scope = screen.getByRole("button", { name: "Search scope: All vaults" });
    await user.click(scope);
    const filter = screen.getByRole("searchbox", { name: "Filter vaults" });
    await waitFor(() => expect(filter).toHaveFocus());
    await user.type(filter, "beta");
    await user.keyboard("{ArrowUp}{Enter}");
    expect(screen.getByRole("combobox")).toHaveAccessibleName("Search in beta");
    expect(scope).toHaveFocus();
    await user.click(scope);
    expect(screen.getByRole("searchbox")).toHaveValue("");
    await user.keyboard("{Escape}");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(scope).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("does not reuse an earlier account's late Vault directory or choices", async () => {
    let finishOld!: (value: { vaults: { name: string }[] }) => void;
    listVaultsMock.mockImplementationOnce(() => new Promise(resolve => { finishOld = resolve; }));
    const contents = (user: CurrentUser) => <MemoryRouter><CurrentUserProvider user={user}><GlobalSearchDialog /></CurrentUserProvider></MemoryRouter>;
    const user = userEvent.setup();
    const { rerender } = render(contents(CURRENT_USER));
    await user.click(screen.getByRole("button", { name: "Search knowledge" }));
    await user.click(screen.getByRole("button", { name: "Search scope: All vaults" }));
    expect(screen.getByText("Loading vaults…")).toBeInTheDocument();
    listVaultsMock.mockResolvedValue({ vaults: [{ name: "new-account-vault" }] });
    rerender(contents({ ...CURRENT_USER, user_id: "user-2" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Search knowledge" }));
    await user.click(screen.getByRole("button", { name: "Search scope: All vaults" }));
    await act(async () => finishOld({ vaults: [{ name: "private-old-vault" }] }));
    expect(await screen.findByRole("menuitemradio", { name: "new-account-vault" })).toBeInTheDocument();
    expect(screen.queryByText("private-old-vault")).not.toBeInTheDocument();
  });

  it("shows an honest empty directory and never mistakes a Vault named all for all-Vault scope", async () => {
    listVaultsMock.mockResolvedValueOnce({ vaults: [] });
    const user = userEvent.setup();
    renderDialog();
    const trigger = screen.getByRole("button", { name: "Search knowledge" });
    await user.click(trigger);
    await user.click(screen.getByRole("button", { name: "Search scope: All vaults" }));
    expect(await screen.findByText("No accessible vaults.")).toBeInTheDocument();
    await user.keyboard("{Escape}{Escape}");
    listVaultsMock.mockResolvedValue({ vaults: [{ name: "all" }] });
    await user.click(trigger);
    await user.click(screen.getByRole("button", { name: "Search scope: All vaults" }));
    await user.click(await screen.findByRole("menuitemradio", { name: "all" }));
    await user.type(screen.getByRole("combobox"), "postgres");
    await waitFor(() => expect(searchDocsMock).toHaveBeenLastCalledWith("postgres", ["all"], 12, expect.any(Object)));
  });

  it.each(["/", "/vault/alpha/members"])("keeps the header search label neutral and shows its scope only in the modal on %s", async (route) => {
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={[route]}><GlobalSearchDialog /></MemoryRouter>);
    const trigger = screen.getByRole("button", { name: "Search knowledge" });
    expect(trigger).toHaveTextContent("Search knowledge…");
    expect(trigger).toHaveAttribute("title", "Search documents, tables, and files");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(trigger);
    expect(screen.getByRole("combobox")).toHaveAccessibleName(
      route === "/" ? "Search all accessible vaults" : "Search in alpha",
    );
    if (route !== "/") {
      expect(screen.getByRole("button", { name: "Search scope: alpha" })).toBeVisible();
    }
  });

  it("starts in the current Vault, switches scope without losing the query, and continues on the matching search page", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/vault/alpha/members"]}>
      <GlobalSearchDialog /><LocationProbe />
    </MemoryRouter>);
    await user.click(screen.getByRole("button", { name: "Search knowledge" }));
    const input = screen.getByRole("combobox", { name: "Search in alpha" });
    await user.type(input, "postgres");
    await screen.findByRole("option");
    expect(searchDocsMock).toHaveBeenLastCalledWith("postgres", ["alpha"], 12, expect.any(Object));
    await user.click(screen.getByRole("button", { name: "Search scope: alpha" }));
    await user.click(screen.getByRole("menuitemradio", { name: /All vaults/ }));
    expect(input).toHaveValue("postgres");
    expect(input).toHaveAccessibleName("Search all accessible vaults");
    await waitFor(() => expect(searchDocsMock).toHaveBeenLastCalledWith("postgres", [], 12, expect.any(Object)));
    await user.click(screen.getByRole("button", { name: "Continue in search page" }));
    expect(screen.getByTestId("location")).toHaveTextContent("/search?q=postgres");
  });

  it("offers an explicit wider search for an empty Vault result without changing scope automatically", async () => {
    searchDocsMock.mockResolvedValue({ query: "missing", results: [], total: 0, returned: 0, total_matches: 0 });
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/vault/alpha"]}><GlobalSearchDialog /></MemoryRouter>);
    await user.click(screen.getByRole("button", { name: "Search knowledge" }));
    await user.type(screen.getByRole("combobox"), "missing");
    const expand = await screen.findByRole("button", { name: "Search all vaults instead" });
    expect(searchDocsMock).toHaveBeenCalledTimes(1);
    expect(searchDocsMock).toHaveBeenLastCalledWith("missing", ["alpha"], 12, expect.any(Object));
    await user.click(expand);
    await waitFor(() => expect(searchDocsMock).toHaveBeenLastCalledWith("missing", [], 12, expect.any(Object)));
    expect(screen.getByRole("combobox")).toHaveValue("missing");
    expect(screen.getByRole("button", { name: "Search scope: All vaults" })).toBeInTheDocument();
  });

  it.each(["/", "/vault", "/vault/new", "/search", "/settings", "/vault/alpha/unknown"])("keeps an all-Vault default outside a recognized named Vault route: %s", async route => {
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={[route]}><GlobalSearchDialog /></MemoryRouter>);
    await user.click(screen.getByRole("button", { name: "Search knowledge" }));
    expect(screen.getByRole("combobox", { name: "Search all accessible vaults" })).toHaveFocus();
    expect(screen.getByRole("button", { name: "Search scope: All vaults" })).toBeInTheDocument();
  });

  it("ignores an old Vault response after the user explicitly expands the scope", async () => {
    const response = await searchDocsMock("postgres");
    searchDocsMock.mockClear();
    let finishOld!: (value: typeof response) => void;
    let finishNew!: (value: typeof response) => void;
    searchDocsMock.mockImplementationOnce(() => new Promise(resolve => { finishOld = resolve; }));
    searchDocsMock.mockImplementationOnce(() => new Promise(resolve => { finishNew = resolve; }));
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/vault/alpha"]}><GlobalSearchDialog /><LocationProbe /></MemoryRouter>);
    await user.click(screen.getByRole("button", { name: "Search knowledge" }));
    await user.type(screen.getByRole("combobox"), "postgres");
    await waitFor(() => expect(searchDocsMock).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("button", { name: "Search scope: alpha" }));
    await user.click(screen.getByRole("menuitemradio", { name: /All vaults/ }));
    await waitFor(() => expect(searchDocsMock).toHaveBeenCalledTimes(2));
    await act(async () => finishOld(response));
    expect(screen.queryByRole("option")).not.toBeInTheDocument();
    await user.click(screen.getByRole("combobox"));
    await user.keyboard("{ArrowDown}{Enter}");
    expect(screen.getByTestId("location")).toHaveTextContent(/^\/vault\/alpha$/);
    await act(async () => finishNew({ ...response, results: [{ ...response.results[0], title: "Global result" }] }));
    expect(await screen.findByRole("option", { name: /Global result/ })).toBeInTheDocument();
  });

  it("preserves the query but restores the visible route default on reopening", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/vault/%ED%8C%80%20Vault"]}><GlobalSearchDialog /></MemoryRouter>);
    const trigger = screen.getByRole("button", { name: "Search knowledge" });
    await user.click(trigger);
    await user.type(screen.getByRole("combobox"), "postgres");
    await screen.findByRole("option");
    await user.click(screen.getByRole("button", { name: "Search scope: 팀 Vault" }));
    await user.click(screen.getByRole("menuitemradio", { name: /All vaults/ }));
    await user.keyboard("{Escape}");
    expect(trigger).toHaveFocus();
    await user.click(trigger);
    expect(screen.getByRole("combobox", { name: "Search in 팀 Vault" })).toHaveValue("postgres");
    await waitFor(() => expect(searchDocsMock).toHaveBeenLastCalledWith("postgres", ["팀 Vault"], 12, expect.any(Object)));
  });

  it.each(["account", "Vault"])("resets pending search state when the %s changes", async (change) => {
    const response = await searchDocsMock("postgres");
    searchDocsMock.mockClear();
    let finish!: (value: typeof response) => void;
    searchDocsMock.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const contents = (user: CurrentUser, vault: string) => <MemoryRouter initialEntries={[`/vault/${vault}`]}>
      <Navigate to={`/vault/${vault}`} />
      <CurrentUserProvider user={user}><GlobalSearchDialog /></CurrentUserProvider>
    </MemoryRouter>;
    const user = userEvent.setup();
    const { rerender } = render(contents(CURRENT_USER, "alpha"));
    await user.click(screen.getByRole("button", { name: "Search knowledge" }));
    await user.type(screen.getByRole("combobox"), "postgres");
    await waitFor(() => expect(searchDocsMock).toHaveBeenCalledOnce());
    rerender(contents(change === "account" ? { ...CURRENT_USER, user_id: "user-2" } : CURRENT_USER, change === "Vault" ? "beta" : "alpha"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Search knowledge" }));
    expect(screen.getByRole("combobox")).toHaveValue("");
    await act(async () => finish(response));
    expect(screen.queryByRole("option")).not.toBeInTheDocument();
  });

  it("keeps Vault search in place and carries scope and kind into advanced search", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/vault/alpha/members"]}>
      <GlobalSearchDialog /><LocationProbe />
    </MemoryRouter>);
    const trigger = screen.getByRole("button", { name: "Search knowledge" });
    await user.click(trigger);
    const input = screen.getByRole("combobox", { name: "Search in alpha" });
    expect(input).toHaveFocus();
    expect(screen.getByTestId("location")).toHaveTextContent("/vault/alpha/members");
    await user.click(screen.getByRole("button", { name: "Documents" }));
    await user.type(input, "postgres");
    await screen.findByRole("option", { name: /PostgreSQL tuning/ });
    expect(searchDocsMock).toHaveBeenLastCalledWith("postgres", ["alpha"], 12, { source_type: "document" });
    await user.click(screen.getByRole("button", { name: "Continue in search page" }));
    expect(screen.getByTestId("location")).toHaveTextContent("/vault/alpha/search?q=postgres&source=document");
  });

  it("returns scoped previews to the same header trigger and defaults to the destination Vault on reopening", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/vault/alpha"]}>
      <GlobalSearchDialog /><LocationProbe />
    </MemoryRouter>);
    await user.click(screen.getByRole("button", { name: "Search knowledge" }));
    await user.type(screen.getByRole("combobox"), "postgres");
    await screen.findByRole("option", { name: /PostgreSQL tuning/ });
    await user.keyboard("{Enter}");
    expect(screen.getByTestId("location-state")).toHaveTextContent('"returnFocusId":"global-search-trigger"');
    expect(screen.getByTestId("location-state")).toHaveTextContent('"documentPreview":true');
    await user.click(screen.getByRole("button", { name: "Search knowledge" }));
    expect(screen.getByRole("combobox")).toHaveValue("postgres");
    expect(screen.getByRole("button", { name: "Search scope: alpha" })).toBeInTheDocument();
  });

  it("shows only this Vault's re-entry history and clears it without affecting other scopes", async () => {
    for (const [query, vaults] of [["global query", []], ["alpha query", ["alpha"]], ["beta query", ["beta"]]] as const) {
      recordRecentSearch(CURRENT_USER.user_id, { query, vaults: [...vaults], mode: "semantic", surface: "global" });
    }
    for (const vault of ["alpha", "beta"]) recordRecentDocumentView(CURRENT_USER.user_id, {
      vault, path: "notes/recent.md", title: `${vault} recent document`,
    });
    listVaultsMock.mockResolvedValue({ vaults: [{ name: "alpha" }, { name: "beta" }] });
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/vault/alpha"]}><CurrentUserProvider user={CURRENT_USER}>
      <GlobalSearchDialog />
    </CurrentUserProvider></MemoryRouter>);
    const trigger = screen.getByRole("button", { name: "Search knowledge" });
    await user.click(trigger);
    expect(await screen.findByRole("button", { name: /alpha recent document/ })).toBeInTheDocument();
    expect(screen.queryByText("beta recent document")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /alpha query/ })).toBeInTheDocument();
    expect(screen.queryByText("global query")).not.toBeInTheDocument();
    expect(screen.queryByText("beta query")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear" }));
    expect(readRecentSearches(CURRENT_USER.user_id).map(item => item.query).sort()).toEqual(["beta query", "global query"]);
    await user.keyboard("{Escape}");
    expect(trigger).toHaveFocus();
  });

  it("consults the active editor before a search result or advanced search leaves it", async () => {
    const guard = vi.fn(() => false);
    function DirtyEditor() { useResourceNavigationGuard(guard); return <p>Unsaved draft</p>; }
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/vault/alpha/doc/draft?view=edit"]}>
      <ResourceNavigationProvider><DirtyEditor /><GlobalSearchDialog /><LocationProbe /></ResourceNavigationProvider>
    </MemoryRouter>);
    await user.click(screen.getByRole("button", { name: "Search knowledge" }));
    await user.type(screen.getByRole("combobox"), "postgres");
    await user.click(await screen.findByRole("option", { name: /PostgreSQL tuning/ }));
    expect(guard).toHaveBeenCalledWith("/vault/alpha/doc/notes%2Fpostgres.md", {
      state: expect.objectContaining({ documentPreview: true, returnFocusId: "global-search-trigger" }),
    });
    expect(screen.getByTestId("location")).toHaveTextContent("/vault/alpha/doc/draft?view=edit");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Search knowledge" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Continue in search page" }));
    expect(guard).toHaveBeenLastCalledWith("/vault/alpha/search?q=postgres");
    expect(screen.getByTestId("location")).toHaveTextContent("/vault/alpha/doc/draft?view=edit");
  });

  it("does not expose or activate hidden partial results during a degraded response", async () => {
    const response = await searchDocsMock("postgres");
    searchDocsMock.mockResolvedValue({ ...response, degraded: true });
    const user = userEvent.setup();
    renderDialog();
    await user.click(screen.getByRole("button", { name: "Search knowledge" }));
    const input = screen.getByRole("combobox");
    await user.type(input, "postgres");
    await screen.findByText(/Search is incomplete/);
    expect(input).toHaveAttribute("aria-expanded", "false");
    expect(input).not.toHaveAttribute("aria-controls");
    expect(input).not.toHaveAttribute("aria-activedescendant");
    await user.keyboard("{ArrowDown}{Enter}");
    expect(screen.getByTestId("location")).toHaveTextContent(/^\/$/);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("opens in place, focuses the search field, and returns focus on Escape", async () => {
    const user = userEvent.setup();
    renderDialog();

    const trigger = screen.getByRole("button", { name: "Search knowledge" });
    await user.click(trigger);

    expect(screen.getByTestId("location")).toHaveTextContent("/");
    const dialog = screen.getByTestId("global-search-dialog");
    expect(dialog).toHaveClass("top-16", "max-w-[96rem]");
    expect(screen.getByRole("heading", { name: "Suggested searches" })).toBeInTheDocument();
    expect(
      screen.getByRole("group", { name: "Limit global search by content kind" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "All" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("combobox", { name: "Search all accessible vaults" })).toHaveFocus();
    expect(searchDocsMock).not.toHaveBeenCalled();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(trigger).toHaveFocus();
  });

  it("searches without changing routes and opens the active result with Enter", async () => {
    const user = userEvent.setup();
    renderDialog();

    await user.click(screen.getByRole("button", { name: "Search knowledge" }));
    const input = screen.getByRole("combobox", { name: "Search all accessible vaults" });
    await user.type(input, "postgres");

    await waitFor(() => expect(searchDocsMock).toHaveBeenCalledWith("postgres", [], 12, expect.any(Object)));
    expect(await screen.findByRole("option", { name: /PostgreSQL tuning/i })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("heading", { name: "Top matches" })).toBeInTheDocument();
    expect(screen.queryByText("91%")).toBeNull();
    expect(screen.getByTestId("location")).toHaveTextContent("/");

    await user.keyboard("{Enter}");
    expect(await screen.findByText("Opened document")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByTestId("location-state")).toHaveTextContent(
      '"documentPreview":true',
    );
    expect(screen.getByTestId("location-state")).toHaveTextContent(
      '"pathname":"/"',
    );
    expect(screen.getByTestId("location-state")).toHaveTextContent(
      '"returnFocusId":"global-search-trigger"',
    );
  });

  it("offers recent global searches before the fixed suggestions", async () => {
    recordRecentSearch(CURRENT_USER.user_id, {
      query: "postgres tuning",
      mode: "semantic",
      surface: "global",
    });
    const user = userEvent.setup();
    renderAuthenticatedDialog();

    await user.click(screen.getByRole("button", { name: "Search knowledge" }));
    expect(screen.getByRole("heading", { name: "Recent searches" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /postgres tuning/i }));

    await waitFor(() =>
      expect(searchDocsMock).toHaveBeenCalledWith("postgres tuning", [], 12, expect.any(Object)),
    );
  });

  it("offers accessible recently viewed documents and opens them as previews", async () => {
    recordRecentDocumentView(CURRENT_USER.user_id, {
      vault: "alpha",
      path: "notes/architecture.md",
      title: "Architecture notes",
      type: "note",
    });
    recordRecentDocumentView(CURRENT_USER.user_id, {
      vault: "revoked",
      path: "private/roadmap.md",
      title: "Private roadmap",
      type: "note",
    });
    const user = userEvent.setup();
    renderAuthenticatedDialog();

    await user.click(screen.getByRole("button", { name: "Search knowledge" }));
    expect(
      await screen.findByRole("heading", { name: "Recently viewed" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Private roadmap")).toBeNull();

    await user.click(screen.getByRole("button", { name: /Architecture notes/i }));
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/vault/alpha/doc/notes%2Farchitecture.md",
    );
    expect(screen.getByTestId("location-state")).toHaveTextContent(
      '"documentPreview":true',
    );
  });

  it("filters mixed top matches on the server and cleans indexed context", async () => {
    searchDocsMock.mockResolvedValue({
      query: "platform",
      total: 2,
      returned: 2,
      total_matches: 2,
      results: [
        {
          source_type: "document",
          uri: "akb://alpha/coll/notes/doc/platform.md",
          vault: "alpha",
          collection: "notes",
          path: "notes/platform.md",
          title: "Platform guide",
          tags: ["runtime"],
          matched_section:
            "[# Platform guide > ## Runtime] * Worker and API responsibilities.",
          score: 0.91,
        },
        {
          source_type: "table",
          uri: "akb://alpha/table/services",
          vault: "alpha",
          path: "services",
          title: "Services",
          score: 0.84,
        },
      ],
    });
    const user = userEvent.setup();
    renderDialog();

    await user.click(screen.getByRole("button", { name: "Search knowledge" }));
    await user.type(
      screen.getByRole("combobox", { name: "Search all accessible vaults" }),
      "platform",
    );
    await waitFor(() => expect(searchDocsMock).toHaveBeenCalled());
    expect(await screen.findByText("Worker and API responsibilities.")).toBeInTheDocument();
    expect(screen.queryByText(/\[# Platform guide/)).toBeNull();

    searchDocsMock.mockResolvedValueOnce({ query: "platform", total: 1, returned: 1, total_matches: 1,
      results: [{ source_type: "table", uri: "akb://alpha/table/services", vault: "alpha", path: "services", title: "Services", score: 1 }] });
    await user.click(screen.getByRole("button", { name: /Tables, 1 result/i }));
    await waitFor(() => expect(searchDocsMock).toHaveBeenLastCalledWith("platform", [], 12, { source_type: "table" }));
    await screen.findByRole("option", { name: /Services/i });
    expect(screen.getByRole("option", { name: /Services/i })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /Platform guide/i })).toBeNull();
  });

  it("keeps an input-time content filter and offers a route out of filtered-empty results", async () => {
    const user = userEvent.setup();
    renderDialog();

    await user.click(screen.getByRole("button", { name: "Search knowledge" }));
    searchDocsMock.mockResolvedValueOnce({ query: "postgres", total: 0, returned: 0, total_matches: 0, results: [] });
    await user.click(screen.getByRole("button", { name: "Tables" }));
    await user.type(
      screen.getByRole("combobox", { name: "Search all accessible vaults" }),
      "postgres",
    );

    expect(await screen.findByText(/No results for/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Tables, 0 results/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await user.click(screen.getByRole("button", { name: "Show all results" }));
    expect(
      await screen.findByRole("option", { name: /PostgreSQL tuning/i }),
    ).toBeInTheDocument();
  });
});
