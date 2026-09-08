import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import SearchPage from "../search";
import { searchDocs, grepDocs, listVaults } from "@/lib/api";

vi.mock("@/lib/api", () => ({
  searchDocs: vi.fn(),
  grepDocs: vi.fn(),
  listVaults: vi.fn(),
}));
const search = vi.mocked(searchDocs);
const grep = vi.mocked(grepDocs);
const response = (results: object[] = []) => ({
  query: "x",
  total: results.length,
  returned: results.length,
  total_matches: results.length,
  results,
});
const hit = (title: string) => ({
  title,
  uri: "akb://v/doc/x.md",
  vault: "v",
  path: "x.md",
  source_type: "document",
  doc_type: "report",
  score: 1,
});
function Navigation() {
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <>
      <output data-testid="url">{location.search}</output>
      <button onClick={() => navigate(-1)}>Back</button>
    </>
  );
}
function renderAt(url = "/search?q=x") {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <SearchPage />
      <Navigation />
    </MemoryRouter>,
  );
}
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(listVaults).mockResolvedValue({ vaults: [] });
  search.mockResolvedValue(response());
  grep.mockResolvedValue({
    pattern: "x",
    regex: false,
    total_docs: 0,
    total_matches: 0,
    results: [],
  });
});
afterEach(cleanup);
const openFilters = async (user: ReturnType<typeof userEvent.setup>) =>
  user.click(screen.getByRole("button", { name: "Filter by document type" }));

