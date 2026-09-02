import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { VaultRefreshProvider } from "@/contexts/vault-refresh-context";
import { CurrentUserProvider } from "@/contexts/current-user-context";
import DocumentPage from "@/pages/document";
import { readRecentDocumentViews } from "@/lib/recent-document-views";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    getDocument: vi.fn(),
    getDocumentDiff: vi.fn(),
    getDocumentHistoryWithFallback: vi.fn(),
    getVaultInfo: vi.fn(),
    getRelations: vi.fn(),
    deleteDocument: vi.fn(),
    publishDoc: vi.fn(),
    unpublishDoc: vi.fn(),
    updateDocument: vi.fn(),
    browseVault: vi.fn(),
    moveDocument: vi.fn(),
  };
});

vi.mock("@/components/markdown-editor", () => ({
  default: ({
    value,
    onChange,
    ariaLabel,
  }: {
    value: string;
    onChange?: (value: string) => void;
    ariaLabel?: string;
  }) => (
    <textarea
      aria-label={ariaLabel}
      defaultValue={value}
      onChange={(event) => onChange?.(event.currentTarget.value)}
    />
  ),
}));

import {
  ApiError,
  DocumentRevisionApiError,
  deleteDocument,
  getDocument,
  getDocumentDiff,
  getDocumentHistoryWithFallback,
  getVaultInfo,
  getRelations,
  browseVault,
  moveDocument,
  updateDocument,
} from "@/lib/api";

const getDocumentMock = getDocument as unknown as ReturnType<typeof vi.fn>;
const deleteDocumentMock = deleteDocument as unknown as ReturnType<typeof vi.fn>;
const getDocumentDiffMock = getDocumentDiff as unknown as ReturnType<typeof vi.fn>;
const getDocumentHistoryMock = getDocumentHistoryWithFallback as unknown as ReturnType<typeof vi.fn>;
const getVaultInfoMock = getVaultInfo as unknown as ReturnType<typeof vi.fn>;
const getRelationsMock = getRelations as unknown as ReturnType<typeof vi.fn>;
const updateDocumentMock = updateDocument as unknown as ReturnType<typeof vi.fn>;
const browseVaultMock = browseVault as unknown as ReturnType<typeof vi.fn>;
const moveDocumentMock = moveDocument as unknown as ReturnType<typeof vi.fn>;

const SAMPLE_CONTENT = "# BodyHeading\n\nworld";
const UPDATED_COMMIT = "fedcba987654321"; // pragma: allowlist secret — synthetic Git commit
const CURRENT_USER = {
  user_id: "document-reader",
  username: "reader",
  email: "reader@example.com",
  display_name: "Document Reader",
  is_admin: false,
  auth_method: "local",
  key_class: null,
};

function makeDoc(overrides: Record<string, unknown> = {}) {
  // NB: the real GET /documents response exposes NO internal `id` — `uri`/
  // `path` is the sole identifier (see DocumentResponse). The mock must
  // mirror that, or guards keyed off `d.id` look fine in tests yet are
  // always-false in production.
  return {
    path: "notes/hello.md",
    title: "DocTitle",
    content: SAMPLE_CONTENT,
    current_commit: "abcdef1234567",
    type: null,
    status: null,
    tags: [],
    is_public: false,
    public_slug: null,
    created_by: null,
    updated_at: null,
    ...overrides,
  };
}

function LocationProbe() {
  const loc = useLocation();
  return (
    <>
      <div data-testid="location-pathname">{loc.pathname}</div>
      <div data-testid="location-search">{loc.search}</div>
      <div data-testid="location-state">{JSON.stringify(loc.state)}</div>
    </>
  );
}

