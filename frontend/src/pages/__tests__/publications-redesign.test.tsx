import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import PublicationsPage from "@/pages/publications";

vi.mock("@/lib/api", () => ({
  listPublications: vi.fn(),
  deletePublication: vi.fn(),
  getDocument: vi.fn(),
}));

import { deletePublication, listPublications } from "@/lib/api";

const listPublicationsMock = listPublications as unknown as ReturnType<
  typeof vi.fn
>;
const deletePublicationMock = deletePublication as unknown as ReturnType<
  typeof vi.fn
>;

const PUBLICATION = {
  slug: "runbook-public",
  share_url: "https://example.test/p/runbook-public",
  resource_type: "document",
  resource_uri: "akb://platform-docs/coll/guides/doc/runbook.md",
  vault: "platform-docs",
  title: "Incident runbook",
  mode: "live",
  expires_at: null,
  max_views: null,
  view_count: 12,
  allow_embed: false,
  section_filter: null,
  password_protected: true,
  created_at: "2026-08-20T00:00:00Z",
  snapshot_at: null,
} as const;

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/vault/platform-docs/publications"]}>
      <Routes>
        <Route
          path="/vault/:name/publications"
          element={<PublicationsPage />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  deletePublicationMock.mockResolvedValue({ deleted: 1 });
});

afterEach(cleanup);

describe("Publish page redesign", () => {
  it("keeps an empty vault focused without a forced context rail", async () => {
    listPublicationsMock.mockResolvedValue({ publications: [] });
    renderPage();

    expect(await screen.findByText("Nothing is public")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 1, name: "Publish" }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("publication-workspace")).toHaveClass(
      "w-full",
      "max-w-none",
    );
    expect(screen.getByTestId("publication-ledger-header")).toHaveClass(
      "bg-surface-2/55",
      "border-border-strong",
    );
    expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
    const policy = screen.getByTestId("publication-policy-summary");
    expect(within(policy).getByText("Public")).toBeInTheDocument();
    expect(within(policy).getByText("Read-only")).toBeInTheDocument();
    expect(within(policy).getByText("No sign-in required")).toBeInTheDocument();
    expect(
      within(policy).getByText(
        "Password, expiry, and view limits remain configurable for each link.",
      ),
    ).toBeInTheDocument();
    const browseLink = screen.getByRole("link", {
      name: /Browse vault content/i,
    });
    expect(browseLink).toHaveAttribute("href", "/vault/platform-docs");
  });

  it("reveals scalable search and type filters only for a larger registry", async () => {
    const user = userEvent.setup();
    const publications = Array.from({ length: 9 }, (_, index) => ({
      ...PUBLICATION,
      slug: `published-${index}`,
      share_url: `https://example.test/p/published-${index}`,
      title: index === 8 ? "Release bundle" : `Document ${index + 1}`,
      resource_type: index === 8 ? "file" : "document",
      resource_uri:
        index === 8
          ? "akb://platform-docs/file/release-bundle"
          : `akb://platform-docs/coll/guides/doc/document-${index + 1}.md`,
    }));
    listPublicationsMock.mockResolvedValue({ publications });
    renderPage();

    const search = await screen.findByPlaceholderText(
      "Filter published links…",
    );
    expect(screen.getByTestId("publication-controls")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Files" }));
    const ledger = screen.getByRole("list", { name: "Published links" });
    expect(within(ledger).getByText("Release bundle")).toBeInTheDocument();
    expect(within(ledger).getByTestId("publication-index")).toHaveTextContent(
      "9",
    );
    expect(within(ledger).queryByText("Document 1")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "All" }));
    await user.type(search, "Document 2");
    expect(within(ledger).getByText("Document 2")).toBeInTheDocument();
    expect(within(ledger).queryByText("Document 1")).not.toBeInTheDocument();
  });

  it("presents public links as a manageable ledger", async () => {
    const user = userEvent.setup();
    listPublicationsMock.mockResolvedValue({ publications: [PUBLICATION] });
    renderPage();

    const ledger = await screen.findByRole("list", { name: "Published links" });
    expect(within(ledger).getByText("Incident runbook")).toBeInTheDocument();
    expect(within(ledger).getByTestId("publication-index")).toHaveTextContent(
      "1",
    );
    expect(within(ledger).getByText("Live")).toBeInTheDocument();
    expect(within(ledger).getByText("Document")).toBeInTheDocument();
    expect(within(ledger).getByText("Password")).toBeInTheDocument();
    expect(within(ledger).getByText("No expiry")).toBeInTheDocument();
    expect(screen.getByText("12 total views")).toBeInTheDocument();
    expect(
      within(ledger).getByRole("link", { name: "Incident runbook" }),
    ).toHaveAttribute("href", "/vault/platform-docs/doc/guides%2Frunbook.md");
    expect(
      within(ledger).getByRole("link", { name: /Open public page/i }),
    ).toHaveAttribute("href", PUBLICATION.share_url);
    expect(screen.getByRole("link", { name: "Browse vault" })).toHaveAttribute(
      "href",
      "/vault/platform-docs",
    );

    await user.click(
      within(ledger).getByRole("button", { name: /More actions/i }),
    );
    await user.click(screen.getByRole("menuitem", { name: "Unpublish" }));
    expect(
      await screen.findByText('Unpublish "Incident runbook"?'),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Unpublish" }));
    await waitFor(() => {
      expect(deletePublicationMock).toHaveBeenCalledWith(
        "platform-docs",
        "runbook-public",
      );
    });
  });
});