describe("server search filters", () => {
  it("requests archived documents on the server, displays status, and preserves scope across modes and Back", async () => {
    search.mockImplementation(async (_q, _v, _l, options) => ({
      ...response(options?.archive_scope === "archived" ? [{ ...hit("Old guide"), status: "archived" }] : []),
      archive_scope: options?.archive_scope ?? "unarchived",
    }));
    grep.mockResolvedValue({ pattern: "x", regex: false, total_docs: 1, total_matches: 1, archive_scope: "archived", results: [{ ...hit("Old literal guide"), status: "archived", matches: [{ section: null, text: "x" }] }] });
    renderAt();
    const user = userEvent.setup();
    await openFilters(user);
    await user.click(screen.getByLabelText("Document state"));
    await user.click(screen.getByRole("menuitemradio", { name: "Archived documents" }));
    await screen.findByText("Old guide");
    expect(screen.getByText("Archived")).toBeInTheDocument();
    expect(search).toHaveBeenLastCalledWith("x", [], 25, expect.objectContaining({ archive_scope: "archived", source_type: "document" }));
    expect(screen.getByTestId("url")).toHaveTextContent("archive_scope=archived");
    await user.click(screen.getByRole("button", { name: "Literal", pressed: false }));
    await screen.findByText("Old literal guide");
    expect(screen.getByText("Archived")).toBeInTheDocument();
    expect(grep).toHaveBeenLastCalledWith("x", [], 20, expect.objectContaining({ archive_scope: "archived" }));
    await user.click(screen.getByRole("button", { name: "Back" }));
    await screen.findByText("Old guide");
    expect(screen.getByLabelText("Document state")).toHaveTextContent("Archived documents");
  });

  it("does not claim no archives when an older server ignores the scope and offers recovery", async () => {
    renderAt("/search?q=x&archive_scope=archived");
    await screen.findByText(/This server could not confirm/);
    expect(screen.queryByRole("heading", { name: /No results/ })).not.toBeInTheDocument();
    expect(screen.queryByText("0 top results loaded")).not.toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "search current documents" }));
    await screen.findByRole("heading", { name: /No results/ });
    expect(screen.getByTestId("url")).not.toHaveTextContent("archive_scope");
  });

  it("hides wrong-scope hits from an older server instead of client-filtering them", async () => {
    search.mockResolvedValue(response([hit("Current guide")]));
    renderAt("/search?q=x&archive_scope=archived");
    await screen.findByText(/This server could not confirm/);
    expect(screen.queryByText("Current guide")).not.toBeInTheDocument();
  });

  it("keeps filters available before results and after a zero-match response", async () => {
    renderAt();
    const user = userEvent.setup();
    await openFilters(user);
    expect(screen.getByRole("button", { name: "Toggle report" })).toBeEnabled();
    expect(screen.getByLabelText("Tags (match any)")).toBeEnabled();
    expect(screen.getByRole("button", { name: "Tables" })).toBeEnabled();
  });

  it("finds a matching document outside the initial 25 results by re-searching on the server", async () => {
    const first25 = Array.from({ length: 25 }, (_, i) => hit(`Initial ${i}`));
    search.mockImplementation(async (_q, _v, _l, options) =>
      response(
        options?.doc_types?.includes("report")
          ? [hit("Previously outside top 25")]
          : first25,
      ),
    );
    renderAt();
    const user = userEvent.setup();
    await screen.findByText("Initial 0");
    await openFilters(user);
    await user.click(screen.getByRole("button", { name: "Toggle report" }));
    await screen.findByText("Previously outside top 25");
    expect(screen.queryByText("Initial 0")).not.toBeInTheDocument();
    expect(search).toHaveBeenLastCalledWith(
      "x",
      [],
      25,
      expect.objectContaining({
        doc_types: ["report"],
        source_type: "document",
      }),
    );
    expect(screen.getByTestId("url")).toHaveTextContent("doc_type=report");
    await user.click(screen.getByRole("button", { name: "Back" }));
    await screen.findByText("Initial 0");
    expect(
      screen.getByRole("button", { name: "Toggle report" }),
    ).toHaveAttribute("aria-pressed", "false");
  });

  it("sends resource type, collection, tags and archive filters rather than slicing results", async () => {
    renderAt();
    const user = userEvent.setup();
    await openFilters(user);
    await user.click(screen.getByRole("button", { name: "Tables" }));
    await waitFor(() =>
      expect(search).toHaveBeenLastCalledWith(
        "x",
        [],
        25,
        expect.objectContaining({ source_type: "table" }),
      ),
    );
    await user.type(
      screen.getByLabelText("Collection path (including children)"),
      "guides/api",
    );
    await user.click(screen.getByRole("button", { name: "Apply" }));
    await user.type(
      screen.getByLabelText("Tags (match any)"),
      "rare-tag{Enter}",
    );
    await user.click(screen.getByLabelText("Document state"));
    await user.click(screen.getByRole("menuitemradio", { name: "All documents" }));
    await waitFor(() =>
      expect(search).toHaveBeenLastCalledWith(
        "x",
        [],
        25,
        expect.objectContaining({
          collection: "guides/api",
          tags: ["rare-tag"],
          source_type: "document",
          archive_scope: "all",
        }),
      ),
    );
    await user.click(screen.getByRole("button", { name: "Reset filters" }));
    await waitFor(() =>
      expect(search).toHaveBeenLastCalledWith(
        "x",
        [],
        25,
        expect.objectContaining({
          collection: undefined,
          tags: [],
          source_type: undefined,
          include_archived: false,
        }),
      ),
    );
  });

  it("restores full URL state and preserves filters across mode changes", async () => {
    renderAt(
      "/search?q=API&v=eng&collection=guides&doc_type=report&doc_type=note&tag=ops&include_archived=true&regex=true&case_sensitive=true",
    );
    const user = userEvent.setup();
    await waitFor(() => expect(search).toHaveBeenCalled());
    await user.click(
      screen.getByRole("button", { name: "Literal", pressed: false }),
    );
    await waitFor(() =>
      expect(grep).toHaveBeenLastCalledWith(
        "API",
        ["eng"],
        20,
        expect.objectContaining({
          collection: "guides",
          doc_types: ["report", "note"],
          tags: ["ops"],
          include_archived: true,
          regex: true,
          case_sensitive: true,
        }),
      ),
    );
    await openFilters(user);
    expect(screen.getByLabelText("Regular expression")).toBeChecked();
    await user.click(screen.getByLabelText("Case sensitive"));
    await waitFor(() =>
      expect(grep).toHaveBeenLastCalledWith(
        "API",
        ["eng"],
        20,
        expect.objectContaining({ case_sensitive: false }),
      ),
    );
  });

  it("does not call an incomplete empty response a genuine zero-match and retries unchanged queries", async () => {
    search.mockResolvedValue({
      ...response(),
      degraded: true,
      degradation_reason: "vector_store_unavailable",
    });
    renderAt();
    const user = userEvent.setup();
    await screen.findByText(/Search is incomplete/);
    expect(
      screen.queryByRole("heading", { name: /No results/ }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(search).toHaveBeenCalledTimes(2);
    await user.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(search).toHaveBeenCalledTimes(3));
  });

  it("uses literal-specific truncation copy and exact line/document counts", async () => {
    grep.mockResolvedValue({
      pattern: "x",
      regex: false,
      total_docs: 40,
      total_matches: 60,
      returned_docs: 1,
      returned_matches: 1,
      truncated: true,
      results: [
        { ...hit("Literal result"), matches: [{ section: null, text: "x" }] },
      ],
    });
    renderAt("/search?q=x&mode=literal");
    await screen.findByText("Literal result");
    expect(
      screen.getByText("Showing some matching documents"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/1 of 40 documents and 1 of 60/),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/Semantic search returns/),
    ).not.toBeInTheDocument();
  });

  it("ignores an older response that arrives after a filter request", async () => {
    let resolveOld!: (value: ReturnType<typeof response>) => void;
    search.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveOld = resolve;
      }),
    );
    search.mockResolvedValue(response([hit("Current result")]));
    renderAt();
    const user = userEvent.setup();
    await openFilters(user);
    await user.click(screen.getByRole("button", { name: "Toggle report" }));
    await screen.findByText("Current result");
    resolveOld(response([hit("Stale result")]));
    await waitFor(() =>
      expect(screen.queryByText("Stale result")).not.toBeInTheDocument(),
    );
    expect(screen.getByText("Current result")).toBeInTheDocument();
  });
});