function renderAt(url: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[url]}>
        <CurrentUserProvider user={CURRENT_USER}>
          <VaultRefreshProvider refetchVaults={vi.fn()} refetchTree={vi.fn()}>
            <Routes>
              <Route path="/vault/:name/doc/:id" element={<DocumentPage />} />
            </Routes>
          </VaultRefreshProvider>
        </CurrentUserProvider>
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function renderPreviewAt(url: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const backgroundLocation = {
    pathname: "/search",
    search: "?q=hello",
    hash: "",
    state: null,
    key: "search-result-list",
  };
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter
        initialEntries={[
          {
            pathname: url.split("?")[0],
            search: url.includes("?") ? `?${url.split("?")[1]}` : "",
            state: { documentPreview: true, backgroundLocation },
          },
        ]}
      >
        <CurrentUserProvider user={CURRENT_USER}>
          <VaultRefreshProvider refetchVaults={vi.fn()} refetchTree={vi.fn()}>
            <Routes>
              <Route
                path="/vault/:name/doc/:id"
                element={<DocumentPage presentation="preview" />}
              />
            </Routes>
          </VaultRefreshProvider>
        </CurrentUserProvider>
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  getDocumentMock.mockReset();
  deleteDocumentMock.mockReset();
  getDocumentDiffMock.mockReset();
  getDocumentHistoryMock.mockReset();
  getVaultInfoMock.mockReset();
  getRelationsMock.mockReset();
  updateDocumentMock.mockReset();
  browseVaultMock.mockReset();
  moveDocumentMock.mockReset();

  getDocumentMock.mockResolvedValue(makeDoc());
  getVaultInfoMock.mockResolvedValue({ role: "reader" });
  getRelationsMock.mockResolvedValue({ relations: [] });
  getDocumentHistoryMock.mockResolvedValue({
    kind: "document_history",
    uri: "akb://v/coll/notes/doc/hello.md",
    source: "document",
    history: [],
  });
  getDocumentDiffMock.mockResolvedValue({
    kind: "document_diff",
    file: "notes/hello.md",
    commit: "abcdef1234567",
    type: "modified",
    diff: "--- a/notes/hello.md\n+++ b/notes/hello.md\n@@ -1,2 +1,2 @@\n-Old title\n+New title\n body",
  });
  updateDocumentMock.mockResolvedValue({
    current_commit: UPDATED_COMMIT,
    commit_hash: UPDATED_COMMIT,
  });
  browseVaultMock.mockResolvedValue({
    items: [
      { type: "collection", path: "notes" },
      { type: "collection", path: "archive" },
      { type: "document", path: "notes/hello.md", name: "DocTitle" },
    ],
  });
  moveDocumentMock.mockResolvedValue({
    kind: "document_write",
    uri: "akb://v/coll/archive/doc/hello.md",
    vault: "v",
    path: "archive/hello.md",
    commit_hash: UPDATED_COMMIT,
    current_commit: UPDATED_COMMIT,
    action: "moved",
  });
  deleteDocumentMock.mockResolvedValue({ deleted: true });

  // Keep a safe transport fallback for components that issue direct requests.
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ activity: [] }),
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("DocumentPage view toggle", () => {
  it("keeps the human title primary and moves file identifiers into technical details", async () => {
    const user = userEvent.setup();
    renderAt("/vault/v/doc/notes%2Fhello.md");

    const heading = await screen.findByRole("heading", { level: 1, name: "DocTitle" });
    const header = heading.closest("header");
    expect(header).not.toBeNull();
    expect(within(header as HTMLElement).queryByText("hello.md")).not.toBeInTheDocument();
    expect(screen.getAllByText("notes").length).toBeGreaterThan(0);

    await user.click(screen.getByRole("button", { name: "Open document panel" }));
    await user.click(screen.getByText("Technical details"));
    expect(screen.getByText("hello.md")).toBeInTheDocument();
    expect(screen.getByText("notes/hello.md")).toBeInTheDocument();
  });

  it("records a successful document read for Home resume", async () => {
    getDocumentMock.mockResolvedValue(makeDoc({
      type: "report",
      updated_at: "2026-08-25T09:00:00Z",
    }));
    renderAt("/vault/v/doc/notes%2Fhello.md");

    await screen.findByRole("heading", { level: 1, name: "DocTitle" });
    await waitFor(() => {
      expect(readRecentDocumentViews(CURRENT_USER.user_id)[0]).toEqual(
        expect.objectContaining({
          vault: "v",
          path: "notes/hello.md",
          title: "DocTitle",
          type: "report",
          updatedAt: "2026-08-25T09:00:00Z",
        }),
      );
    });
  });

  it("keeps preview history state across view tabs and clears it for full-page reading", async () => {
    const user = userEvent.setup();
    renderPreviewAt("/vault/v/doc/notes%2Fhello.md");

    const workspace = await screen.findByRole("region", {
      name: "Document workspace",
    });
    expect(workspace).toHaveAttribute("data-presentation", "preview");
    expect(screen.getByTestId("location-state")).toHaveTextContent(
      '"documentPreview":true',
    );

    await user.click(screen.getByRole("tab", { name: "Raw" }));
    expect(await screen.findByTestId("doc-raw")).toBeInTheDocument();
    expect(screen.getByTestId("location-state")).toHaveTextContent(
      '"documentPreview":true',
    );

    await user.click(screen.getByRole("button", { name: "Full page" }));
    expect(screen.getByTestId("location-state")).toHaveTextContent("null");
    expect(screen.getByTestId("location-search")).toHaveTextContent("view=raw");
  });

  it("links a preview back to its Vault overview", async () => {
    renderPreviewAt("/vault/v/doc/notes%2Fhello.md");

    expect(
      await screen.findByRole("link", { name: "Open v Vault overview" }),
    ).toHaveAttribute("href", "/vault/v");
  });

  it("keeps the Vault guide readable inside a search preview", async () => {
    getDocumentMock.mockResolvedValue(
      makeDoc({
        path: "overview/vault-skill.md",
        title: "v Guide",
      }),
    );
    renderPreviewAt("/vault/v/doc/overview%2Fvault-skill.md");

    expect(
      await screen.findByRole("heading", { level: 1, name: "v Guide" }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("location-state")).toHaveTextContent(
      '"documentPreview":true',
    );
  });

  it("uses a full-width document canvas with an overlay details drawer", async () => {
    const user = userEvent.setup();
    renderAt("/vault/v/doc/notes%2Fhello.md");

    const heading = await screen.findByRole("heading", { level: 1, name: "DocTitle" });
    expect(screen.getByRole("region", { name: "Document workspace" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Document content" })).toBeInTheDocument();
    expect(screen.getAllByText("abcdef1").length).toBeGreaterThan(0);
    expect(
      within(heading.closest("header") as HTMLElement).queryByText(
        "akb://v/coll/notes/doc/hello.md",
      ),
    ).not.toBeInTheDocument();

    const article = screen.getByRole("article");
    expect(article).toHaveClass("w-full", "p-2", "sm:p-3");
    expect(article).not.toHaveClass("max-w-6xl");
    expect(article.querySelector(".document-reading-flow")).toBeInTheDocument();
    const documentViewTabs = screen.getByRole("tablist", { name: "Document view" });
    expect(documentViewTabs.parentElement).toHaveClass("justify-end");
    expect(
      screen.getByLabelText("Document statistics: 3 lines, 20 Bytes"),
    ).toBeInTheDocument();
    const copyMarkdown = screen.getByRole("button", { name: "Copy markdown" });
    expect(copyMarkdown).toBeVisible();
    expect(copyMarkdown.nextElementSibling).toBe(documentViewTabs);
    expect(document.getElementById("document-reading-canvas")).not.toHaveClass(
      "lg:pr-80",
      "xl:pr-88",
    );

    const details = document.getElementById("document-details-panel") as HTMLElement;
    const detailsToggle = screen.getByRole("button", { name: "Open document panel" });
    expect(details).toHaveAttribute("aria-hidden", "true");
    expect(details).toHaveClass("translate-x-full");
    expect(detailsToggle).toHaveAttribute("aria-expanded", "false");

    await user.click(detailsToggle);
    expect(details).toHaveAttribute("aria-hidden", "false");
    expect(details).toHaveClass("translate-x-0");
    expect(detailsToggle).toHaveAttribute("aria-expanded", "true");
    expect(detailsToggle).toHaveAccessibleName("Hide document panel");
    expect(details).toHaveClass("lg:w-96");

    const detailViews = screen.getByRole("tablist", { name: "Document detail views" });
    const infoTab = screen.getByRole("tab", { name: "Info" });
    expect(detailViews).toContainElement(infoTab);
    expect(infoTab).toHaveAttribute("aria-selected", "true");
    const infoPanel = screen.getByRole("tabpanel", { name: "Info" });
    expect(infoPanel).toHaveClass("min-h-0", "flex-1", "overflow-y-auto");

    await user.click(screen.getByRole("tab", { name: /^Outline/ }));
    expect(screen.getByRole("heading", { level: 3, name: "On this page" })).toBeVisible();
    expect(screen.getByRole("tabpanel", { name: /^Outline/ })).toHaveClass(
      "min-h-0",
      "flex-1",
      "overflow-y-auto",
    );

    await user.click(screen.getByRole("button", { name: "Close document panel" }));
    expect(details).toHaveAttribute("aria-hidden", "true");
    await waitFor(() => expect(detailsToggle).toHaveFocus());

    await user.click(screen.getByRole("button", { name: "History" }));
    expect(details).toHaveAttribute("aria-hidden", "false");
    expect(screen.getByRole("tab", { name: /^History/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );

    await user.keyboard("{Escape}");
    expect(details).toHaveAttribute("aria-hidden", "true");
    await waitFor(() => expect(detailsToggle).toHaveFocus());
  });

  it("renders Markdown by default", async () => {
    renderAt("/vault/v/doc/notes%2Fhello.md");
    // Body markdown headings are demoted one level (the page title is the sole
    // <h1>), so the body `# BodyHeading` renders as an <h2>.
    await waitFor(() =>
      expect(
        screen.getByRole("heading", { level: 2, name: "BodyHeading" }),
      ).toBeInTheDocument(),
    );
    // The raw <pre> should NOT be present.
    expect(screen.queryByTestId("doc-raw")).not.toBeInTheDocument();
  });

  it("keeps Edit in the document header and exits a clean editor with Cancel", async () => {
    const user = userEvent.setup();
    getVaultInfoMock.mockResolvedValue({ role: "owner" });
    renderAt("/vault/v/doc/notes%2Fhello.md");

    const edit = await screen.findByRole("button", { name: "Edit" });
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
      "Rendered",
      "Raw",
    ]);

    await user.click(edit);
    expect(await screen.findByText("Editing document")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Document title" })).toHaveValue("DocTitle");
    expect(screen.queryByRole("tablist", { name: "Document view" })).not.toBeInTheDocument();

    const cancel = screen.getByRole("button", { name: "Cancel" });
    expect(cancel).toBeEnabled();
    await user.click(cancel);

    await screen.findByRole("heading", { level: 2, name: "BodyHeading" });
    expect(screen.getByTestId("location-search")).toHaveTextContent("");
    await waitFor(() => expect(screen.getByRole("button", { name: "Edit" })).toHaveFocus());
  });

  it("confirms before Cancel discards body edits and then returns to reading", async () => {
    const user = userEvent.setup();
    getVaultInfoMock.mockResolvedValue({ role: "owner" });
    renderAt("/vault/v/doc/notes%2Fhello.md");

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const editor = await screen.findByRole("textbox", {
      name: "Document body (markdown)",
    });
    await user.type(editor, "XY");
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(
      await screen.findByRole("heading", { name: "Discard unsaved changes?" }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Discard changes" }));

    await screen.findByRole("heading", { level: 2, name: "BodyHeading" });
    expect(screen.queryByRole("textbox", { name: "Document body (markdown)" })).not.toBeInTheDocument();
    expect(updateDocumentMock).not.toHaveBeenCalled();
  });

  it("returns focus to the edit Cancel action when discard confirmation is dismissed", async () => {
    const user = userEvent.setup();
    getVaultInfoMock.mockResolvedValue({ role: "owner" });
    renderAt("/vault/v/doc/notes%2Fhello.md");

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.type(
      await screen.findByRole("textbox", { name: "Document body (markdown)" }),
      "unsaved",
    );
    const pageCancel = screen.getByRole("button", { name: "Cancel" });
    await user.click(pageCancel);
    await screen.findByRole("heading", { name: "Discard unsaved changes?" });

    await user.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(pageCancel).toHaveFocus());
    expect(
      screen.getByRole("textbox", { name: "Document body (markdown)" }),
    ).toBeInTheDocument();
  });

  it("merges a compact summary into the document viewer toolbar without opening Details", async () => {
    getDocumentMock.mockResolvedValue(
      makeDoc({ summary: "A concise orientation to the document before the full body begins." }),
    );
    renderAt("/vault/v/doc/notes%2Fhello.md");

    const summary = await screen.findByRole("note", { name: "Document summary" });
    expect(summary).toHaveClass("hidden", "lg:flex", "flex-1", "border-l");
    expect(summary).toHaveTextContent("Summary");
    expect(summary).toHaveTextContent(
      "A concise orientation to the document before the full body begins.",
    );
    const statistics = screen.getByLabelText("Document statistics: 3 lines, 20 Bytes");
    const copyMarkdown = screen.getByRole("button", { name: "Copy markdown" });
    expect(summary.parentElement).toContainElement(statistics);
    expect(summary.parentElement?.nextElementSibling).toBe(copyMarkdown);
    expect(screen.queryByRole("region", { name: "Document summary" })).not.toBeInTheDocument();
    expect(document.getElementById("document-details-panel")).toHaveAttribute(
      "aria-hidden",
      "true",
    );
  });

  it("omits the reading summary when the backend does not provide one", async () => {
    renderAt("/vault/v/doc/notes%2Fhello.md");

    await screen.findByRole("heading", { level: 2, name: "BodyHeading" });
    expect(screen.queryByRole("note", { name: "Document summary" })).not.toBeInTheDocument();
  });

  it("counts logical lines and UTF-8 bytes", async () => {
    getDocumentMock.mockResolvedValue(makeDoc({ content: "가\n나" }));
    renderAt("/vault/v/doc/notes%2Fhello.md");

    expect(
      await screen.findByLabelText("Document statistics: 2 lines, 7 Bytes"),
    ).toBeInTheDocument();
  });

  it("loads relations keyed by the document path (not a nonexistent id)", async () => {
    // Regression: the panel used to fetch via `d.id`, which the API never
    // returns, so relations silently never loaded ("No relations yet.").
    renderAt("/vault/v/doc/notes%2Fhello.md");
    await screen.findByRole("heading", { level: 2, name: "BodyHeading" });
    await waitFor(() =>
      expect(getRelationsMock).toHaveBeenCalledWith("v", "notes/hello.md"),
    );
  });

  it("does not include a foreign endpoint in the outer relation count", async () => {
    getRelationsMock.mockResolvedValue({
      relations: [
        {
          direction: "outgoing",
          relation: "references",
          uri: "akb://v/coll/notes/doc/local.md",
          resource_type: "doc",
          kind: "explicit",
        },
        {
          direction: "outgoing",
          relation: "references",
          uri: "akb://private/coll/notes/doc/hidden.md",
          resource_type: "doc",
          kind: "explicit",
        },
      ],
    });

    renderAt("/vault/v/doc/notes%2Fhello.md");

    await userEvent.setup().click(await screen.findByRole("button", { name: "Open document panel" }));
    const relationsTab = await screen.findByRole("tab", { name: /^Relations/ });
    expect(relationsTab).toHaveTextContent(/^Relations1$/);
    expect(relationsTab).not.toHaveTextContent("2");
  });

  it("loads logical document lineage instead of path-scoped Vault activity", async () => {
    renderAt("/vault/v/doc/notes%2Fhello.md");

    await screen.findByRole("heading", { level: 1, name: "DocTitle" });
    await waitFor(() =>
      expect(getDocumentHistoryMock).toHaveBeenCalledWith(
        "v",
        "notes/hello.md",
        20,
      ),
    );
  });

  it("opens a History revision as a full-canvas Diff and restores History focus", async () => {
    const user = userEvent.setup();
    getDocumentHistoryMock.mockResolvedValue({
      kind: "document_history",
      uri: "akb://v/coll/notes/doc/hello.md",
      source: "document",
      history: [
        {
          hash: "abcdef1234567",
          message: "Update title",
          author: "user-1",
          author_name: "Kim",
          date: "2026-09-02T00:00:00Z",
        },
        {
          hash: "1234567abcdef", // pragma: allowlist secret — synthetic Git commit
          message: "Create document",
          author: "user-1",
          author_name: "Kim",
          date: "2026-09-01T00:00:00Z",
        },
      ],
    });
    renderAt("/vault/v/doc/notes%2Fhello.md");

    await user.click(await screen.findByRole("button", { name: "History" }));
    const changes = await screen.findByRole("button", {
      name: "View changes in version abcdef1",
    });
    await user.click(changes);

    expect(await screen.findByRole("table", { name: /unified document changes/i })).toBeInTheDocument();
    expect(screen.getByTestId("location-search")).toHaveTextContent(
      "commit=abcdef1234567&view=diff",
    );
    expect(document.getElementById("document-details-panel")).toHaveAttribute(
      "aria-hidden",
      "true",
    );
    expect(screen.queryByRole("tablist", { name: "Document view" })).toBeNull();

    await user.click(screen.getByRole("button", { name: "Back to version" }));
    await screen.findByRole("heading", { level: 2, name: "BodyHeading" });
    expect(screen.getByTestId("location-search")).toHaveTextContent(
      "commit=abcdef1234567",
    );
    expect(screen.getByTestId("location-search")).not.toHaveTextContent("view=diff");
    expect(document.getElementById("document-details-panel")).toHaveAttribute(
      "aria-hidden",
      "false",
    );
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "View changes in version abcdef1" }),
      ).toHaveFocus(),
    );
  });

  it("keeps Search preview route state while entering Diff", async () => {
    const user = userEvent.setup();
    getDocumentHistoryMock.mockResolvedValue({
      kind: "document_history",
      source: "document",
      history: [{
        hash: "abcdef1234567",
        message: "Update title",
        author: "user-1",
        date: "2026-09-02T00:00:00Z",
      }],
    });
    renderPreviewAt("/vault/v/doc/notes%2Fhello.md");

    await user.click(await screen.findByRole("button", { name: "History" }));
    await user.click(await screen.findByRole("button", {
      name: "View changes in version abcdef1",
    }));

    expect(await screen.findByRole("table", { name: /unified document changes/i })).toBeInTheDocument();
    expect(screen.getByTestId("location-state")).toHaveTextContent(
      '"documentPreview":true',
    );
    expect(screen.getByTestId("location-search")).toHaveTextContent("view=diff");
  });

  it("keeps the Diff frame available for an unsupported direct revision URL", async () => {
    getDocumentDiffMock.mockRejectedValue(
      new DocumentRevisionApiError("Not found", 404),
    );

    renderAt(
      "/vault/v/doc/notes%2Fhello.md?commit=missing-revision&view=diff",
    );

    expect(
      await screen.findByRole("heading", {
        name: "Changes are not available on this server",
      }),
    ).toBeInTheDocument();
    expect(getDocumentMock).toHaveBeenCalledWith(
      "v",
      "notes/hello.md",
      undefined,
    );
  });

  it("?view=raw renders the raw markdown inside <pre>", async () => {
    renderAt("/vault/v/doc/notes%2Fhello.md?view=raw");
    const pre = await screen.findByTestId("doc-raw");
    expect(pre.tagName).toBe("PRE");
    expect(pre.textContent).toBe(SAMPLE_CONTENT);
    // The rendered-markdown body heading should NOT be present.
    expect(
      screen.queryByRole("heading", { level: 2, name: "BodyHeading" }),
    ).not.toBeInTheDocument();
    // No `.prose` container is rendered in raw mode.
    expect(document.querySelector(".prose")).toBeNull();
  });

  it("clicking the toggle button switches to raw view and updates the URL", async () => {
    const user = userEvent.setup();
    renderAt("/vault/v/doc/notes%2Fhello.md");

    // Wait for the page to settle in rendered mode.
    await screen.findByRole("heading", { level: 2, name: "BodyHeading" });

    const rawTab = screen.getByRole("tab", { name: "Raw" });
    const renderedTab = screen.getByRole("tab", { name: "Rendered" });
    expect(rawTab).toHaveAttribute("aria-selected", "false");
    expect(renderedTab).toHaveAttribute("aria-selected", "true");
    await user.click(rawTab);

    // Pre appears now.
    const pre = await screen.findByTestId("doc-raw");
    expect(pre).toBeInTheDocument();

    // Selection flips on the segmented control.
    expect(screen.getByRole("tab", { name: "Raw" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Rendered" })).toHaveAttribute("aria-selected", "false");

    // The URL search now contains view=raw.
    expect(screen.getByTestId("location-search")).toHaveTextContent("view=raw");
  });

  it("Copy button writes raw content to the clipboard and flips to COPIED", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
      writable: true,
    });

    renderAt("/vault/v/doc/notes%2Fhello.md?view=raw");

    await screen.findByTestId("doc-raw");
    const copy = screen.getByRole("button", { name: /copy markdown/i });
    expect(copy).toHaveTextContent("Copy");

    // Direct .click() avoids userEvent's clipboard-aware setup, which
    // installs its own ClipboardStubImpl and shadows our writeText spy.
    copy.click();

    await waitFor(() => {
      expect(writeText).toHaveBeenCalledWith(SAMPLE_CONTENT);
    });

    // Same button element (React reuses the node); after copy its visible text
    // flips to COPIED and its aria-label flips to "Markdown copied".
    await waitFor(() => {
      expect(copy).toHaveTextContent("Copied");
    });
    expect(copy).toHaveAccessibleName(/markdown copied/i);
  });

  it("returns an edited HEAD-pinned URL to the live document cache", async () => {
    const user = userEvent.setup();
    let saved = false;
    getVaultInfoMock.mockResolvedValue({ role: "owner" });
    updateDocumentMock.mockImplementation(async () => {
      saved = true;
      return {
        current_commit: UPDATED_COMMIT,
        commit_hash: UPDATED_COMMIT,
      };
    });
    getDocumentMock.mockImplementation(async () =>
      makeDoc(
        saved
          ? {
              content: "Updated from pinned HEAD",
              current_commit: UPDATED_COMMIT,
            }
          : {},
      ),
    );
    renderAt(
      "/vault/v/doc/notes%2Fhello.md?commit=abcdef1234567&view=edit",
    );

    const editor = await screen.findByRole("textbox", {
      name: "Document body (markdown)",
    });
    await user.clear(editor);
    await user.type(editor, "Updated from pinned HEAD");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(updateDocumentMock).toHaveBeenCalledWith(
        "v",
        "notes/hello.md",
        { content: "Updated from pinned HEAD" },
      ),
    );
    await waitFor(() =>
      expect(screen.getByTestId("location-search")).toHaveTextContent(""),
    );
    // The optimistic document and the live-query document replace each other
    // during the URL transition. Re-query the current DOM on each attempt so
    // the assertion cannot retain a node detached by that handoff.
    await waitFor(
      () =>
        expect(screen.getByText("Updated from pinned HEAD")).toBeInTheDocument(),
      { timeout: 5_000 },
    );
  });

  it("exposes document deletion in the header for writers and leaves the stale route", async () => {
    const user = userEvent.setup();
    getVaultInfoMock.mockResolvedValue({ role: "writer" });
    renderAt("/vault/v/doc/notes%2Fhello.md");

    await user.click(await screen.findByRole("button", { name: "Actions for DocTitle" }));
    await user.click(screen.getByRole("menuitem", { name: "Delete document" }));
    await user.click(screen.getByRole("button", { name: "Delete document" }));

    await waitFor(() =>
      expect(deleteDocumentMock).toHaveBeenCalledWith("v", "notes/hello.md"),
    );
    expect(screen.getByTestId("location-pathname")).toHaveTextContent("/vault/v");
  });

  it("keeps Move discoverable for readers and does not expose a separate Rename action", async () => {
    const user = userEvent.setup();
    renderAt("/vault/v/doc/notes%2Fhello.md");

    await user.click(await screen.findByRole("button", { name: "Actions for DocTitle" }));
    const move = screen.getByRole("menuitem", { name: /Move document/i });
    expect(screen.queryByRole("menuitem", { name: /Rename title/i })).not.toBeInTheDocument();
    expect(move).toHaveAttribute("aria-disabled", "true");
    expect(move).toHaveTextContent("Writer access or higher is required.");
  });

  it("edits the human title and body in one save without changing the document path", async () => {
    const user = userEvent.setup();
    let saved = false;
    getVaultInfoMock.mockResolvedValue({ role: "writer" });
    updateDocumentMock.mockImplementation(async () => {
      saved = true;
      return {
        current_commit: UPDATED_COMMIT,
        commit_hash: UPDATED_COMMIT,
      };
    });
    getDocumentMock.mockImplementation(async () =>
      makeDoc(
        saved
          ? { title: "API contract", content: "Updated contract body" }
          : {},
      ),
    );
    renderAt("/vault/v/doc/notes%2Fhello.md");

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const title = screen.getByRole("textbox", { name: "Document title" });
    const body = screen.getByRole("textbox", { name: "Document body (markdown)" });
    await user.clear(title);
    await user.type(title, "API contract");
    await user.clear(body);
    await user.type(body, "Updated contract body");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(updateDocumentMock).toHaveBeenCalledWith(
        "v",
        "notes/hello.md",
        {
          content: "Updated contract body",
          title: "API contract",
          title_conflict_policy: "reject",
        },
      ),
    );
    expect(await screen.findByRole("heading", { level: 1, name: "API contract" })).toBeInTheDocument();
    expect(screen.getByTestId("location-pathname")).toHaveTextContent(
      "/vault/v/doc/notes%2Fhello.md",
    );
    expect(screen.queryByText("Document renamed")).not.toBeInTheDocument();
  });

  it("blocks an exact title twin in Edit and requires an explicit keep-both save", async () => {
    const user = userEvent.setup();
    getVaultInfoMock.mockResolvedValue({ role: "writer" });
    browseVaultMock.mockResolvedValue({
      items: [
        { type: "collection", path: "notes" },
        { type: "document", path: "notes/hello.md", name: "DocTitle" },
        { type: "document", path: "notes/api-contract.md", name: "API contract" },
      ],
    });
    getDocumentMock.mockImplementation(async (_vault: string, path: string) =>
      path === "notes/api-contract.md"
        ? makeDoc({ path, title: "API contract" })
        : makeDoc(),
    );
    renderAt("/vault/v/doc/notes%2Fhello.md");

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const title = screen.getByRole("textbox", { name: "Document title" });
    await user.clear(title);
    await user.type(title, "API contract");
    await user.tab();
    expect(
      await screen.findByText("“API contract” already exists here"),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(updateDocumentMock).not.toHaveBeenCalled();
    expect(await screen.findByText("This document already exists")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Open existing" }));
    expect(
      await screen.findByRole("heading", {
        name: "Discard changes and open the existing document?",
      }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Keep editing" }));
    expect(screen.getByTestId("location-pathname")).toHaveTextContent(
      "/vault/v/doc/notes%2Fhello.md",
    );
    expect(screen.getByRole("textbox", { name: "Document title" })).toHaveValue(
      "API contract",
    );

    await user.click(screen.getByRole("button", { name: "Save duplicate title" }));
    await waitFor(() =>
      expect(updateDocumentMock).toHaveBeenCalledWith(
        "v",
        "notes/hello.md",
        { title: "API contract", title_conflict_policy: "allow" },
      ),
    );
  });

  it("recovers from a server-side title race inside the Edit form", async () => {
    const user = userEvent.setup();
    getVaultInfoMock.mockResolvedValue({ role: "writer" });
    getDocumentMock.mockImplementation(async (_vault: string, path: string) =>
      path === "notes/api-contract.md"
        ? makeDoc({ path, title: "API contract", content: "Different body" })
        : makeDoc(),
    );
    updateDocumentMock
      .mockRejectedValueOnce(
        new ApiError("duplicate title", 409, {
          code: "document_title_conflict",
          details: {
            title: "API contract",
            collection: "notes",
            existing_path: "notes/api-contract.md",
            existing_title: "API contract",
          },
        }),
      )
      .mockResolvedValueOnce({
        current_commit: UPDATED_COMMIT,
        commit_hash: UPDATED_COMMIT,
      });
    renderAt("/vault/v/doc/notes%2Fhello.md");

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const title = screen.getByRole("textbox", { name: "Document title" });
    await user.clear(title);
    await user.type(title, "API contract");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(
      await screen.findByText("“API contract” already exists here"),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save duplicate title" }));
    await waitFor(() =>
      expect(updateDocumentMock).toHaveBeenLastCalledWith(
        "v",
        "notes/hello.md",
        { title: "API contract", title_conflict_policy: "allow" },
      ),
    );
  });

  it("moves a writer to the backend-returned path and names the destination", async () => {
    const user = userEvent.setup();
    getVaultInfoMock.mockResolvedValue({ role: "writer" });
    getDocumentMock.mockImplementation(async (_vault: string, path: string) =>
      makeDoc({ path }),
    );
    renderAt("/vault/v/doc/notes%2Fhello.md");

    await user.click(await screen.findByRole("button", { name: "Actions for DocTitle" }));
    await user.click(screen.getByRole("menuitem", { name: "Move document" }));
    await user.click(await screen.findByLabelText("Target collection"));
    await user.click(screen.getByRole("menuitemradio", { name: "archive" }));
    await user.click(screen.getByRole("button", { name: "Move document" }));

    await waitFor(() =>
      expect(moveDocumentMock).toHaveBeenCalledWith(
        "v",
        "notes/hello.md",
        { collection: "archive", title_conflict_policy: "reject" },
      ),
    );
    await waitFor(() =>
      expect(screen.getByTestId("location-pathname")).toHaveTextContent(
        "/vault/v/doc/archive%2Fhello.md",
      ),
    );
    const movedStatus = (await screen.findByText("Moved to archive")).closest(
      '[role="status"]',
    );
    expect(movedStatus).toHaveTextContent("title, links, and version history were preserved");
    expect(movedStatus).not.toHaveTextContent("archive/hello.md");
  });
});
