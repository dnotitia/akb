import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { GlobalSearchDialog } from "@/components/global-search-dialog";
import { listVaults, searchDocs, type CurrentUser } from "@/lib/api";
import { CurrentUserProvider } from "@/contexts/current-user-context";
import { recordRecentSearch } from "@/lib/recent-searches";
import { recordRecentDocumentView } from "@/lib/recent-document-views";

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

    await waitFor(() => expect(searchDocsMock).toHaveBeenCalledWith("postgres", [], 12));
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
      expect(searchDocsMock).toHaveBeenCalledWith("postgres tuning", [], 12),
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

  it("filters mixed top matches locally and cleans indexed context", async () => {
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

    await user.click(screen.getByRole("button", { name: /Tables, 1 result/i }));
    expect(screen.getByRole("option", { name: /Services/i })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /Platform guide/i })).toBeNull();
  });

  it("keeps an input-time content filter and offers a route out of filtered-empty results", async () => {
    const user = userEvent.setup();
    renderDialog();

    await user.click(screen.getByRole("button", { name: "Search knowledge" }));
    await user.click(screen.getByRole("button", { name: "Tables" }));
    await user.type(
      screen.getByRole("combobox", { name: "Search all accessible vaults" }),
      "postgres",
    );

    expect(await screen.findByText("No tables in these matches")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Tables, 0 results/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await user.click(screen.getByRole("button", { name: "Show all results" }));
    expect(
      screen.getByRole("option", { name: /PostgreSQL tuning/i }),
    ).toBeInTheDocument();
  });
});
